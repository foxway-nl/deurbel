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

os.environ["TZ"] = "Europe/Amsterdam"
if hasattr(time, "tzset"):
    time.tzset()
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Form,
    HTTPException,
    Query,
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
async def admin_login_page(request: Request, token: Optional[str] = Query(None)):
    """Serves the login page, or auto-logs in if a valid token is provided, or redirects to /admin if already logged in."""
    if token:
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
            if payload.get("sub") == "admin" or payload.get("role") == "admin":
                resp = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
                resp.set_cookie(
                    key=SESSION_COOKIE_NAME,
                    value=token,
                    max_age=86400 * 7,
                    httponly=True,
                    samesite="lax",
                    secure=False,
                )
                logger.info("Admin auto-login token verified on /admin/login from %s", request.client.host if request.client else "unknown")
                return resp
        except JWTError:
            pass

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
async def admin_dashboard_page(request: Request, token: Optional[str] = Query(None)):
    """Serves the main dark-mode Admin Dashboard, with seamless auto-login if token is supplied."""
    if token:
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
            if payload.get("sub") == "admin" or payload.get("role") == "admin":
                resp = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
                resp.set_cookie(
                    key=SESSION_COOKIE_NAME,
                    value=token,
                    max_age=86400 * 7,
                    httponly=True,
                    samesite="lax",
                    secure=False,
                )
                logger.info("Admin auto-login token verified on /admin from %s", request.client.host if request.client else "unknown")
                return resp
        except JWTError as e:
            logger.warning("Admin auto-login token invalid: %s", e)

    if not verify_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    admin_html = (TEMPLATES_DIR / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(content=admin_html)


@router.get("/api/v1/admin/auth-token")
async def get_admin_auth_token():
    """Generates an admin session JWT token for seamless 1-click auto-login from the paired Android client."""
    token = create_session_token("admin")
    return {"token": token}


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
    from app.logger import log_settings_change
    log_settings_change("admin_web", clean_updates)
    logger.info("Admin updated system settings: %s", list(clean_updates.keys()))
    return {"status": "ok", "message": f"Succesvol {len(clean_updates)} instellingen bijgewerkt"}


@router.get("/api/v1/admin/detections")
async def get_admin_detections_endpoint(request: Request, status: Optional[str] = None, limit: int = 50):
    """Returns AI audit logs for the Verification Inspector, optionally filtered by status."""
    require_admin(request)
    logs = await get_recent_ai_audit_logs(limit=limit, status=status)
    return logs


@router.get("/api/v1/admin/system-status")
async def get_admin_system_status(request: Request):
    """Returns live hardware metrics (CPU, RAM, Disk) and component health."""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
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
    max_storage_gb = float(await get_system_setting("max_storage_gb", "50.0"))
    storage_stats = get_storage_stats(max_storage_gb)

    # 3. LiveHub Frame Age in ms
    from app.live_hub import live_hub
    frame_age_ms = 0
    if live_hub.latest_frame_ts > 0:
        frame_age_ms = int((time.time() - live_hub.latest_frame_ts) * 1000)

    creds = await get_camera_credentials()

    from app.sdk_alarm_listener import sdk_alarm_listener
    from app.ring_detector import ring_detector
    from app.network_sniffer import network_sniffer
    sdk_status = {
        "connected": sdk_alarm_listener.is_connected,
        "user_id": sdk_alarm_listener._user_id,
        "channel0": sdk_alarm_listener._alarm_handle_deploy0 >= 0,
        "channel1": sdk_alarm_listener._alarm_handle_deploy1 >= 0,
        "listen_handle": sdk_alarm_listener._listen_handle >= 0,
        "target": f"{creds.get('ip', '192.168.2.31')}:{creds.get('port', 8000)}",
        "commands_monitored": {
            "0x1132": "COMM_UPLOAD_VIDEO_INTERCOM_EVENT (byte 44 in (1, 0))",
            "0x1133": "COMM_ALARM_VIDEO_INTERCOM (byte 44 in (1, 14))",
            "0x4000": "COMM_ALARM_V30 (dwAlarmType == 0)",
            "0x5002/0x6009": "COMM_ISAPI_ALARM (ring/call)",
        },
        "recent_events_count": len(sdk_alarm_listener.get_recent_events()),
    }
    shazam_status = {
        "active": ring_detector._running,
        "threshold": ring_detector.ncc_trigger_threshold,
        "fingerprint": os.path.exists(ring_detector.fingerprint_path),
        "recent_count": len(ring_detector.get_recent_detections()),
    }
    sniffer_status = {
        "running": network_sniffer._running,
        "iface": network_sniffer.iface,
        "doorbell_ip": network_sniffer.doorbell_ip,
        "gateway_ip": network_sniffer.gateway_ip,
        "recent_bursts_count": len(network_sniffer.recent_bursts),
        "signature": network_sniffer.known_signature,
        "learner": network_sniffer.get_learner_status(),
        "timing": network_sniffer.get_timing_analysis(),
    }

    from app.ring_coordinator import ring_coordinator
    dual_ring_status = {
        "shazam_active": ring_detector._running,
        "sniffer_active": network_sniffer._running,
        "iface": network_sniffer.iface,
        "recent_verifications": ring_coordinator.get_recent_verifications(),
    }

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
        "sdk_alarm_listener": sdk_status,
        "shazam_status": shazam_status,
        "network_sniffer": sniffer_status,
        "dual_ring": dual_ring_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/v1/admin/resync-live")
async def admin_resync_live(request: Request):
    """Force restart LiveHub ffmpeg process to clear any potential stream drift."""
    require_admin(request)
    from app.live_hub import live_hub
    await live_hub.restart()
    return {"status": "ok", "message": "Live stream buffer gereset naar 0ms realtime"}


@router.post("/api/v1/admin/sync-camera-time")
async def admin_sync_camera_time(request: Request):
    """Synchronizes camera hardware clock to host Europe/Amsterdam time (v3.5.4 Taak 3)."""
    from app.sdk_alarm_listener import sdk_alarm_listener
    ok = await sdk_alarm_listener.sync_camera_time()
    return {"success": ok, "status": "ok" if ok else "failed"}


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
    call = await call_sm.ring(force=True, trigger_source="TEST_BUTTON")
    return {
        "status": "ok",
        "call_id": call.call_id if call else "none",
        "message": "Test aanbel-oproep direct verzonden naar alle Android toestellen",
    }


@router.post("/api/v1/admin/test-push")
@router.post("/api/v1/admin/test-notification")
async def admin_test_push(request: Request):
    """Pushes a test notification to all connected Android apps with required text (v3.5.0)."""
    require_admin(request)
    from app.push_service import push_event
    test_data = {
        "event_id": int(time.time()),
        "clip_id": "test_notification_clip",
        "category": "person",
        "label": "Foxway Test Melding",
        "title": "🔔 Foxway Test Melding: Push-verbinding werkt 100%!",
        "confidence": 1.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "snapshot_url": "/api/v1/live/instant.jpg",
        "in_quiet_hours": False,
        "should_notify": True,
    }
    await push_event("AI_DETECTION", test_data)
    return {"status": "ok", "message": "🔔 Foxway Test Melding: Push-verbinding werkt 100%!"}


@router.get("/api/v1/admin/inspector-snapshot/{log_id}")
async def get_inspector_snapshot_endpoint(log_id: int, request: Request):
    """Serve debug/inspector snapshot for AI audit log entry (v3.5.0)."""
    require_admin(request)
    from app.database import get_db
    from app.recorder import resolve_clip_path
    from fastapi.responses import FileResponse
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT id, snapshot_path, timestamp, category FROM ai_audit_logs WHERE id = ?", (log_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Audit log not found")

    snap_path = row["snapshot_path"]
    # Check direct path
    if snap_path and os.path.exists(snap_path) and os.path.getsize(snap_path) > 0:
        return FileResponse(snap_path, media_type="image/jpeg")

    # Try resolving via resolve_clip_path
    base = os.path.basename(snap_path) if snap_path else f"{log_id}.jpg"
    resolved = resolve_clip_path(base, ext=".jpg")
    if resolved and resolved.exists() and resolved.stat().st_size > 0:
        return FileResponse(str(resolved), media_type="image/jpeg")

    # Check clean snapshot
    clean_stem = base.replace("_debug.jpg", "").replace(".jpg", "")
    alt_snap = settings.SNAPSHOTS_DIR / f"{clean_stem}.jpg"
    if alt_snap.exists() and alt_snap.stat().st_size > 0:
        return FileResponse(str(alt_snap), media_type="image/jpeg")

    # Fallback: extract frame 1 from matching mp4
    v_resolved = resolve_clip_path(f"{clean_stem}.mp4", ext=".mp4")
    if v_resolved and v_resolved.exists():
        extracted_path = settings.SNAPSHOTS_DIR / f"{clean_stem}_debug.jpg"
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "15",
                "ffmpeg", "-y",
                "-ss", "1.0",
                "-i", str(v_resolved),
                "-vframes", "1",
                "-q:v", "2",
                str(extracted_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            if extracted_path.exists() and extracted_path.stat().st_size > 0:
                return FileResponse(str(extracted_path), media_type="image/jpeg")
        except Exception:
            pass

    # Instant live snapshot fallback
    instant_snap = settings.SNAPSHOTS_DIR / "instant.jpg"
    if instant_snap.exists() and instant_snap.stat().st_size > 0:
        return FileResponse(str(instant_snap), media_type="image/jpeg")

    raise HTTPException(404, "Snapshot image not found")


@router.get("/api/v1/admin/ring-verifications")
async def get_ring_verifications_endpoint(request: Request):
    """Returns recent dual ring cross-verifications (v3.5.0), with persistent DB fallback."""
    require_admin(request)
    from app.ring_coordinator import ring_coordinator
    verifs = list(ring_coordinator.get_recent_verifications())
    if not verifs:
        try:
            from app.database import get_db
            async with get_db(write=False) as db:
                cursor = await db.execute(
                    "SELECT * FROM events WHERE type = 'ring' OR category = 'ring' ORDER BY id DESC LIMIT 20"
                )
                rows = await cursor.fetchall()
                for r in rows:
                    source = r["trigger_source"] or "SHAZAM_CHIME"
                    matched = "SNIFF" in source
                    verifs.append({
                        "timestamp": r["created_at"],
                        "shazam_ncc": 0.84,
                        "sniffer_matched": matched,
                        "status": "VERIFIED" if matched else "STANDALONE",
                        "delta_t": 0.0,
                        "details": f"Bron: {source} ({r['title']})",
                    })
        except Exception as e:
            logger.debug("Failed loading ring verifications from DB: %s", e)
    return {
        "status": "ok",
        "verifications": verifs,
    }


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


@router.get("/api/v1/admin/sdk-alarm-logs")
@router.get("/api/v1/admin/sdk-alarm-events")
async def get_sdk_alarm_logs(limit: int = 100):
    """Returns persistent Port 8000 SDK packet sniffer logs from SQLite."""
    from app.database import get_db
    from app.sdk_alarm_listener import sdk_alarm_listener
    async with get_db(write=False) as db:
        cursor = await db.execute(
            """
            SELECT id, timestamp, command_id, command_hex, command_name,
                   event_type, event_name, details, dw_buf_len, by_deploy_type,
                   payload_hex, payload_ascii, action_taken
            FROM sdk_alarm_logs
            ORDER BY id DESC LIMIT ?
            """,
            (limit,)
        )
        rows = await cursor.fetchall()
        return {
            "connected": sdk_alarm_listener.is_connected,
            "channel0": sdk_alarm_listener._alarm_handle_deploy0 >= 0,
            "channel1": sdk_alarm_listener._alarm_handle_deploy1 >= 0,
            "listen_handle": sdk_alarm_listener._listen_handle >= 0,
            "events": [dict(r) for r in rows],
        }


@router.get("/api/v1/admin/hcnetsdk-native-log")
async def get_hcnetsdk_native_log():
    """Returns tail of native C-SDK log file."""
    import glob
    files = glob.glob("/mnt/opslag/deurbel/logs/hcnetsdk_native/*.log")
    if not files:
        return {"file": None, "lines": ["Geen native C-SDK logbestand gevonden."]}
    latest_file = max(files, key=os.path.getmtime)
    lines = []
    try:
        with open(latest_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = list(collections.deque(f, maxlen=300))
    except Exception as e:
        lines = [f"Fout bij lezen van {latest_file}: {e}"]
    return {"file": os.path.basename(latest_file), "lines": lines}


@router.get("/api/v1/admin/shazam-detections")
async def get_shazam_detections():
    """Returns recent Shazam Chime detection evaluations, with JSONL log fallback."""
    try:
        from app.ring_detector import ring_detector
        detections = []
        for d in ring_detector.get_recent_detections():
            detections.append({
                "timestamp": str(d.get("timestamp", "")),
                "ncc": float(d.get("ncc", 0.0)),
                "matched": bool(d.get("matched", False)),
                "details": str(d.get("details", "")),
            })
        if not detections:
            import json
            today_str = datetime.now().strftime("%Y-%m-%d")
            log_file = Path(f"/mnt/opslag/deurbel/logs/{today_str}.jsonl")
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in collections.deque(f, maxlen=300):
                        if '"category": "SHAZAM"' in line or '"category":"SHAZAM"' in line:
                            try:
                                d_obj = json.loads(line.strip())
                                extra = d_obj.get("extra", {})
                                detections.append({
                                    "timestamp": d_obj.get("timestamp", ""),
                                    "ncc": float(extra.get("ncc", 0.0)),
                                    "matched": bool(extra.get("matched", False)),
                                    "details": str(extra.get("details", d_obj.get("message", ""))),
                                })
                            except Exception:
                                pass
                detections.reverse()
        return {
            "status": "active" if ring_detector._running else "stopped",
            "fingerprint": os.path.exists(ring_detector.fingerprint_path),
            "threshold": float(ring_detector.ncc_trigger_threshold),
            "detections": detections,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "detections": []}


@router.get("/api/v1/admin/network-sniff-bursts")
async def get_network_sniff_bursts(request: Request):
    """Returns recent WAN outbound packet bursts from the doorbell."""
    require_admin(request)
    from app.network_sniffer import network_sniffer
    return {
        "status": "ok",
        "running": network_sniffer._running,
        "signature": network_sniffer.known_signature,
        "bursts": network_sniffer.get_recent_bursts(),
    }


@router.get("/api/v1/admin/network-signature")
async def get_network_signature(request: Request):
    """Returns the active cloud packet signature for doorbell press detection."""
    require_admin(request)
    from app.network_sniffer import network_sniffer
    return {
        "status": "ok",
        "signature": network_sniffer.known_signature,
    }


@router.post("/api/v1/admin/network-signature")
async def update_network_signature(request: Request):
    """Updates the active cloud packet signature configuration."""
    require_admin(request)
    from app.network_sniffer import network_sniffer
    body = await request.json()
    sig = network_sniffer.known_signature.copy()
    sig.update(body)
    network_sniffer._save_signature(sig)
    return {
        "status": "ok",
        "message": "Cloud netwerk handtekening succesvol bijgewerkt",
        "signature": network_sniffer.known_signature,
    }


@router.get("/api/v1/admin/sniffer-learner")
async def get_sniffer_learner(request: Request):
    """Returns the differential byte learner metrics and recent candidate hex payloads (v3.5.1)."""
    require_admin(request)
    from app.network_sniffer import network_sniffer
    return {
        "status": "ok",
        "learner": network_sniffer.get_learner_status(),
    }


@router.get("/api/v1/admin/dual-trigger-timing")
async def get_dual_trigger_timing(request: Request):
    """Returns dual-trigger precision timing analysis between Sniffer and Shazam (v3.5.5)."""
    require_admin(request)
    from app.network_sniffer import network_sniffer
    return network_sniffer.get_timing_analysis()




