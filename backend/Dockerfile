FROM python:3.11-slim

# System dependencies: FFmpeg for recording/trimming/audio injection
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app/ app/
COPY audio_replies/ audio_replies/

# Storage directories (will be bind-mounted in production)
RUN mkdir -p /mnt/opslag/deurbel/clips /mnt/opslag/deurbel/snapshots

EXPOSE 8090

# Run with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", "info"]
