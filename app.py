import os
import uuid
import subprocess
import mimetypes
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse

app = FastAPI()

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


def compress_video(input_path, output_path):
    command = [
        "ffmpeg",
        "-i", input_path,

        # Resize to 720p (keep aspect ratio)
        "-vf", "scale=-2:720",

        # Video codec
        "-c:v", "libx264",

        # Strong compression
        "-crf", "30",

        # Better compression efficiency
        "-preset", "slow",

        # Audio compression
        "-c:a", "aac",
        "-b:a", "128k",

        # Optimize for web playback
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


@app.post("/compress/")
async def compress_file(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    
    input_path = os.path.join(UPLOAD_DIR, file_id + "_" + file.filename)
    output_path = os.path.join(OUTPUT_DIR, "compressed_" + file.filename)

    # Save uploaded file
    with open(input_path, "wb") as f:
        f.write(await file.read())

    # Compress based on type
    if file.content_type.startswith("video"):
        compress_video(input_path, output_path)
    elif file.content_type.startswith("image"):
        compress_image(input_path, output_path)
    else:
        return {"error": "Unsupported file type"}

    # Detect MIME type automatically
    mime_type, _ = mimetypes.guess_type(output_path)

    return FileResponse(
        output_path,
        media_type=mime_type,
        filename=os.path.basename(output_path)
    )
