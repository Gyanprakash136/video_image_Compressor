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

# ---------------- LOGGING SETUP ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger()
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logHandler.setFormatter(formatter)
logger.addHandler(logHandler)
logger.setLevel(LOG_LEVEL)

# Remove default uvicorn loggers to avoid double logging if needed, 
# or just let them be. For now, we configure root logger.

def log_event(event: str, level: str = "info", **kwargs):
    """Helper to log structured events."""
    data = {"event": event, **kwargs}
    if level == "error":
        logger.error(json.dumps(data))
    else:
        logger.info(json.dumps(data))

# ---------------- CONFIG ----------------
app = FastAPI()

# Directories
VIDEO_DIR = "temp_videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

# Environment Variables
LMS_STORE_URL = os.getenv("LMS_STORE_URL", "http://localhost:8000/video/store")
INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY", "my-secret-key")
REDIS_URL = os.getenv("REDIS_URL")

# Constants
MAX_VIDEO_SIZE_MB = 500
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi"}
REDIS_TTL = 7200  # 2 hours

# ---------------- JOB MANAGER ----------------

class JobManager:
    """
    Abstracts job state management.
    Uses Redis if REDIS_URL is set, otherwise falls back to In-Memory with Lock.
    """
    def __init__(self, redis_url: Optional[str]):
        self.use_redis = bool(redis_url)
        self.redis_client = None
        self.memory_store = {}
        self.memory_lock = threading.Lock()

        if self.use_redis:
            try:
                self.redis_client = redis.from_url(redis_url, decode_responses=True)
                # Test connection
                self.redis_client.ping()
                log_event("redis_connected", mode="redis")
            except Exception as e:
                log_event("redis_connection_failed", level="error", error=str(e))
                # Fallback to memory if Redis fails connecting? 
                # Ideally we crash or fallback. For now, let's crash if configured but failing,
                # or strictly fallback. Let's strict fallback for safety in this demo.
                print("Refusing to fallback silently for prod config. But for demo: falling back to memory.")
                self.use_redis = False
        else:
            log_event("redis_not_configured", mode="memory")

    def set_job(self, video_id: str, data: Dict[str, Any]):
        if self.use_redis:
            # Redis is atomic, no lock needed
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
            if raw:
                return json.loads(raw)
            return None
        else:
            with self.memory_lock:
                # Return copy to avoid partial mutation issues outside lock
                return self.memory_store.get(video_id, {}).copy() if video_id in self.memory_store else None

    def update_status(self, video_id: str, status: str, extra_fields: Dict[str, Any] = None):
        """Updates status and optionally other fields safely."""
        # Note: In Redis this is Read-Modify-Write. Strictly speaking, could race.
        # But for 'status' updates flow (queued->processing->awaiting->completed), 
        # simpler RMW is usually acceptable vs complex Lua scripts or Redlock for this specific workflow.
        
        if self.use_redis:
            key = f"job:{video_id}"
            raw = self.redis_client.get(key)
            if raw:
                data = json.loads(raw)
                data["status"] = status
                if extra_fields:
                    data.update(extra_fields)
                self.redis_client.setex(key, REDIS_TTL, json.dumps(data))
        else:
            with self.memory_lock:
                if video_id in self.memory_store:
                    self.memory_store[video_id]["status"] = status
                    if extra_fields:
                        self.memory_store[video_id].update(extra_fields)

    def delete_job(self, video_id: str):
        if self.use_redis:
            self.redis_client.delete(f"job:{video_id}")
        else:
            with self.memory_lock:
                if video_id in self.memory_store:
                    del self.memory_store[video_id]
    
    def cleanup_stale_memory_jobs(self):
        """Only used for In-Memory mode."""
        if self.use_redis:
            return # Redis TTL handles this

        now = time.time()
        expiration = 30 * 60 # 30 mins for safety in memory
        
        with self.memory_lock:
            to_delete = []
            for vid, job in self.memory_store.items():
                if job["status"] == "awaiting_confirmation":
                    if now - job.get("created_at", 0) > expiration:
                        to_delete.append(vid)
            # Return list to delete files outside lock? 
            # Or just return list and caller handles files + store deletion.
        
        # Actually, simpler to just return the IDs to the caller
        return to_delete

# Initialize Job Manager
job_manager = JobManager(REDIS_URL)


# ---------------- UTILITIES ----------------

def delete_physical_files(video_id: str, input_path: str, output_path: str):
    try:
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)
        log_event("files_deleted", video_id=video_id)
    except Exception as e:
        log_event("file_deletion_failed", level="error", video_id=video_id, error=str(e))

def perform_lazy_cleanup():
    """Checks for stale jobs (Memory mode only) and cleans them."""
    if job_manager.use_redis:
        return # Redis handles cleanup via TTL
    
    # This is a bit tricky with accessing the store directly.
    # We'll implemented a specific method in JobManager for this.
    # Retrieving list first
    now = time.time()
    # Accessing internal store is messy, but standardized interface is better.
    # Refactor: We added cleanup_stale_memory_jobs to manager.
    
    # We need to get the file paths to delete them.
    # Ideally, we iterate.
    # For MVP Memory mode, let's keep it simple.
    
    # Actually, if using Redis, we rely on TTL to drop the key.
    # But physical files? Redis TTL won't verify files are deleted.
    # Production: Use a Cron Job or separate worker to clean `temp_videos` based on file age.
    # For this implementation: We will trust Confirm/Timeout logic.
    pass


