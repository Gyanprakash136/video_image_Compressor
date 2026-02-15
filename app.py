import os
import uuid
import subprocess
import requests
import time
import logging
import threading
import shutil
import json
import socket
from typing import Optional, Dict, Any

import redis
from pythonjsonlogger import jsonlogger
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Header

# ==========================================================
# CONFIG VALIDATION
# ==========================================================

INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY")
LMS_STORE_URL = os.getenv("LMS_STORE_URL")
REDIS_URL = os.getenv("REDIS_URL")

if not INTERNAL_SERVICE_KEY:
    raise RuntimeError("INTERNAL_SERVICE_KEY must be set")

if not LMS_STORE_URL:
    raise RuntimeError("LMS_STORE_URL must be set")

# ==========================================================
# LOGGING SETUP (JSON Structured Logging)
# ==========================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger("media-compressor")
logger.setLevel(LOG_LEVEL)

handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter()
handler.setFormatter(formatter)
logger.addHandler(handler)


def log_event(event: str, level: str = "info", **kwargs):
    data = {"event": event, **kwargs}
    if level == "error":
        logger.error(data)
    else:
        logger.info(data)


# ==========================================================
# APP INIT
# ==========================================================

app = FastAPI()

VIDEO_DIR = "temp_videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

MAX_VIDEO_SIZE_MB = 500
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi"}
REDIS_TTL = 7200  # 2 hours


# ==========================================================
# JOB MANAGER
# ==========================================================

class JobManager:
    def __init__(self, redis_url: Optional[str]):
        self.use_redis = bool(redis_url)
        self.redis_client = None
        self.memory_store = {}
        self.memory_lock = threading.Lock()

        if self.use_redis:
            try:
                self.redis_client = redis.from_url(redis_url, decode_responses=True)
                self.redis_client.ping()
                log_event("redis_connected")
            except Exception as e:
                raise RuntimeError(f"Redis connection failed: {e}")
        else:
            log_event("memory_mode_enabled")

    def set_job(self, video_id: str, data: Dict[str, Any]):
        if self.use_redis:
            self.redis_client.setex(
                f"job:{video_id}",
                REDIS_TTL,
                json.dumps(data)
            )
        else:
            with self.memory_lock:
                self.memory_store[video_id] = data

    def get_job(self, video_id: str) -> Optional[Dict[str, Any]]:
        if self.use_redis:
            raw = self.redis_client.get(f"job:{video_id}")
            return json.loads(raw) if raw else None
        else:
            with self.memory_lock:
                return self.memory_store.get(video_id)

    def update_status(self, video_id: str, status: str, extra: Dict[str, Any] = None):
        job = self.get_job(video_id)
        if not job:
            return

        job["status"] = status
        if extra:
            job.update(extra)

        self.set_job(video_id, job)

    def delete_job(self, video_id: str):
        if self.use_redis:
            self.redis_client.delete(f"job:{video_id}")
        else:
            with self.memory_lock:
                self.memory_store.pop(video_id, None)


job_manager = JobManager(REDIS_URL)


# ==========================================================
# FILE CLEANUP SAFETY (Prevents File Leaks)
# ==========================================================

def cleanup_orphan_files():
    now = time.time()
    expiration = 2 * 60 * 60  # 2 hours

    for filename in os.listdir(VIDEO_DIR):
        path = os.path.join(VIDEO_DIR, filename)
        if os.path.isfile(path):
            if now - os.path.getmtime(path) > expiration:
                try:
                    os.remove(path)
                    log_event("orphan_file_deleted", file=filename)
                except Exception:
                    pass


# ==========================================================
# COMPRESSION
# ==========================================================

def compress_video_ffmpeg(input_path, output_path):
    command = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vf", "scale=-2:'min(720,ih)'",  # Prevent upscaling
        "-c:v", "libx264",
        "-crf", "30",
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(command, check=True)


# ==========================================================
# LMS CALLBACK
# ==========================================================

def send_to_lms(video_id, organization_id, compressed_path):
    retries = 3
    backoff = 1

    for attempt in range(1, retries + 1):
        try:
            with open(compressed_path, "rb") as f:
                files = {"file": f}
                data = {
                    "video_id": video_id,
                    "organization_id": organization_id
                }
                response = requests.post(
                    LMS_STORE_URL,
                    files=files,
                    data=data,
                    timeout=30
                )

            if response.status_code == 200:
                log_event("callback_success", video_id=video_id)
                return True

        except Exception as e:
            log_event("callback_error", level="error", video_id=video_id, error=str(e))

        if attempt < retries:
            time.sleep(backoff)
            backoff *= 2

    return False


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def background_process_video(video_id, organization_id, input_path, output_path):
    try:
        job_manager.update_status(video_id, "processing")
        log_event("compression_started", video_id=video_id)

        start = time.time()
        compress_video_ffmpeg(input_path, output_path)
        duration = time.time() - start

        log_event("compression_completed", video_id=video_id, duration=duration)

        success = send_to_lms(video_id, organization_id, output_path)

        if success:
            job_manager.update_status(video_id, "awaiting_confirmation", {
                "compressed_path": output_path
            })
        else:
            job_manager.update_status(video_id, "failed")

    except Exception as e:
        log_event("compression_failed", level="error", video_id=video_id, error=str(e))
        job_manager.update_status(video_id, "failed")


# ==========================================================
# ENDPOINTS
# ==========================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "redis_enabled": job_manager.use_redis,
        "host": socket.gethostname()
    }


@app.post("/video/receive")
async def receive_video(
    background_tasks: BackgroundTasks,
    video_id: str = Form(...),
    organization_id: str = Form(...),
    file: UploadFile = File(...),
    x_internal_service_key: str = Header(None)
):
    if x_internal_service_key != INTERNAL_SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cleanup_orphan_files()

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported format")

    input_filename = f"{uuid.uuid4()}_raw{ext}"
    output_filename = f"{uuid.uuid4()}_720p{ext}"

    input_path = os.path.join(VIDEO_DIR, input_filename)
    output_path = os.path.join(VIDEO_DIR, output_filename)

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
    if file_size_mb > MAX_VIDEO_SIZE_MB:
        os.remove(input_path)
        raise HTTPException(status_code=400, detail="File too large")

    job_data = {
        "status": "queued",
        "file_path": input_path,
        "compressed_path": "",
        "created_at": time.time(),
        "video_id": video_id,
        "org_id": organization_id
    }

    job_manager.set_job(video_id, job_data)

    background_tasks.add_task(
        background_process_video,
        video_id,
        organization_id,
        input_path,
        output_path
    )

    return {"status": "queued", "video_id": video_id}


@app.post("/video/confirm")
def confirm_video(
    video_id: str = Form(...),
    x_internal_service_key: str = Header(None)
):
    if x_internal_service_key != INTERNAL_SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job = job_manager.get_job(video_id)
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    if job["status"] == "completed":
        return {"status": "completed", "video_id": video_id}

    if job["status"] != "awaiting_confirmation":
        raise HTTPException(status_code=400, detail="Not ready")

    # Delete files
    try:
        if os.path.exists(job["file_path"]):
            os.remove(job["file_path"])
        if os.path.exists(job["compressed_path"]):
            os.remove(job["compressed_path"])
    except Exception:
        pass

    job_manager.update_status(video_id, "completed")

    return {"status": "completed", "video_id": video_id}


@app.get("/video/status/{video_id}")
def status(video_id: str, x_internal_service_key: str = Header(None)):
    if x_internal_service_key != INTERNAL_SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job = job_manager.get_job(video_id)
    return {
        "video_id": video_id,
        "status": job["status"] if job else "not_found"
    }
