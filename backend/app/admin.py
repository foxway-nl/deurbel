"""
Foxway Smart Doorbell — Web Admin System & Diagnostics Router (/admin).

Provides:
  1. Secure authentication (/admin/login) with signed HTTP-only session cookie.
  2. Tab 1: Live 4:3 Camera Stream, Frame Latency, FPS, CPU/RAM/Disk stats, 1-Click Operations.
  3. Tab 2: Dynamic System & AI Variables (/api/v1/admin/settings) + Interactive Exclusion Zones.
  4. Tab 3: AI Verification Inspector (/api/v1/admin/detections) with bounding boxes and reasons.
  5. Tab 4: Backend Ring Log & Android Client Crash Logger (/api/v1/logs/client-crash).
"""

from __future__ import annotations

import collections
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt, JWTError
from pydantic import BaseModel

from app.config import settings
from app.models import (
    get_system_settings,
    get_system_setting,
    update_system_settings,
    get_recent_ai_audit_logs,
    save_crash_log,
    get_recent_crash_logs,
    get_camera_credentials,
)

logger = logging.getLogger("foxway.admin")

router = APIRouter(tags=["Admin Panel"])

# ── 1. In-Memory Rotating Ring Logger for Backend Errors ─────────────

class InMemoryRingLogHandler(logging.Handler):
    """Stores the latest 100 WARNING/ERROR log messages in memory."""
    def __init__(self, capacity: int = 100):
        super().__init__(level=logging.WARNING)
        self.buffer = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.buffer.append({
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name.replace("foxway.", ""),
                "message": record.getMessage(),
            })
        except Exception:
            pass

ring_log_handler = InMemoryRingLogHandler(capacity=100)
ring_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(ring_log_handler)


# ── 2. Session Authentication Helpers ─────────────────────────────────

SESSION_COOKIE_NAME = "foxway_admin_session"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_session_token(username: str = "admin") -> str:
    """Generate a signed JWT session token for the admin cookie."""
    payload = {
        "sub": username,
        "role": "admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + (86400 * 7),  # 7 days
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_admin_session(request: Request) -> bool:
    """Verify session cookie or Authorization Bearer header."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    if not token:
        return False
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload.get("sub") == "admin" or payload.get("role") == "admin"
    except JWTError:
        return False


def require_admin(request: Request):
    """FastAPI dependency to protect admin API endpoints."""
    if not verify_admin_session(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Toegang geweigerd: Geldige admin sessie vereist",
        )


# ── 3. Web UI HTML Routes ─────────────────────────────────────────────

@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """Serves the login page, or redirects to /admin if already logged in."""
    if verify_admin_session(request):
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    login_html = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(content=login_html.replace("{% if error %}", "").replace("{% endif %}", "").replace("{{ error }}", ""))


@router.post("/admin/login")
async def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Handles admin login submission, verifies password against DB setting."""
    configured_password = await get_system_setting("admin_password", "foxway2024")
    
    if username.strip() == "admin" and password.strip() == configured_password.strip():
        token = create_session_token("admin")
        resp = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=86400 * 7,
            httponly=True,
            samesite="lax",
            secure=False,  # Allow on local LAN HTTP (192.168.2.1:8090)
        )
        logger.info("Admin login SUCCESS from %s", request.client.host if request.client else "unknown")
        return resp

    logger.warning("Admin login FAILED from %s (invalid credentials)", request.client.host if request.client else "unknown")
    login_html = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
    error_msg = '<div class="p-3.5 rounded-xl bg-red-500/10 border border-red-500/30 text-red-400 text-sm">Ongeldige gebruikersnaam of wachtwoord.</div>'
    rendered = login_html.replace("{% if error %}", error_msg).replace("{% endif %}", "").replace("{{ error }}", "")
    return HTMLResponse(content=rendered, status_code=status.HTTP_401_UNAUTHORIZED)