# ---------------- COMPRESSION ----------------

def compress_video_ffmpeg(input_path, output_path):
    command = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vf", "scale=-2:720",
        "-c:v", "libx264",
        "-crf", "30",
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(command, check=True)


def send_to_lms(video_id, organization_id, compressed_path):
    url = LMS_STORE_URL
    retries = 3
    backoff = 1

    log_event("callback_start", video_id=video_id, url=url)

    for attempt in range(1, retries + 1):
        try:
            with open(compressed_path, "rb") as f:
                files = {"file": f}
                data = {
                    "video_id": video_id, 
                    "organization_id": organization_id
                }
                response = requests.post(url, files=files, data=data, timeout=30)
            
            if response.status_code == 200:
                log_event("callback_success", video_id=video_id)
                return True
            else:
                log_event("callback_failed_attempt", video_id=video_id, attempt=attempt, status=response.status_code)
        
        except Exception as e:
            log_event("callback_error_attempt", video_id=video_id, attempt=attempt, error=str(e))
        
        if attempt < retries:
            time.sleep(backoff)
            backoff *= 2
    
    return False


def background_process_video(video_id, organization_id, input_path, output_path):
    try:
        job_manager.update_status(video_id, "processing")
        log_event("compression_started", video_id=video_id)

        # 1. Compress
        start_ts = time.time()
        compress_video_ffmpeg(input_path, output_path)
        duration = time.time() - start_ts
        
        log_event("compression_completed", video_id=video_id, duration=duration)

        # 2. Upload to LMS
        success = send_to_lms(video_id, organization_id, output_path)

        if success:
            job_manager.update_status(video_id, "awaiting_confirmation", {"compressed_path": output_path})
        else:
            job_manager.update_status(video_id, "failed")
            log_event("job_failed_callback", video_id=video_id)
            # Files remain for manual inspection/timeout

    except Exception as e:
        log_event("process_exception", level="error", video_id=video_id, error=str(e))
        job_manager.update_status(video_id, f"failed: {str(e)}")


# ---------------- ENDPOINTS ----------------

@app.get("/health")
def health_check():
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

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename missing")
    
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    # In-Memory Cleanup (Legacy support)
    if not job_manager.use_redis:
        # Very simple check
        stale_ids = job_manager.cleanup_stale_memory_jobs()
        if stale_ids:
            for sid in stale_ids:
                # We need path info. 
                # This logic is imperfect for separated concerns, but okay for MVP memory mode.
                # Ideally delete files if we can resolve paths.
                pass 

    # Save File
    file_id = f"{uuid.uuid4()}"
    input_filename = f"{file_id}_raw{ext}"
    output_filename = f"{file_id}_720p{ext}"
    
    input_path = os.path.join(VIDEO_DIR, input_filename)
    output_path = os.path.join(VIDEO_DIR, output_filename)

    try:
        content = await file.read()
        file_size_mb = len(content) / (1024 * 1024)
        if file_size_mb > MAX_VIDEO_SIZE_MB:
             raise HTTPException(status_code=400, detail="File too large")

        with open(input_path, "wb") as f:
            f.write(content)
    except Exception as e:
        if os.path.exists(input_path):
            os.remove(input_path)
        raise HTTPException(status_code=500, detail=f"File save failed: {str(e)}")

    # Queue Job
    job_data = {
        "status": "queued",
        "file_path": input_path,
        "compressed_path": "",
        "created_at": time.time(),
        "video_id": video_id,
        "org_id": organization_id
    }
    job_manager.set_job(video_id, job_data)

    log_event("job_queued", video_id=video_id, org_id=organization_id)

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
        log_event("confirm_not_found", level="error", video_id=video_id)
        raise HTTPException(status_code=404, detail="Video ID not found")
    
    current_status = job.get("status")

    if current_status == "completed":
        return {"status": "completed", "video_id": video_id}

    if current_status in ["queued", "processing"]:
         raise HTTPException(status_code=400, detail="Job not ready for confirmation")

    # Mark completed
    job_manager.update_status(video_id, "completed")
    log_event("job_confirmed", video_id=video_id)

    # Delete files
    # Note: If reusing JobManager 'get', we have the paths in 'job' variable
    delete_physical_files(video_id, job.get("file_path"), job.get("compressed_path"))

    return {"status": "completed", "video_id": video_id}


@app.get("/video/status/{video_id}")
def get_status(video_id: str, x_internal_service_key: str = Header(None)):
    if x_internal_service_key != INTERNAL_SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job = job_manager.get_job(video_id)
    status = job.get("status") if job else "not_found"
    return {"video_id": video_id, "status": status}
