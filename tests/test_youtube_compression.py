
import requests
import subprocess
import time
import os
import signal
import sys
import threading
import glob
from uvicorn import Server, Config
from fastapi import FastAPI, UploadFile, File, Form, Request

# ---------------- CONFIG ----------------
SERVICE_PORT = 8001
LMS_PORT = 8003 # Different port to avoid conflict

SERVICE_URL = f"http://127.0.0.1:{SERVICE_PORT}"
LMS_URL = f"http://127.0.0.1:{LMS_PORT}"
LMS_STORE_ENDPOINT = f"{LMS_URL}/video/store"

TEST_KEY = "my-secret-key"
YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw" # "Me at the zoo" (18s)
DOWNLOAD_DIR = "downloaded_videos"
VIDEO_ID = "youtube_test_001"
ORG_ID = "org_yt_001"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------- MOCK LMS ----------------
lms_app = FastAPI()
store_events = []

@lms_app.post("/video/store")
async def lms_store(
    request: Request,
    video_id: str = Form(...),
    organization_id: str = Form(...),
    file: UploadFile = File(...)
):
    print(f"[MOCK_LMS] Received file for {video_id}")
    content = await file.read()
    size = len(content)
    print(f"[MOCK_LMS] File received. Size: {size} bytes")
    
    store_events.append({
        "video_id": video_id,
        "size": size
    })
    return {"status": "stored"}

def run_lms_server():
    server = Server(Config(lms_app, host="127.0.0.1", port=LMS_PORT, log_level="warning"))
    server.run()

# ---------------- HELPERS ----------------

def download_youtube_video(url, output_dir):
    print(f"Downloading {url}...")
    template = os.path.join(output_dir, "%(id)s.%(ext)s")
    command = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", template,
        url
    ]
    subprocess.run(command, check=True)
    
    # Find the downloaded file
    files = glob.glob(os.path.join(output_dir, "*"))
    # Return the most recent file
    return max(files, key=os.path.getctime)

# ---------------- TEST LOGIC ----------------

def run_test():
    # 1. Download Video
    try:
        video_path = download_youtube_video(YOUTUBE_URL, DOWNLOAD_DIR)
        print(f"Downloaded video: {video_path}")
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        print(f"File Size: {file_size_mb:.2f} MB")
    except Exception as e:
        print(f"Failed to download video: {e}")
        return

    # 2. Start Mock LMS
    print("--- Starting Mock LMS ---")
    lms_thread = threading.Thread(target=run_lms_server, daemon=True)
    lms_thread.start()
    time.sleep(2)

    # 3. Start Compression Service
    print("--- Starting Compression Service ---")
    env = os.environ.copy()
    env["LMS_STORE_URL"] = LMS_STORE_ENDPOINT
    env["INTERNAL_SERVICE_KEY"] = TEST_KEY
    
    server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(SERVICE_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
        env=env
    )
    
    time.sleep(3)

    try:
        # 4. Upload to Service
        print("\n--- Uploading Video ---")
        start_time = time.time()
        with open(video_path, "rb") as f:
            resp = requests.post(
                f"{SERVICE_URL}/video/receive",
                files={"file": f},
                data={"video_id": VIDEO_ID, "organization_id": ORG_ID},
                headers={"X-Internal-Service-Key": TEST_KEY}
            )
        
        print(f"Receive Status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"Response: {resp.text}")
            return

        # 5. Poll for Completion (Callback)
        print("\n--- Waiting for Compression ---")
        max_wait = 60 # YouTube video might take longer
        start_wait = time.time()
        
        while len(store_events) == 0:
            if time.time() - start_wait > max_wait:
                print("❌ Timeout waiting for compression/callback")
                return
            
            # Check internal status
            try:
                status_resp = requests.get(
                     f"{SERVICE_URL}/video/status/{VIDEO_ID}",
                     headers={"X-Internal-Service-Key": TEST_KEY}
                )
                print(f"Status: {status_resp.json().get('status')}")
            except:
                pass
                
            time.sleep(2)
        
        duration = time.time() - start_wait
        print(f"✔ Compression & Callback completed in {duration:.2f}s")
        
        # 6. Verify Results
        original_size = os.path.getsize(video_path)
        compressed_size = store_events[0]["size"]
        
        print(f"\n--- Metrics ---")
        print(f"Original Size: {original_size / 1024:.2f} KB")
        print(f"Compressed Size: {compressed_size / 1024:.2f} KB")
        ratio = (1 - (compressed_size / original_size)) * 100
        print(f"Reduction: {ratio:.2f}%")
        
        # 7. Confirm & Cleanup
        print("\n--- Confirming ---")
        requests.post(
            f"{SERVICE_URL}/video/confirm",
            data={"video_id": VIDEO_ID},
            headers={"X-Internal-Service-Key": TEST_KEY}
        )
        
        # Verify status is completed
        status_resp = requests.get(
             f"{SERVICE_URL}/video/status/{VIDEO_ID}",
             headers={"X-Internal-Service-Key": TEST_KEY}
        )
        print(f"Final Status: {status_resp.json().get('status')}")

    except Exception as e:
        print(f"TEST FAILED: {e}")
    finally:
        print("Stopping service...")
        server_process.terminate()
        outs, errs = server_process.communicate()
        # print(outs) # Detailed logs if needed

if __name__ == "__main__":
    run_test()
