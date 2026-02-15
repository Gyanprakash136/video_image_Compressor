# Media Compression Service

## Overview

The Media Compression Service is a stateless backend microservice responsible for:

- Video compression to 720p using FFmpeg
- Image compression using FFmpeg
- Background asynchronous processing
- Secure delivery of compressed files to client storage
- Job status tracking
- Temporary file lifecycle management

This service is designed to operate as a backend-to-backend component within the LMS architecture.
It must not be accessed directly by frontend clients.

---

## Architecture

### Logical Flow

1. LMS backend creates a media record in the database.
2. LMS backend generates a unique `media_id`.
3. LMS backend sends file and `media_id` to the Compression Service.
4. Compression Service:
   - Validates file
   - Queues background job
   - Compresses media
   - Uploads compressed file to client storage (SFTP)
   - Calls LMS confirmation API
5. LMS backend updates the database.
6. Compression Service deletes temporary files.

---

## Base URL

Production URL:
`https://<cloud-run-service-url>`

Example:
`https://compression-service-xxxxx.a.run.app`

---

## Authentication

All requests must include the following HTTP header:

```
X-API-Key: <secret-key>
```

Requests without a valid API key will receive:

`401 Unauthorized`

The API key must be stored securely in backend environment variables and never exposed to frontend clients.

---

## API Endpoints

### 1. Compress Media

**Endpoint**

`POST /compress/`

**Headers**

```
X-API-Key: <secret-key>
Content-Type: multipart/form-data
```

**Body (Form Data)**

| Field     | Type   | Required | Description                     |
|-----------|--------|----------|---------------------------------|
| file      | File   | Yes      | Video or image file             |
| media_id  | String | Yes      | Unique ID from LMS database     |

#### Response (Immediate)

`200 OK`

```json
{
  "status": "accepted",
  "mediaId": "abc123",
  "message": "Compression started"
}
```

This endpoint is non-blocking. Compression occurs asynchronously.

#### Possible Errors

| Status Code | Description               |
| ----------- | ------------------------- |
| 400         | Unsupported file type     |
| 400         | File size exceeds limit   |
| 401         | Unauthorized              |
| 500         | Internal processing error |

---

### 2. Job Status

**Endpoint**

`GET /status/{media_id}`

**Headers**

```
X-API-Key: <secret-key>
```

**Response**

```json
{
  "mediaId": "abc123",
  "status": "processing"
}
```

#### Possible Status Values

| Status           | Meaning                 |
| ---------------- | ----------------------- |
| queued           | Job accepted, waiting   |
| processing       | Compression running     |
| completed        | Success                 |
| db_update_failed | LMS confirmation failed |
| failed: <reason> | Error occurred          |
| not_found        | Invalid media_id        |

---

## Supported File Types

### Video

Supported formats:
* `.mp4`
* `.mov`
* `.mkv`

Maximum size: 500 MB

Output format:
* 720p resolution
* H.264 (libx264)
* AAC audio (128k)

### Image

Supported formats:
* `.jpg`
* `.jpeg`
* `.png`
* `.webp`

Maximum size: 20 MB

---

## File Naming

All internal and remote filenames are generated using UUIDs to prevent collisions.
Original filenames are not preserved.

---

## LMS Confirmation Contract

After successful compression and SFTP upload, the service calls:

`POST <CLIENT_CONFIRM_API>`

Payload:

```json
{
  "mediaId": "abc123",
  "filePath": "/public_html/videos/<generated_filename>.mp4"
}
A scalable, reliable microservice for compressing videos and images before storage. This service implements a **3-Step Handshake Workflow** to ensure data integrity and supports production-grade scaling via Redis.

## 🚀 Key Features
- **3-Step Handshake**: `Receive` -> `Store (Callback)` -> `Confirm`.
- **Stateless & Scalable**: Uses Redis for job state management (supports Cloud Run horizontal scaling).
- **Resilient**: Implements retry logic with exponential backoff for callbacks.
- **Fail-Safe**: Auto-cleans stale jobs if confirmation is not received (TTL).
- **Observability**: Structured JSON logging for Cloud Logging integration.

---

## 🛠 Integration Guide for Backend Team

### Workflow Overview

1.  **Backend** uploads raw video to `POST /video/receive`.
2.  **Service** returns `200 OK` immediately (queued).
3.  **Service** processes video and pushes result to your `LMS_STORE_URL`.
4.  **Backend** verifies file receipt and calls `POST /video/confirm`.
5.  **Service** deletes temporary files.

### API Reference

#### 1. Receive Video
Upload a video file to start the compression job.

- **Endpoint**: `POST /video/receive`
- **Headers**: `X-Internal-Service-Key: <your-secret-key>`
- **Content-Type**: `multipart/form-data`

| Form Field | Type | Description |
| :--- | :--- | :--- |
| `video_id` | String | Unique ID for the video (used for tracking). |
| `organization_id` | String | Org ID for context/logging. |
| `file` | File | The video file (mp4, mov, mkv, avi). |

**Response (Success)**:
```json
{
  "status": "queued",
  "video_id": "12345"
}
```

#### 2. Store Callback (Webhook)
The service calls *your* endpoint when compression is done.
**You must implement this endpoint.**

- **Endpoint**: `POST <LMS_STORE_URL>` (Configurable)
- **Content-Type**: `multipart/form-data`

| Form Field | Type | Description |
| :--- | :--- | :--- |
| `video_id` | String | The ID you sent in Step 1. |
| `organization_id` | String | The Org ID you sent in Step 1. |
| `file` | File | The compressed video file (720p). |

**Expected Response**: `200 OK` (Any other status triggers retry).

#### 3. Confirm & Cleanup
Call this *after* you have successfully stored the compressed video.

- **Endpoint**: `POST /video/confirm`
- **Headers**: `X-Internal-Service-Key: <your-secret-key>`
- **Content-Type**: `application/x-www-form-urlencoded`

| Form Field | Type | Description |
| :--- | :--- | :--- |
| `video_id` | String | ID of the job to confirm. |

**Response**:
```json
{
  "status": "completed",
  "video_id": "12345"
}
```

---

## ⚙️ Configuration

Set these environment variables in your deployment (e.g., Cloud Run "Variables" tab).

| Variable | Description | Required | Default |
| :--- | :--- | :--- | :--- |
* Redis-backed job tracking
* Multi-resolution output
* HLS streaming support
* Structured logging and monitoring
* Private service-to-service authentication
