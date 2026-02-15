
import requests
import subprocess
import time
import os
import signal
import sys

SERVER_URL = "http://127.0.0.1:8001"
VIDEO_FILE = "test_video_1080p.mp4"
MEDIA_ID = "test_video_001"

TEST_API_KEY = "secret-test-key"

def run_test():
    print("starting test server...")
    
    # Copy env and add API_KEY
    env = os.environ.copy()
    env["API_KEY"] = TEST_API_KEY
    
    server_process = subprocess.Popen(
        [sys.executable, "run_test_server.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
        env=env
    )
    
    # Wait for server to start
    print("Waiting for server to be ready...")
    time.sleep(3) # Give it a moment
    
    try:
        # Check original file stats
        orig_size = os.path.getsize(VIDEO_FILE)
        print(f"Original file size: {orig_size}")
        
        # 0. Test Unauthorized Access
        print("Testing unauthorized access...")
        unauth_res = requests.get(f"{SERVER_URL}/status/test_id")
        if unauth_res.status_code == 401:
            print("✔ Unauthorized access correctly blocked (401)")
        else:
            print(f"❌ FAILED: Unauthorized access NOT blocked! Status: {unauth_res.status_code}")
            return

        # 1. Upload file
        print(f"Uploading {VIDEO_FILE}...")
        start_req_time = time.time()
        
        headers = {"X-API-Key": TEST_API_KEY}
        
        with open(VIDEO_FILE, "rb") as f:
            response = requests.post(
                f"{SERVER_URL}/compress/",
                files={"file": f},
                data={"media_id": MEDIA_ID},
                headers=headers
            )
            
        req_duration = time.time() - start_req_time
        print(f"Request duration: {req_duration:.4f}s")
        
        if req_duration > 1.0:
            print("WARNING: API response took longer than 1s (Blocking?)")
        else:
            print("✔ API returned immediately (Non-blocking)")
            
        if response.status_code != 200:
            print(f"FAILED: Status code {response.status_code}")
            print(response.text)
            return

        print("Response:", response.json())
        assert response.json()["status"] == "accepted"
        
        # 2. Poll status
        print("Polling status...")
        status = "queued"
        start_poll_time = time.time()
        
        while status not in ["completed", "failed"] and (time.time() - start_poll_time < 30):
            res = requests.get(f"{SERVER_URL}/status/{MEDIA_ID}", headers=headers)
            data = res.json()
            new_status = data.get("status")
            if new_status != status:
                print(f"Status changed: {status} -> {new_status}")
                status = new_status
            time.sleep(0.5)
            
        print(f"Final Job Status: {status}")
        
        # 3. Analyze verify logs/metrics from server output
        # We need to read the server output. Since we are in the same script, 
        # checking stdout of a running process is tricky without blocking.
        # We will terminate the server and read what we captured.
        
    except Exception as e:
        print(f"Test Exception: {e}")
        
    finally:
        print("Stopping server...")
        server_process.terminate()
        try:
            outs, errs = server_process.communicate(timeout=5)
            print("\n--- SERVER LOGS (STDOUT) ---")
            print(outs)
            print("\n--- SERVER LOGS (STDERR) ---")
            print(errs)
            print("-------------------")
            
            # Parse logs for validation
            if "[METRICS] Compressed size:" in outs:
                print("✔ Metrics captured from server.")
            else:
                print("❌ Metrics MISSING in server logs.")
                
            if "[CLEANUP] Removing file:" in outs:
                print("✔ Cleanup verified.")
            else:
                print("❌ Cleanup NOT detected.")
                
            if "Compressed height: 720" in outs:
                print("✔ Resolution verified as 720p.")
            
        except subprocess.TimeoutExpired:
            server_process.kill()
            print("Server process killed forcefully.")

if __name__ == "__main__":
    run_test()
