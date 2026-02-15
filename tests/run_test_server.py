
import uvicorn
import os
import sys
# Add current directory to path
sys.path.append(os.getcwd())

from app import app
import app as app_module
from unittest.mock import MagicMock
import time
import requests

# Mock SFTP to avoid failure and simulate success
# We also add a small delay to simulate network latency if needed.
def mock_upload(local, remote):
    if os.path.exists(local):
        size = os.path.getsize(local)
        print(f"[METRICS] Compressed size: {size} bytes")
        
        # Get resolution
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", 
                 "-show_entries", "stream=height", "-of", "csv=s=x:p=0", local],
                capture_output=True, text=True
            )
            height = probe.stdout.strip()
            print(f"[METRICS] Compressed height: {height}")
        except Exception as e:
            print(f"[METRICS] Error probing resolution: {e}")
    time.sleep(2) # Simulate upload time

app_module.upload_to_sftp = mock_upload

# Mock remove to verify cleanup calls
original_remove = os.remove
def mock_remove(path):
    print(f"[CLEANUP] Removing file: {path}")
    original_remove(path)

app_module.os.remove = mock_remove

# Mock requests.post for confirmation call
app_module.CLIENT_CONFIRM_API = "http://mock-confirm"
original_post = requests.post

def mock_post(url, *args, **kwargs):
    print(f"[MOCK_POST] Calling {url}")
    if url == app_module.CLIENT_CONFIRM_API:
        print("[MOCK_POST] Simulating 200 OK for confirmation")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        return mock_resp
    return original_post(url, *args, **kwargs)

app_module.requests.post = mock_post

if __name__ == "__main__":
    # Run on a different port to avoid conflicts
    uvicorn.run(app, host="127.0.0.1", port=8001)