@router.get("/admin/logout")
async def admin_logout():
    """Clears the session cookie and redirects to login."""
    resp = RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(key=SESSION_COOKIE_NAME)
    return resp


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_page(request: Request):
    """Serves the main dark-mode Admin Dashboard."""
    if not verify_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    admin_html = (TEMPLATES_DIR / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(content=admin_html)


# ── 4. Admin API Endpoints ────────────────────────────────────────────

@router.get("/api/v1/admin/settings")
async def get_admin_settings_endpoint(request: Request):
    """Returns all current system settings from SQLite."""
    require_admin(request)
    settings_dict = await get_system_settings()
    # Mask password for security
    if "admin_password" in settings_dict:
        settings_dict["admin_password"] = "••••••••"
    return settings_dict


@router.put("/api/v1/admin/settings")
async def update_admin_settings_endpoint(request: Request, body: dict):
    """Batch updates system settings and triggers live parameter updates."""
    require_admin(request)
    # Filter out masked password if not changed
    clean_updates = {}
    for k, v in body.items():
        if k == "admin_password" and v == "••••••••":
            continue
        clean_updates[k] = str(v)

    await update_system_settings(clean_updates)
    logger.info("Admin updated system settings: %s", list(clean_updates.keys()))
    return {"status": "ok", "message": f"Succesvol {len(clean_updates)} instellingen bijgewerkt"}


@router.get("/api/v1/admin/detections")
async def get_admin_detections_endpoint(request: Request):
    """Returns the last 50 AI audit logs for the Verification Inspector."""
    require_admin(request)
    logs = await get_recent_ai_audit_logs(limit=50)
    return logs


@router.get("/api/v1/admin/system-status")
async def get_admin_system_status(request: Request):
    """Returns live hardware metrics (CPU, RAM, Disk) and component health."""
    require_admin(request)
    
    # 1. CPU & RAM via psutil
    cpu_pct = 0.0
    ram_pct = 0.0
    ram_used_mb = 0
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0.05)
        mem = psutil.virtual_memory()
        ram_pct = mem.percent
        ram_used_mb = int(mem.used / (1024 * 1024))
    except Exception:
        pass

    # 2. Disk Storage via recorder stats
    from app.recorder import get_storage_stats
    retention_days = int(await get_system_setting("retention_days", "30"))
    max_storage_gb = float(await get_system_setting("max_storage_gb", "10.0"))
    storage_stats = get_storage_stats(max_storage_gb)

    # 3. LiveHub Frame Age in ms
    from app.live_hub import live_hub
    frame_age_ms = 0
    if live_hub.latest_frame_ts > 0:
        frame_age_ms = int((time.time() - live_hub.latest_frame_ts) * 1000)

    creds = await get_camera_credentials()

    return {
        "status": "ok",
        "cpu_percent": cpu_pct,
        "ram_percent": ram_pct,
        "ram_used_mb": ram_used_mb,
        "disk_used_mb": storage_stats["used_mb"],
        "disk_max_gb": max_storage_gb,
        "disk_usage_percent": storage_stats["usage_percent"],
        "total_clips": storage_stats["total_clips"],
        "frame_age_ms": frame_age_ms,
        "live_fresh": live_hub.is_fresh(),
        "doorbell_ip": creds.get("ip", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/v1/admin/resync-live")
async def admin_resync_live(request: Request):
    """Force restart LiveHub ffmpeg process to clear any potential stream drift."""
    require_admin(request)
    from app.live_hub import live_hub
    await live_hub.restart()
    return {"status": "ok", "message": "Live stream buffer gereset naar 0ms realtime"}


@router.post("/api/v1/admin/cleanup-false-clips")
async def admin_cleanup_false_clips(request: Request):
    """Purge today's false wind clips from disk and SQLite."""
    require_admin(request)
    from app.recorder import cleanup_today_false_clips
    result = await cleanup_today_false_clips()
    return result


@router.post("/api/v1/admin/test-ring")
async def admin_test_ring(request: Request):
    """Triggers an instantaneous test ring event across the state machine."""
    require_admin(request)
    from app.main import call_sm
    call = await call_sm.ring(force=True)
    return {
        "status": "ok",
        "call_id": call.call_id if call else "none",
        "message": "Test aanbel-oproep direct verzonden naar alle Android toestellen",
    }


@router.post("/api/v1/admin/test-notification")
async def admin_test_notification(request: Request):
    """Pushes a test notification to all connected Android apps."""
    require_admin(request)
    test_data = {
        "event_id": int(time.time()),
        "clip_id": "test_notification_clip",
        "category": "person",
        "label": "Test Melding",
        "title": "🔔 Test Melding vanuit Web Admin",
        "confidence": 0.99,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "snapshot_url": "/api/v1/live/instant.jpg",
        "in_quiet_hours": False,
        "should_notify": True,
    }
    await push_event("AI_DETECTION", test_data)
    return {"status": "ok", "message": "Test push-notificatie verzonden via WebSocket en FCM"}


@router.get("/api/v1/admin/crash-logs")
async def get_admin_crash_logs(request: Request):
    """Returns recent Android client crash reports and backend memory ring logs."""
    require_admin(request)
    client_crashes = await get_recent_crash_logs(limit=50)
    backend_logs = list(ring_log_handler.buffer)
    backend_logs.reverse()
    return {
        "status": "ok",
        "client_crashes": client_crashes,
        "backend_logs": backend_logs,
    }


# ── 5. Android Client Crash Ingestion Endpoint (Public) ──────────────

class ClientCrashReport(BaseModel):
    device_name: str = ""
    device_id: str = ""
    app_version: str = ""
    error_message: str
    stacktrace: str
    screen_state: str = ""


@router.post("/api/v1/logs/client-crash")
async def ingest_client_crash(body: ClientCrashReport):
    """
    Ingests Android client crash reports and persists them in crash_logs table.
    No authentication required so uncoupled/crashed devices can always report.
    """
    logger.error("ANDROID CRASH from %s (%s): %s", body.device_name, body.app_version, body.error_message)
    log_id = await save_crash_log(
        device_name=body.device_name,
        device_id=body.device_id,
        app_version=body.app_version,
        error_message=body.error_message,
        stacktrace=body.stacktrace,
        screen_state=body.screen_state,
    )
    return {"status": "ok", "log_id": log_id, "message": "Crash stacktrace gelogd"}
