# -----------------------------
# Base Image
# -----------------------------
FROM python:3.11-slim

# -----------------------------
# System Dependencies
# -----------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# -----------------------------
# Environment
# -----------------------------
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# -----------------------------
# Working Directory
# -----------------------------
WORKDIR /app

# -----------------------------
# Install Python Dependencies
# -----------------------------
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# -----------------------------
# Copy Application Code
# -----------------------------
COPY . .

# -----------------------------
# Security: Run as non-root user
# -----------------------------
RUN useradd -m appuser
USER appuser

# -----------------------------
# Start Server
# Cloud Run automatically injects $PORT
# -----------------------------
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
