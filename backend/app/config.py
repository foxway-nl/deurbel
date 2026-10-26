"""
Foxway Smart Doorbell — Configuration.

All settings are dynamically discovered or loaded from environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── go2rtc media engine (running locally on host) ───────────────────
    GO2RTC_API_URL: str = "http://127.0.0.1:1984"
    GO2RTC_RTSP_URL: str = "rtsp://127.0.0.1:8554/deurbel"
    GO2RTC_STREAM_NAME: str = "deurbel"

    # ── Doorbell Hardware ──────────────────────────────────────────────
    DOORBELL_IP: str = ""
    DOORBELL_SDK_PORT: int = 8000
    DOORBELL_HTTPS_PORT: int = 8443
    DOORBELL_USER: str = "admin"
    DOORBELL_PASS: str = ""
    DOORBELL_AUDIO_CODEC: str = "pcm_alaw"

    # ── Storage paths ──────────────────────────────────────────────────
    STORAGE_PATH: Path = Path("/mnt/opslag/deurbel")
    CLIPS_DIR: Path = Path("/mnt/opslag/deurbel/clips")
    SNAPSHOTS_DIR: Path = Path("/mnt/opslag/deurbel/snapshots")
    AUDIO_REPLIES_DIR: Path = (
        Path("/mnt/opslag/deurbel/backend/audio_replies")
        if Path("/mnt/opslag/deurbel/backend/audio_replies").exists()
        else Path("/app/audio_replies")
    )
    DB_PATH: Path = Path("/mnt/opslag/deurbel/foxway.db")

    # ── Auth / JWT ─────────────────────────────────────────────────────
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"

    # ── Firebase Cloud Messaging ───────────────────────────────────────
    FCM_CREDENTIALS_PATH: str = ""  # path to service-account JSON; empty = disabled

    # ── Networking (Resolved dynamically at runtime) ──────────────────
    LAN_SUBNET: str = ""
    LAN_URL: str = ""
    WAN_URL: str = "https://deurbel.foxway.nl"
    GO2RTC_LAN_WS: str = ""
    GO2RTC_WAN_WS: str = ""
    HOST_PORT: int = 8090

    # ── Call behaviour ─────────────────────────────────────────────────
    RING_COOLDOWN_SECONDS: int = 5
    RING_TIMEOUT_SECONDS: int = 35
    RECORDING_DURATION_SECONDS: int = 60
    POST_HANGUP_RECORD_SECONDS: int = 5

    # ── Clip retention ─────────────────────────────────────────────────
    CLIP_RETENTION_DAYS: int = 30

    class Config:
        env_prefix = "FOXWAY_"
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

# Ensure storage directories exist (no-op inside container if mounted)
settings.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
settings.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
