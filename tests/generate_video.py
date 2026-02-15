import subprocess
import os

def generate_video(filename="test_video_1080p.mp4"):
    # Generate 5 seconds of 1080p video
    command = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", "testsrc=duration=5:size=1920x1080:rate=30",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        filename
    ]
    subprocess.run(command, check=True)
    print(f"Generated {filename}")

if __name__ == "__main__":
    generate_video()
