import os
import uuid
import subprocess
import paramiko
import requests
import time
import logging

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks

app = FastAPI()

# ---------------- DIRECTORIES ----------------
VIDEO_DIR = "temp_videos"
IMAGE_DIR = "temp_images"

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)

# ---------------- CONFIG ----------------
SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USERNAME = os.getenv("SFTP_USERNAME")
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD")
CLIENT_CONFIRM_API = os.getenv("CLIENT_CONFIRM_API")

# ---------------- JOB STORE ----------------
JOB_STATUS = {}

# ------------------------------------------------
# ---------------- UTILITIES ---------------------
# ------------------------------------------------

def cleanup_old_files(directory, max_age_minutes=20):
    if not os.path.exists(directory):
        return

    now = time.time()
    max_age_seconds = max_age_minutes * 60

    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        if os.path.isfile(file_path):
            try:
                file_age = now - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    os.remove(file_path)
                    logging.info(f"Deleted old file: {file_path}")
            except Exception as e:
                logging.warning(f"Cleanup failed for {file_path}: {str(e)}")


def compress_video(input_path, output_path):
    command = [
        "ffmpeg",
        "-i", input_path,
        "-vf", "scale=-2:720",
        "-c:v", "libx264",
        "-crf", "30",
        "-preset", "slow",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(command, check=True)


def compress_image(input_path, output_path):
    command = [
        "ffmpeg",
        "-i", input_path,
        "-q:v", "10",
        output_path
    ]
    subprocess.run(command, check=True)


def upload_to_sftp(local_file_path, remote_file_path):
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)

    sftp = paramiko.SFTPClient.from_transport(transport)
    sftp.put(local_file_path, remote_file_path)

    sftp.close()
    transport.close()


# ------------------------------------------------
# ------------ BACKGROUND PROCESS ---------------
# ------------------------------------------------

def process_media(input_path, output_path, media_type, media_id, remote_path):
    try:
        JOB_STATUS[media_id] = "processing"

        if media_type == "video":
            compress_video(input_path, output_path)
        else:
            compress_image(input_path, output_path)

        upload_to_sftp(output_path, remote_path)

        response = requests.post(
            CLIENT_CONFIRM_API,
            json={
                "mediaId": media_id,
                "filePath": remote_path
            },
            timeout=15
        )

        if response.status_code != 200:
            JOB_STATUS[media_id] = "db_update_failed"
            return

        os.remove(input_path)
        os.remove(output_path)

        JOB_STATUS[media_id] = "completed"

    except Exception as e:
        JOB_STATUS[media_id] = f"failed: {str(e)}"


# ------------------------------------------------
# ----------------- ENDPOINT --------------------
# ------------------------------------------------

@app.post("/compress/")
async def compress_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    media_id: str = Form(...)
):
    # Cleanup old leftovers
    cleanup_old_files(VIDEO_DIR)
    cleanup_old_files(IMAGE_DIR)

    file_id = str(uuid.uuid4())

    if file.content_type.startswith("video"):
        input_path = os.path.join(VIDEO_DIR, file_id + "_" + file.filename)
        output_path = os.path.join(VIDEO_DIR, "compressed_" + file.filename)
        remote_path = f"/public_html/videos/{file.filename}"
        media_type = "video"

    elif file.content_type.startswith("image"):
        input_path = os.path.join(IMAGE_DIR, file_id + "_" + file.filename)
        output_path = os.path.join(IMAGE_DIR, "compressed_" + file.filename)
        remote_path = f"/public_html/images/{file.filename}"
        media_type = "image"

    else:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    JOB_STATUS[media_id] = "queued"

    background_tasks.add_task(
        process_media,
        input_path,
        output_path,
        media_type,
        media_id,
        remote_path
    )

    return {
        "status": "accepted",
        "mediaId": media_id,
        "message": "Compression started"
    }


# ------------------------------------------------
# ---------------- STATUS API -------------------
# ------------------------------------------------

@app.get("/status/{media_id}")
def get_status(media_id: str):
    return {
        "mediaId": media_id,
        "status": JOB_STATUS.get(media_id, "not_found")
    }
