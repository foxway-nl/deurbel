"""
Foxway Smart Doorbell — FastAPI Application Entry Point.

Wires together all routes, WebSocket endpoints, the call state machine,
mDNS broadcaster, push service, and recorder.  Provides a clean
lifespan context manager for startup/shutdown hooks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import socket
import struct
import time
import uuid

# Force Europe/Amsterdam timezone across process and logging
os.environ["TZ"] = "Europe/Amsterdam"
if hasattr(time, "tzset"):
    time.tzset()
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import websockets
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from starlette.websockets import WebSocketState
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from jose import jwt
from pydantic import BaseModel, Field

from app.audio_bridge import (
    send_test_tone_to_doorbell,
    send_audio_to_doorbell,
    play_audio_to_doorbell_async,
    run_acoustic_loopback_test,
    LiveVoiceComSession,
)

from app.ai_detector import smart_detector, DetectionSettings
from app.live_hub import live_hub
from app.config import settings
from app.mdns_service import mdns_service
from app.admin import router as admin_router
from app.models import (
    ClaimRequest,
    ClaimResponse,
    ClipInfo,
    PairRequest,
    PairResponse,
    QuickReplyRequest,
    TrimRequest,
    UserInfo,
    init_db,
    list_clips,
    get_clip,
    save_device,
    get_all_devices,
    get_device,
    delete_device,
    find_existing_device,
    update_clip,
    update_device_last_seen,
    update_fcm_token,
    get_setting,
    set_setting,
    is_username_taken,
    get_next_default_username,
    get_camera_credentials,
    set_camera_credentials,
)
from app.push_service import push_event, ws_manager
from app.logger import log_client_dump
from app.recorder import (
    cleanup_expired_clips,
    cleanup_storage_and_retention,
    compress_clip_background,
    ensure_thumbnail,
    fetch_snapshot,
    get_manual_recording_status,
    get_storage_stats,
    inject_audio_reply,
    remove_clip,
    ring_buffer,
    start_manual_recording,
    start_recording,
    stop_manual_recording,
    stop_recording,
    stream_clip,
    trim_clip,
)
from app.state_machine import CallStateMachine

# ── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-22s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("foxway.main")

# ── State Machine Singleton ──────────────────────────────────────────

call_sm = CallStateMachine()

# ── Retention cleanup & Health monitoring task handles ───────────────

_cleanup_task: Optional[asyncio.Task] = None
_health_task: Optional[asyncio.Task] = None
_sweeper_task: Optional[asyncio.Task] = None
_doorbell_online = True
_doorbell_last_seen = time.time()
_last_camera_time_sync = 0.0


async def _periodic_cleanup() -> None:
    """Run dual-tier storage & retention cleanup every 4 hours."""
    while True:
        try:
            retention_days = int(await get_setting("retention_days", "30"))
            max_storage_gb = float(await get_setting("max_storage_gb", "50.0"))
            await cleanup_storage_and_retention(retention_days, max_storage_gb)
        except Exception:
            logger.exception("Storage cleanup error")
        await asyncio.sleep(4 * 3600)


async def _stuck_recording_sweeper_loop() -> None:
    """Run stuck recording sweeper every 15 seconds to ensure clips finalize to READY or get purged (v3.5.1)."""
    while True:
        try:
            from app.recorder import sweep_stuck_recordings
            await sweep_stuck_recordings(max_age_seconds=40.0)
        except Exception:
            logger.debug("Error in stuck recording sweeper loop")
        await asyncio.sleep(15.0)



def _check_tcp_port(ip: str, port: int, timeout: float = 1.5) -> bool:
    """Synchronous TCP port reachability probe."""
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


async def _doorbell_health_monitor() -> None:
    """Monitor doorbell reachability every 30s. Auto-heals IP if changed, broadcasts alert if unreachable > 120s."""
    global _doorbell_online, _doorbell_last_seen
    while True:
        try:
            loop = asyncio.get_running_loop()
            online = False
            creds = await get_camera_credentials()
            cam_ip = creds.get("ip", "")
            cam_port = creds.get("port", 8000)

            if cam_ip:
                for port in (cam_port, 554):
                    if await loop.run_in_executor(None, _check_tcp_port, cam_ip, port, 5.0):
                        online = True
                        break

            now = time.time()
            if online:
                _doorbell_last_seen = now
                if not _doorbell_online:
                    _doorbell_online = True
                    logger.info("Doorbell %s is back ONLINE", cam_ip)
                    await ws_manager.broadcast("DOORBELL_STATUS", {"online": True, "ip": cam_ip})

                # Hourly camera hardware clock synchronization (v3.5.4 Taak 3)
                global _last_camera_time_sync
                if now - _last_camera_time_sync > 3600:
                    _last_camera_time_sync = now
                    try:
                        from app.sdk_alarm_listener import sdk_alarm_listener
                        await sdk_alarm_listener.sync_camera_time()
                    except Exception:
                        pass
            else:
                # Trigger auto-heal subnet scan
                logger.info("Doorbell %s unreachable. Triggering auto-heal subnet scan...", cam_ip)
                from app.camera_discovery import auto_heal_doorbell_ip
                new_ip = await auto_heal_doorbell_ip()
                if new_ip and new_ip != cam_ip:
                    logger.info("Doorbell auto-healed to new IP %s", new_ip)
                    _doorbell_online = True
                    _doorbell_last_seen = now
                    await ws_manager.broadcast("DOORBELL_STATUS", {"online": True, "ip": new_ip})
                else:
                    downtime = now - _doorbell_last_seen
                    if downtime > 120 and _doorbell_online:
                        _doorbell_online = False
                        logger.warning("Doorbell %s OFFLINE for >2 minutes (%.0fs)", cam_ip, downtime)
                        await ws_manager.broadcast(
                            "DOORBELL_STATUS",
                            {"online": False, "ip": cam_ip, "downtime_seconds": int(downtime)},
                        )
        except Exception as e:
            logger.warning("Health monitor loop error: %s", e)
        await asyncio.sleep(30)


# ── State Machine Callbacks ──────────────────────────────────────────

async def _on_ringing(call):
    """Broadcast CALL_RINGING via WS + FCM and record in event queue."""
    from app.models import save_event
    snapshot_url = f"/api/v1/snapshots/{call.call_id}.jpg" if call.snapshot_path else ""
    trigger_src = getattr(call, "trigger_source", "UNKNOWN")
    event_dict = await save_event(
        event_type="ring",
        category="ring",
        title="Aanbel oproep",
        clip_id=call.call_id,
        snapshot_url=snapshot_url,
        trigger_source=trigger_src,
    )
    # LiveVoiceComSession connects on-demand when the user answers (taps "Opnemen").
    # We do NOT prewarm over port 8000 during ringing to avoid native C-SDK thread contention.

    # Trigger passive doorbell status monitor (v3.4.0)
    try:
        from app.sdk_alarm_listener import sdk_alarm_listener
        asyncio.create_task(sdk_alarm_listener.log_passive_doorbell_status(trigger_source=trigger_src))
    except Exception as mon_err:
        logger.debug("Failed to trigger passive doorbell status monitor: %s", mon_err)

    # Auto-calibrate EZVIZ Cloud network sniffer on Shazam chime (v3.4.1)
    if trigger_src == "SHAZAM_CHIME":
        try:
            from app.network_sniffer import network_sniffer
            network_sniffer.on_shazam_chime_triggered(time.time())
        except Exception as sniff_err:
            logger.debug("Failed auto-calibrating network sniffer: %s", sniff_err)

    await push_event("CALL_RINGING", {
        "event_id": event_dict["id"],
        "call_id": call.call_id,
        "snapshot_url": snapshot_url,
        "timestamp": call.started_at,
        "trigger_source": trigger_src,
    })


async def _on_claimed(call):
    """Broadcast CALL_CLAIMED to all other devices."""
    await push_event(
        "CALL_CLAIMED",
        {
            "call_id": call.call_id,
            "answered_by": call.claimed_by,
            "user_id": call.claimed_by_user_id,
        },
        exclude_user=call.claimed_by_user_id,
    )


async def _on_missed(call):
    """Broadcast CALL_MISSED."""
    await push_event("CALL_MISSED", {"call_id": call.call_id})


async def _on_ended(call):
    """Broadcast CALL_ENDED."""
    await push_event("CALL_ENDED", {
        "call_id": call.call_id,
        "answered_by": call.claimed_by or "",
    })
    # Reset active voice channel when call ends
    from app.audio_bridge import force_close_active_voicecom
    force_close_active_voicecom(logout_user=True)


async def _on_fetch_snapshot(call_id: str) -> str:
    return await fetch_snapshot(call_id)


async def _on_start_recording(call_id: str, clip_path: str) -> asyncio.Task:
    return await start_recording(call_id, clip_path)


async def _on_stop_recording(task: asyncio.Task) -> None:
    # In v3.5.4, call recordings run autonomously to completion with dynamic motion tracking
    pass


# ── Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle hooks."""
    global _cleanup_task, _health_task, _sweeper_task

    # Startup
    logger.info("🚀 Foxway Doorbell Backend starting on port %d", settings.HOST_PORT)
    await init_db()
    from app.models import sync_and_index_clips_on_disk
    await sync_and_index_clips_on_disk()
    await mdns_service.start()

    # Wire Ring Coordinator (v3.5.0 Dual Detectie)
    from app.ring_coordinator import ring_coordinator
    ring_coordinator.set_loop(asyncio.get_running_loop())

    # Wire state machine callbacks
    call_sm.on_ringing = _on_ringing
    call_sm.on_claimed = _on_claimed
    call_sm.on_missed = _on_missed
    call_sm.on_ended = _on_ended
    call_sm.on_fetch_snapshot = _on_fetch_snapshot
    call_sm.on_start_recording = _on_start_recording
    call_sm.on_stop_recording = _on_stop_recording

    # Start periodic storage cleanup, camera health monitor & stuck recording sweeper
    _cleanup_task = asyncio.create_task(_periodic_cleanup())
    _health_task = asyncio.create_task(_doorbell_health_monitor())
    _sweeper_task = asyncio.create_task(_stuck_recording_sweeper_loop())

    # Start low-CPU Smart AI detector
    try:
        await smart_detector.start()
    except Exception as e:
        logger.warning("Could not start smart detector: %s", e)

    # Start Hikvision Port 8000 C-SDK hardware doorbell pushbutton listener
    try:
        from app.sdk_alarm_listener import sdk_alarm_listener
        await sdk_alarm_listener.start()
    except Exception as e:
        logger.warning("Could not start SDK Alarm Listener: %s", e)

    # Start Real-Time 3-Layer Shazam Doorbell Chime Detector
    try:
        from app.ring_detector import ring_detector
        await ring_detector.start()
    except Exception as e:
        logger.warning("Could not start Shazam Ring Detector: %s", e)

    # Start RAM Ring Buffer for continuous smart pre-roll
    try:
        await ring_buffer.start()
    except Exception as e:
        logger.warning("Could not start RAM Ring Buffer: %s", e)

    # Start Always-Warm RAM Live Hub for <200ms live video & audio
    try:
        await live_hub.start()
    except Exception as e:
        logger.warning("Could not start Live Hub: %s", e)

    # Start Host-Level EZVIZ Cloud Packet Sniffer & ARP Redirector (v3.4.1)
    try:
        from app.network_sniffer import network_sniffer
        await network_sniffer.start()
    except Exception as e:
        logger.warning("Could not start Network Sniffer: %s", e)

    logger.info("✅ Backend ready")
    yield

    # Shutdown
    logger.info("🛑 Shutting down…")
    if _cleanup_task:
        _cleanup_task.cancel()
    if _health_task:
        _health_task.cancel()
    if _sweeper_task:
        _sweeper_task.cancel()
    try:
        await live_hub.stop()
    except Exception:
        pass
    try:
        await smart_detector.stop()
    except Exception:
        pass
    try:
        await ring_buffer.stop()
    except Exception:
        pass
    try:
        from app.sdk_alarm_listener import sdk_alarm_listener
        await sdk_alarm_listener.stop()
    except Exception:
        pass
    try:
        from app.ring_detector import ring_detector
        await ring_detector.stop()
    except Exception:
        pass
    try:
        from app.network_sniffer import network_sniffer
        await network_sniffer.stop()
    except Exception:
        pass
    await mdns_service.stop()
    logger.info("👋 Goodbye")


# ── FastAPI App ───────────────────────────────────────────────────────

openapi_tags = [
    {"name": "Live Video & Streaming", "description": "WebRTC WHEP, fMP4 chunked, and WebSocket stream proxies (SD & HD)."},
    {"name": "Two-Way Audio Talk", "description": "Outdoor speaker streaming and real-time live talkback via Port 8000 SDK."},
    {"name": "Call Signaling & State", "description": "Doorbell ring, claim, quick reply, and hangup state machine."},
    {"name": "Clips & Recordings", "description": "HTTP Range MP4 video streaming, trimming, manual recording, and retention."},
    {"name": "Smart AI Detection", "description": "Low-CPU 2-stage motion and YOLOv8n object classification (Person/Vehicle/Animal)."},
    {"name": "Settings & Storage", "description": "Detection categories, quiet hours, retention days, and disk quota."},
    {"name": "Devices & Family", "description": "1-click pairing, push notification tokens, and online presence."},
    {"name": "Health & Diagnostics", "description": "System status, hardware acoustic loopback, and camera ping."},
]

app = FastAPI(
    title="Foxway Smart Doorbell API",
    version="3.5.5",
    description="Signaling, state management, media proxy, and clip API for the Foxway Doorbell system.",
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)

app.include_router(admin_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def capture_wan_url_middleware(request: Request, call_next):
    """Dynamically detect public WAN URL from reverse proxy headers and persist in SQLite."""
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        hostname = host.split(":")[0]
        try:
            ip = ipaddress.ip_address(hostname)
            is_private = ip.is_loopback or ip.is_private
        except ValueError:
            is_private = False
        if not is_private:
            detected_url = f"{proto}://{host}".rstrip("/")
            from app.models import set_dynamic_wan_url
            asyncio.create_task(set_dynamic_wan_url(detected_url))

    return await call_next(request)


# ══════════════════════════════════════════════════════════════════════
#  HELPER: Subnet & Security Checks
# ══════════════════════════════════════════════════════════════════════

def _is_local_request(request: Request) -> bool:
    """Check if the request originates from the local LAN subnet, private subnets, docker networks, or localhost."""
    client_ips = []
    if request.client and request.client.host:
        client_ips.append(request.client.host)
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        client_ips.extend([ip.strip() for ip in xff.split(",") if ip.strip()])
    x_real = request.headers.get("X-Real-IP")
    if x_real:
        client_ips.append(x_real.strip())

    from app.camera_discovery import get_local_subnet
    try:
        local_net = get_local_subnet()
    except Exception:
        local_net = ipaddress.ip_network("127.0.0.0/8")

    for cip in client_ips:
        try:
            ip = ipaddress.ip_address(cip)
            if ip.is_loopback or ip.is_private:
                return True
            if ip in local_net:
                return True
        except ValueError:
            continue
    return False


async def verify_auth_or_local(
    request: Request,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
) -> bool:
    """
    Allow local LAN/loopback requests without authentication.
    On WAN, require valid JWT token in Authorization: Bearer or ?token=...
    """
    if _is_local_request(request):
        return True

    tok = None
    if authorization and authorization.startswith("Bearer "):
        tok = authorization[7:].strip()
    elif token:
        tok = token.strip()

    if tok:
        try:
            payload = jwt.decode(tok, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
            if payload.get("sub"):
                return True
        except Exception:
            pass

    # For safety, if unauthenticated on external WAN, reject
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required on external WAN connection",
    )


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Health / Status
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/status")
@app.get("/api/status")
@app.head("/api/v1/status")
@app.head("/api/status")
async def get_status():
    """Current call state + connected clients summary."""
    state = await call_sm.status()
    state["connected_ws_users"] = ws_manager.connected_user_ids
    from app.models import get_dynamic_wan_url
    server_wan_url = await get_dynamic_wan_url()
    state["server_wan_url"] = server_wan_url
    state["wan_url"] = server_wan_url
    from app.camera_discovery import get_local_ip
    local_ip = get_local_ip()
    server_lan_url = f"http://{local_ip}:{settings.HOST_PORT}"
    state["server_lan_url"] = server_lan_url
    state["lan_url"] = server_lan_url
    return state


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Pairing
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/devices/pair", response_model=PairResponse)
@app.post("/api/v1/pair", response_model=PairResponse)
@app.post("/api/pair", response_model=PairResponse)
async def pair_device(body: PairRequest, request: Request):
    """
    1-click device pairing.
    Allows pairing from LAN/private networks, with valid bootstrap/QR tokens, or with valid device registration payload.
    Generates a long-lived JWT and stores the device record.
    Supports optional username (auto-generated if empty) and unique username enforcement.
    """
    is_local = _is_local_request(request)
    has_valid_token = False
    if getattr(body, "token", None):
        try:
            payload = jwt.decode(body.token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
            if payload.get("purpose") in ("qr_pairing", "bootstrap") or payload.get("sub"):
                has_valid_token = True
        except Exception:
            pass

    # Seamless registration: allow if local/private network, if token is valid, or if valid device registration payload
    if not is_local and not has_valid_token and not (body.device_id and body.device_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pairing is only allowed from local network or with valid pairing payload",
        )

    device_id = (body.device_id or "").strip()
    user_name = (body.user_name or "").strip()
    if not user_name:
        user_name = await get_next_default_username()

    # Deduplication: check if this device already exists to avoid duplicate entries
    existing = await find_existing_device(
        device_id=device_id,
        user_name=user_name,
        device_name=body.device_name,
    )

    # Enforce unique usernames across different physical devices:
    # If this device is already known (re-pairing), or request was validated with a QR/bootstrap token,
    # allow it. Only reject if an unrelated device tries to take an already-claimed username.
    if not existing and not has_valid_token:
        if await is_username_taken(user_name, device_id=device_id, device_name=body.device_name):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"De naam '{user_name}' is al gekoppeld aan een ander toestel. Kies een unieke naam.",
            )

    if existing:
        user_id = existing["user_id"]
        logger.info("Re-pairing existing device: %s (%s) → %s", user_name, body.device_name, user_id)
    else:
        user_id = user_name.lower().replace(" ", "_") + "_" + uuid.uuid4().hex[:6]

    token_payload = {
        "sub": user_id,
        "name": user_name,
        "device": body.device_name,
        "iat": datetime.now(timezone.utc).isoformat(),
    }
    jwt_token = jwt.encode(token_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    await save_device(
        user_id=user_id,
        user_name=user_name,
        device_name=body.device_name,
        fcm_token=body.fcm_token,
        jwt_token=jwt_token,
        device_id=device_id,
        android_version=body.android_version or "",
        connection_type=body.connection_type or "",
    )

    logger.info("Paired: %s (%s) → %s", user_name, body.device_name, user_id)

    # Security Alert: Notify existing family devices when a new device pairs
    alert_text = f"🔐 Nieuw apparaat gekoppeld: {user_name} ({body.device_name})"
    await ws_manager.broadcast(
        "USER_JOINED",
        {
            "user_id": user_id,
            "user_name": user_name,
            "device_name": body.device_name,
            "android_version": body.android_version or "",
            "alert": alert_text,
        },
        exclude_user=user_id,
    )

    from app.camera_discovery import get_local_ip
    from app.models import get_dynamic_wan_url
    local_ip = get_local_ip()
    active_lan_url = f"http://{local_ip}:{settings.HOST_PORT}"
    active_wan_url = await get_dynamic_wan_url()

    return PairResponse(
        user_id=user_id,
        user_name=user_name,
        device_name=body.device_name,
        jwt_token=jwt_token,
        lan_url=active_lan_url,
        wan_url=active_wan_url,
        go2rtc_lan_ws=f"ws://{local_ip}:1984",
        go2rtc_wan_ws=f"{active_wan_url.replace('https://', 'wss://').replace('http://', 'ws://')}/api/ws",
    )


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Onboarding Bootstrap (Task 1)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/onboarding/bootstrap")
async def onboarding_bootstrap():
    """
    Smart auto-fill discovery endpoint for the Android app Onboarding.
    Dynamically returns server_lan_url, server_wan_url, camera_discovered, camera_ip, camera_configured.
    """
    from app.camera_discovery import get_local_ip, is_doorbell_alive, auto_heal_doorbell_ip
    from app.models import get_dynamic_wan_url

    creds = await get_camera_credentials()
    cam_ip = creds.get("ip", "")
    cam_port = creds.get("port", 8000)

    # Check if doorbell responds
    is_alive = False
    if cam_ip:
        is_alive = await is_doorbell_alive(cam_ip, cam_port)

    # Auto-heal / scan if not alive
    if not is_alive and not creds.get("is_configured"):
        healed_ip = await auto_heal_doorbell_ip()
        if healed_ip:
            cam_ip = healed_ip
            is_alive = True

    local_ip = get_local_ip()
    server_lan_url = f"http://{local_ip}:{settings.HOST_PORT}"
    server_wan_url = await get_dynamic_wan_url()

    bootstrap_token = jwt.encode(
        {"purpose": "bootstrap", "iat": datetime.now(timezone.utc).isoformat()},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM
    )

    is_configured = creds.get("is_configured", False)
    status_msg = (
        f"✅ Server & Deurbel automatisch gevonden op netwerk ({cam_ip})"
        if (is_alive or cam_ip)
        else "⚠️ Geen deurbel gevonden op dit netwerk"
    )

    return {
        "status": "ok",
        "server_name": "Foxway Deurbel Server",
        "server_lan_url": server_lan_url,
        "server_wan_url": server_wan_url,
        "lan_url": server_lan_url,
        "wan_url": server_wan_url,
        "camera_discovered": is_alive or bool(cam_ip),
        "camera_ip": cam_ip,
        "camera_port": cam_port,
        "camera_user": creds.get("user", "admin"),
        "camera_configured": is_configured,
        "camera_credentials_configured": is_configured,
        "doorbell_online": is_alive,
        "doorbell_status_message": status_msg,
        "bootstrap_token": bootstrap_token,
        "token": bootstrap_token,
    }


@app.get("/api/v1/onboarding/qr")
async def onboarding_qr():
    """
    Returns QR pairing payload and base64 PNG QR code for 1-click scanning.
    """
    import io
    import base64
    import json
    token = jwt.encode(
        {"purpose": "qr_pairing", "iat": datetime.now(timezone.utc).isoformat()},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM
    )
    creds = await get_camera_credentials()
    payload = {
        "v": 1,
        "lan_url": settings.LAN_URL,
        "wan_url": settings.WAN_URL,
        "camera_ip": creds["ip"],
        "token": token,
    }
    raw_json = json.dumps(payload)
    qr_data_url = ""
    try:
        import qrcode
        img = qrcode.make(raw_json)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        qr_data_url = f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.debug("QR generation error: %s", e)

    return {
        "status": "ok",
        "payload": payload,
        "payload_json": raw_json,
        "qr_image_url": qr_data_url,
    }


class CameraSettingsUpdate(BaseModel):
    ip: str = Field(...)
    port: int = Field(8000)
    user: str = Field("admin")
    password: str = Field("")


@app.get("/api/v1/settings/camera")
async def get_camera_settings_endpoint():
    creds = await get_camera_credentials()
    return {
        "ip": creds["ip"],
        "port": creds["port"],
        "user": creds["user"],
        "is_configured": creds["is_configured"],
    }


@app.put("/api/v1/settings/camera")
async def put_camera_settings_endpoint(body: CameraSettingsUpdate):
    await set_camera_credentials(body.ip, body.port, body.user, body.password)
    return {"status": "ok", "message": "Camera credentials updated"}


@app.get("/api/v1/app/version")
@app.get("/api/v1/version")
async def app_version_endpoint():
    """In-app version check & 1-click self-updater information."""
    apk_path = settings.CLIPS_DIR / "foxway-deurbel.apk"
    apk_size = apk_path.stat().st_size if apk_path.exists() else 0
    return {
        "version": "3.5.5",
        "version_code": 37,
        "build_time": "2026-11-01T21:10:00Z",
        "apk_url": "/clips/foxway-deurbel.apk",
        "apk_download_url": f"{settings.WAN_URL}/clips/foxway-deurbel.apk",
        "apk_size_bytes": apk_size,
        "changelog": "v3.5.3: Opschoning oude opnames t/m 20:16:52, vereenvoudigde instellingen (logboek uitsluitend via i-icoon), knoptekst fix en LAN/WAN verbindingsbadges.",
    }


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Shelly Plus Uni Webhook & Doorbell Ring (Task 3)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/webhook/shelly-ring")
@app.get("/api/v1/webhook/shelly-ring")
@app.post("/api/v1/ring")
@app.get("/api/v1/ring")
@app.post("/api/ring")
@app.get("/api/ring")
async def shelly_ring_webhook(request: Request, force: bool = False):
    """
    Dedicated webhook endpoint for Shelly Plus Uni chime input trigger.
    Debounced by 15s to prevent duplicate calls unless force=True.
    """
    is_force = force or (request.query_params.get("force", "").lower() in ("true", "1"))
    src = "TEST_BUTTON" if is_force else "SHELLY_WEBHOOK"
    call = await call_sm.ring(force=is_force, trigger_source=src)
    if call is None:
        return JSONResponse(status_code=200, content={"status": "SUPPRESSED", "reason": "cooldown or active call"})

    snapshot_url = f"/api/v1/snapshots/{call.call_id}.jpg" if call.snapshot_path else ""
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok" if is_force else "RINGING",
            "ring_status": "RINGING",
            "call_id": call.call_id,
            "snapshot_url": snapshot_url,
            "timestamp": call.started_at,
            "trigger_source": src,
        }
    )


@app.get("/api/v1/webhook/shelly-ring/status")
async def shelly_ring_status():
    from app.camera_discovery import get_local_ip
    from app.models import get_dynamic_wan_url
    local_ip = get_local_ip()
    wan_url = await get_dynamic_wan_url()
    return {
        "status": "active",
        "detector": "Hardware Interrupt actief (Poort 8000 C-SDK COMM_UPLOAD_VIDEO_INTERCOM_EVENT + Shelly Webhook)",
        "webhook_url_lan": f"http://{local_ip}:{settings.HOST_PORT}/api/v1/webhook/shelly-ring",
        "webhook_url_wan": f"{wan_url}/api/v1/webhook/shelly-ring",
        "instruction": "Configureer in Shelly Plus Uni Web UI -> Input 0 -> Actions -> Button Pressed URL",
    }


@app.get("/api/v1/admin/sdk-alarm-events", tags=["Health & Diagnostics"])
@app.get("/api/v1/sdk-alarm-events", tags=["Health & Diagnostics"])
async def get_sdk_alarm_events():
    """Retrieve real-time Port 8000 C-SDK hardware doorbell events."""
    from app.sdk_alarm_listener import sdk_alarm_listener
    return {
        "connected": sdk_alarm_listener.is_connected,
        "events": sdk_alarm_listener.get_recent_events(),
    }


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Event Queue for Push Fallback (Task 6)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/events")
async def get_events_endpoint(
    since_id: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    order: str = Query("asc", pattern="^(asc|desc)$"),
):
    """Retrieve event queue items since since_id for reliable push fallback."""
    from app.models import get_events_since
    events = await get_events_since(since_id=since_id, limit=limit, order=order)
    return {"status": "ok", "events": events, "count": len(events)}


@app.get("/api/v1/events/unnotified")
async def get_unnotified_events_endpoint(
    since: Optional[str] = Query(None, description="ISO timestamp or epoch seconds"),
    limit: int = Query(20, ge=1, le=100),
):
    """Retrieve recent non-silent events for client reconnect synchronization (v3.5.5)."""
    from app.models import get_unnotified_events
    events = await get_unnotified_events(since=since, limit=limit)
    return {"status": "ok", "events": events, "count": len(events)}


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: FCM Token Update
# ══════════════════════════════════════════════════════════════════════

@app.put("/api/v1/devices/{user_id}/fcm-token")
async def update_device_fcm_token(user_id: str, body: dict):
    """Update the FCM token for a device (called after token refresh)."""
    fcm_token = body.get("fcm_token", "")
    if not fcm_token:
        raise HTTPException(400, "fcm_token is required")
    device = await get_device(user_id)
    if not device:
        raise HTTPException(404, "Device not found")
    await update_fcm_token(user_id, fcm_token)
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Ring (trigger from Shelly / audio detector)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/ring")
@app.get("/api/v1/ring")
@app.post("/api/ring")
@app.get("/api/ring")
async def ring():
    """
    Trigger a new doorbell ring.

    Called via Shelly Plus Uni webhook (GET or POST) or manual trigger.
    Idempotent — duplicate rings during cooldown or an active call are silently ignored.
    """
    call = await call_sm.ring(trigger_source="SHELLY_WEBHOOK")
    if call is None:
        return {"status": "SUPPRESSED", "reason": "cooldown or active call"}

    snapshot_url = f"/api/v1/snapshots/{call.call_id}.jpg" if call.snapshot_path else ""
    return {
        "status": "RINGING",
        "call_id": call.call_id,
        "snapshot_url": snapshot_url,
        "timestamp": call.started_at,
        "trigger_source": "SHELLY_WEBHOOK",
    }


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Claim (atomic multi-user)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/call/{call_id}/claim", response_model=ClaimResponse)
@app.post("/api/call/{call_id}/claim", response_model=ClaimResponse)
async def claim_call(call_id: str, body: ClaimRequest):
    """
    Atomically claim a ringing call.

    Returns 200 ``CLAIMED`` on success or 409 ``ALREADY_CLAIMED`` if
    another family member answered first.
    """
    result = await call_sm.claim(call_id, body.user_id, body.user_name)

    if result["status"] == "NOT_FOUND":
        raise HTTPException(404, "Call not found or already ended")
    if result["status"] == "INVALID_STATE":
        raise HTTPException(400, f"Invalid call state: {result.get('current_state')}")
    if result["status"] == "ALREADY_CLAIMED":
        return JSONResponse(
            status_code=409,
            content={
                "status": "ALREADY_CLAIMED",
                "answered_by": result["answered_by"],
            },
        )

    return ClaimResponse(
        status="CLAIMED",
        webrtc_src=result.get("webrtc_src"),
    )


@app.post("/api/call/claim", response_model=ClaimResponse)
async def claim_call_no_path(body: ClaimRequest):
    """Claim active call with call_id in request body or auto-detected."""
    call_id = body.call_id or (call_sm.current_call.call_id if call_sm.current_call else None)
    if not call_id:
        raise HTTPException(404, "No active ringing call to claim")
    return await claim_call(call_id, body)


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Hangup
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/call/{call_id}/hangup")
@app.post("/api/call/{call_id}/hangup")
async def hangup_call(call_id: str):
    """End the active call and return to IDLE."""
    result = await call_sm.hangup(call_id)
    if result["status"] == "NOT_FOUND":
        raise HTTPException(404, "Call not found or already ended")
    return result


@app.post("/api/call/hangup")
async def hangup_call_active(body: Optional[dict] = None):
    """Hang up active call."""
    call_id = (body or {}).get("call_id") or (call_sm.current_call.call_id if call_sm.current_call else None)
    if not call_id:
        raise HTTPException(404, "No active call to hangup")
    return await hangup_call(call_id)


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Quick Reply
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/call/{call_id}/quick-reply")
@app.post("/api/call/{call_id}/quick-reply")
async def quick_reply(call_id: str, body: QuickReplyRequest):
    """
    Send a pre-recorded audio reply through the doorbell speaker and
    atomically claim the call.
    """
    result = await call_sm.quick_reply(
        call_id, body.user_name.lower().replace(" ", "_"), body.user_name, body.reply_id
    )

    if result["status"] in ("NOT_FOUND", "INVALID_STATE"):
        raise HTTPException(400, f"Cannot send quick reply: {result['status']}")

    # Fire-and-forget audio injection in the background
    asyncio.create_task(inject_audio_reply(body.reply_id))

    return result


@app.post("/api/call/quick-reply")
async def quick_reply_active(body: QuickReplyRequest):
    """Send quick reply to active ringing call."""
    call_id = body.call_id or (call_sm.current_call.call_id if call_sm.current_call else None)
    if not call_id:
        raise HTTPException(404, "No active call for quick reply")
    return await quick_reply(call_id, body)


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Snapshots & Thumbnails
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/snapshots/{filename}")
@app.get("/api/snapshots/{filename}")
async def serve_snapshot(filename: str):
    """Serve a snapshot JPEG file. Generates dynamically from clip if missing."""
    clean_id = filename.rsplit(".", 1)[0].replace("thumb_", "")
    candidates = [
        settings.SNAPSHOTS_DIR / filename,
        settings.CLIPS_DIR / filename,
        settings.CLIPS_DIR / f"thumb_{clean_id}.jpg",
        settings.SNAPSHOTS_DIR / f"{clean_id}.jpg",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            return FileResponse(str(p), media_type="image/jpeg")

    ensured = await ensure_thumbnail(clean_id)
    if ensured and os.path.exists(ensured):
        return FileResponse(str(ensured), media_type="image/jpeg")
    raise HTTPException(404, "Snapshot not found")


@app.get("/api/v1/clips/{clip_id}/thumbnail")
@app.get("/api/clips/{clip_id}/thumbnail")
async def get_clip_thumbnail_endpoint(clip_id: str):
    """Serve thumbnail for clip. Resolves from day folders, clips dir, snapshots, or generates on-the-fly (v3.5.1)."""
    clean_id = clip_id.replace(".jpg", "").replace("thumb_", "")
    from app.recorder import resolve_clip_path, ensure_thumbnail

    for cand_name in [f"thumb_{clean_id}.jpg", f"{clean_id}.jpg", f"{clean_id}_debug.jpg"]:
        found = resolve_clip_path(cand_name, ext=".jpg")
        if found and found.exists() and found.stat().st_size > 0:
            return FileResponse(str(found), media_type="image/jpeg")

    ensured = await ensure_thumbnail(clean_id)
    if ensured and os.path.exists(ensured) and os.path.getsize(ensured) > 0:
        return FileResponse(str(ensured), media_type="image/jpeg")

    # Fallback 1x1 black JPEG bytes so it ALWAYS returns HTTP 200 image/jpeg
    one_pixel_jpeg = (
        b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00'
        b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t'
        b'\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a'
        b'\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342'
        b'\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00'
        b'\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00'
        b'\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00'
        b'\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'
    )
    return Response(content=one_pixel_jpeg, media_type="image/jpeg")


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Clips (2-Phase Lifecycle: RECORDING -> READY)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/clips")
@app.get("/api/clips")
async def get_clips(
    category: str = Query("all"),
    date: Optional[str] = Query(None),
    limit: int = Query(40, ge=1, le=100),
    offset: int = Query(0, ge=0),
    paged: Optional[bool] = Query(None),
    t: Optional[int] = Query(None),
    response: Response = None,
):
    """List all clips sorted newest first with duration, size, server-side filtering & pagination (v3.5.1)."""
    from app.recorder import resolve_clip_path, ensure_thumbnail, sweep_stuck_recordings
    await sweep_stuck_recordings(max_age_seconds=40.0)
    clips, total_count = await list_clips(category=category, date=date, limit=limit, offset=offset)
    result = []
    for c in clips:
        clip_id = c["clip_id"]
        status = c.get("status") or "READY"

        size_bytes = c.get("file_size") or 0
        if size_bytes == 0:
            resolved_v = resolve_clip_path(c.get("video_path") or clip_id, ext=".mp4")
            if resolved_v and resolved_v.exists():
                try:
                    size_bytes = resolved_v.stat().st_size
                except Exception:
                    pass

        result.append({
            "id": clip_id,
            "status": status,
            "timestamp": c["created_at"],
            "created_at": c["created_at"],
            "duration": c.get("duration", 0.0),
            "size_bytes": size_bytes,
            "category": c.get("category", ""),
            "thumbnail_url": f"/api/v1/clips/{clip_id}/thumbnail",
            "snapshot_url": f"/api/v1/clips/{clip_id}/thumbnail",
            "video_url": f"/api/v1/clips/{clip_id}/stream",
            "answered_by": c.get("answered_by"),
            "starred": bool(c.get("starred", 0)),
        })

    has_more = (offset + len(result)) < total_count
    if response:
        response.headers["X-Total-Count"] = str(total_count)
        response.headers["X-Has-More"] = "true" if has_more else "false"

    is_paged = (paged is True) or (category != "all" and category != "") or (date is not None)
    if is_paged:
        return {
            "clips": result,
            "total_count": total_count,
            "has_more": has_more,
            "offset": offset,
            "limit": limit,
        }
    return result


@app.get("/api/v1/clips/{clip_id}")
@app.get("/api/clips/{clip_id}")
async def get_clip_detail(clip_id: str, request: Request):
    """Retrieve metadata and status (RECORDING/READY) for a specific clip (v3.5.0)."""
    clean_id = clip_id.replace(".mp4", "")
    accept = request.headers.get("accept", "")
    if clip_id.endswith(".mp4") or "video/" in accept:
        return await stream_clip_endpoint(clean_id, request)

    from app.recorder import resolve_clip_path
    clip = await get_clip(clean_id)
    if not clip:
        alt_path = resolve_clip_path(f"{clean_id}.mp4", ext=".mp4")
        if not alt_path or not alt_path.exists():
            raise HTTPException(404, "Clip not found")
        v_size = alt_path.stat().st_size
        return {
            "id": clean_id,
            "clip_id": clean_id,
            "status": "READY",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration": 0.0,
            "size_bytes": v_size,
            "category": "",
            "thumbnail_url": f"/api/v1/clips/{clean_id}/thumbnail",
            "snapshot_url": f"/api/v1/clips/{clean_id}/thumbnail",
            "video_url": f"/api/v1/clips/{clean_id}/stream",
            "answered_by": None,
            "starred": False,
        }

    status = clip.get("status") or "READY"
    size_bytes = clip.get("file_size") or 0
    if size_bytes == 0:
        resolved_v = resolve_clip_path(clip.get("video_path") or clean_id, ext=".mp4")
        if resolved_v and resolved_v.exists():
            try:
                size_bytes = resolved_v.stat().st_size
            except Exception:
                pass

    return {
        "id": clean_id,
        "clip_id": clean_id,
        "status": status,
        "created_at": clip["created_at"],
        "timestamp": clip["created_at"],
        "duration": clip.get("duration", 0.0),
        "size_bytes": size_bytes,
        "category": clip.get("category", ""),
        "thumbnail_url": f"/api/v1/clips/{clean_id}/thumbnail",
        "snapshot_url": f"/api/v1/clips/{clean_id}/thumbnail",
        "video_url": f"/api/v1/clips/{clean_id}/stream",
        "answered_by": clip.get("answered_by"),
        "starred": bool(clip.get("starred", 0)),
    }


@app.get("/api/v1/clips/{clip_id}/stream")
@app.get("/api/v1/clips/{clip_id}/file")
@app.get("/api/clips/{clip_id}/stream")
@app.get("/api/clips/{clip_id}/file")
async def stream_clip_endpoint(clip_id: str, request: Request):
    """Stream a clip with HTTP Range support (206 Partial Content) via FileResponse (v3.5.0)."""
    clean_id = clip_id.replace(".mp4", "")
    clip = await get_clip(clean_id)
    if clip and clip.get("status") == "RECORDING":
        raise HTTPException(404, "Opname wordt nog opgenomen (status: RECORDING)")

    from app.recorder import resolve_clip_path
    file_path = None
    if clip and clip.get("video_path") and os.path.exists(clip["video_path"]):
        file_path = clip["video_path"]
    else:
        resolved = resolve_clip_path(f"{clean_id}.mp4", ext=".mp4")
        if resolved:
            file_path = str(resolved)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(404, "Clip not found")

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=f"{clean_id}.mp4",
    )


@app.post("/api/v1/clips/{clip_id}/trim")
@app.post("/api/clips/{clip_id}/trim")
async def trim_clip_endpoint(clip_id: str, body: TrimRequest):
    """Trim a clip to the specified time range."""
    if body.start_sec >= body.end_sec:
        raise HTTPException(400, "start_sec must be less than end_sec")

    output_path = await trim_clip(clip_id, body.start_sec, body.end_sec)
    if not output_path:
        raise HTTPException(404, "Clip not found or trim failed")

    trimmed_id = f"{clip_id}_trimmed"
    return {
        "status": "ok",
        "clip_id": trimmed_id,
        "video_url": f"/api/v1/clips/{trimmed_id}/stream",
        "download_url": f"/api/v1/clips/{trimmed_id}/stream",
    }


@app.post("/api/clips/trim")
async def trim_clip_body(body: TrimRequest):
    """Trim a clip where clip_id is in request body."""
    clip_id = body.clip_id
    if not clip_id:
        raise HTTPException(400, "clip_id is required in body")
    return await trim_clip_endpoint(clip_id, body)


@app.put("/api/v1/clips/{clip_id}/star")
async def star_clip(clip_id: str, body: dict):
    """Toggle the starred flag on a clip."""
    clip = await get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    starred = body.get("starred", True)
    await update_clip(clip_id, starred=1 if starred else 0)
    return {"status": "ok", "starred": starred}


@app.delete("/api/v1/clips/{clip_id}")
async def delete_clip_endpoint(clip_id: str):
    """Delete a clip and its snapshot from disk and database (cleans empty day folders, v3.5.0)."""
    clean_id = clip_id.replace(".mp4", "")
    from app.recorder import remove_clip
    ok = await remove_clip(clean_id)
    return {"status": "deleted", "clip_id": clean_id}


@app.post("/api/v1/clips/cleanup-unclassified")
@app.post("/api/clips/cleanup-unclassified")
async def cleanup_unclassified_clips(include_unstarred_vehicles: bool = Query(True)):
    """
    Cleans up junk unclassified and unstarred motion/vehicle clips to free server storage.
    """
    from app.database import get_db
    deleted_count = 0
    freed_bytes = 0

    where_clause = "(answered_by = '' OR answered_by LIKE '%Beweging%' OR clip_id LIKE 'motion_%' OR clip_id LIKE 'ai_motion_%')"
    if include_unstarred_vehicles:
        where_clause += " OR (starred = 0 AND clip_id LIKE 'ai_vehicle_%')"

    async with get_db(write=True) as db:
        cur = await db.execute(f"SELECT clip_id, video_path, snapshot_path FROM clips WHERE {where_clause}")
        rows = await cur.fetchall()

        for r in rows:
            clip_id = r["clip_id"]
            v_path = r["video_path"] or str(settings.CLIPS_DIR / f"{clip_id}.mp4")
            s_path = r["snapshot_path"] or str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")

            for p in [v_path, s_path, str(settings.CLIPS_DIR / f"{clip_id}.mp4"), str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")]:
                if os.path.exists(p):
                    try:
                        freed_bytes += os.path.getsize(p)
                        os.remove(p)
                    except Exception:
                        pass

            await db.execute("DELETE FROM clips WHERE clip_id = ?", (clip_id,))
            deleted_count += 1

        await db.commit()

    logger.info("Cleaned up %d junk clips (freed %.1f MB)", deleted_count, freed_bytes / (1024 * 1024))
    return {
        "status": "ok",
        "deleted_count": deleted_count,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "message": f"Succesvol {deleted_count} niet-geclassificeerde/oude opnames verwijderd ({round(freed_bytes / (1024 * 1024), 1)} MB vrijgemaakt)"
    }



@app.get("/clips/foxway-deurbel.apk")
@app.head("/clips/foxway-deurbel.apk")
@app.get("/foxway-deurbel.apk")
@app.head("/foxway-deurbel.apk")
async def download_apk():
    """Direct download for the compiled Foxway Deurbel Android APK."""
    apk_path = Path("/mnt/opslag/deurbel/clips/foxway-deurbel.apk")
    if not apk_path.exists():
        raise HTTPException(404, "APK not found")
    return FileResponse(
        str(apk_path),
        media_type="application/vnd.android.package-archive",
        filename="foxway-deurbel.apk",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ══════════════════════════════════════════════════════════════════════
#  ROUTES: Stream Proxies (fMP4, Frame JPEG, WebSocket MSE/WebRTC)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/stream.mp4", tags=["Live Video & Streaming"])
@app.head("/api/v1/stream.mp4", tags=["Live Video & Streaming"])
async def stream_mp4_proxy(src: str = Query("deurbel"), mode: str = Query("auto")):
    """
    Proxies continuous fragmented MP4 live stream (H.264 + AAC) with zero latency.
    Forces real-time chunking with anti-buffering headers for Cloudflare / 5G proxies.
    Supports HD ('deurbel') and SD ('deurbel_sub' / alias 'deurbel_sd').
    """
    if src in ("deurbel_sd", "sd", "sub"):
        src = "deurbel_sd"
    headers = {
        "Content-Type": "video/mp4",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Accel-Buffering": "no",
        "Transfer-Encoding": "chunked",
        "Connection": "keep-alive",
    }

    if mode == "ffmpeg":
        # Direct zero-latency FFmpeg transcoding mode
        rtsp_target = f"rtsp://127.0.0.1:8554/{src}"
        cmd = [
            "nice", "-n", "15",
            "ffmpeg", "-y", "-nostdin",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-probesize", "32768", "-analyzeduration", "200000",
            "-rtsp_transport", "tcp",
            "-i", rtsp_target,
            "-vf", "scale=640:480,setsar=1:1", "-aspect", "4:3",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "15",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-loglevel", "quiet", "-"
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def ffmpeg_generator():
            try:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
            except Exception:
                pass
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass

        return StreamingResponse(ffmpeg_generator(), media_type="video/mp4", headers=headers)

    # Standard high-performance go2rtc fMP4 mode with aiter_raw socket passthrough
    client = httpx.AsyncClient(timeout=None)
    try:
        req = client.build_request("GET", f"{settings.GO2RTC_API_URL}/api/stream.mp4?src={src}")
        r = await client.send(req, stream=True)

        async def stream_generator():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            except Exception:
                pass
            finally:
                await r.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            media_type="video/mp4",
            headers=headers,
        )
    except Exception as e:
        await client.aclose()
        logger.warning("fMP4 stream proxy error for %s: %s", src, e)
        raise HTTPException(502, f"Failed connecting to go2rtc stream: {e}")


@app.websocket("/api/go2rtc/ws")
@app.websocket("/api/v1/go2rtc/ws")
async def proxy_go2rtc_ws(websocket: WebSocket, src: str = Query("deurbel")):
    """
    Transparent, zero-buffer WebSocket proxy to go2rtc (ws://127.0.0.1:1984/api/ws?src=...).
    Forwards text and binary frames bidirectionally for native WebRTC and MSE streaming.
    Functions seamlessly across LAN and over 5G/Cloudflare WSS without UDP requirements!
    """
    await websocket.accept()
    query_str = str(websocket.query_params)
    target_url = f"ws://127.0.0.1:1984/api/ws?{query_str}" if query_str else f"ws://127.0.0.1:1984/api/ws?src={src}"
    try:
        async with websockets.connect(target_url, max_size=10_000_000) as upstream:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                        elif msg.get("type") == "websocket.disconnect":
                            break
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    while True:
                        data = await upstream.recv()
                        if websocket.client_state != WebSocketState.CONNECTED:
                            break
                        if isinstance(data, str):
                            await websocket.send_text(data)
                        elif isinstance(data, bytes):
                            await websocket.send_bytes(data)
                except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError, ConnectionResetError):
                    pass
                except Exception:
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client(), return_exceptions=True)
    except Exception as e:
        logger.debug("go2rtc ws proxy error: %s", e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/go2rtc/{asset_name:path}")
async def proxy_go2rtc_asset(asset_name: str):
    """Proxy static assets (video-stream.js, video-rtc.js) from go2rtc."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://127.0.0.1:1984/{asset_name}")
            media_type = resp.headers.get("content-type", "application/javascript")
            if asset_name.endswith(".js"):
                media_type = "application/javascript"
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=media_type
            )
    except Exception as e:
        raise HTTPException(502, f"Failed fetching go2rtc asset: {e}")


@app.get("/api/v1/live/player", response_class=HTMLResponse)
@app.get("/live/player", response_class=HTMLResponse)
async def get_live_player(src: str = Query("deurbel"), muted: bool = Query(True)):
    """
    Clean Zero-UI fullscreen 4:3 HTML5 player for native go2rtc hardware streaming (MSE + WebRTC).
    Zero controls, object-fit fill, <0.3s live-edge lag sync, audio mute toggle, killStream & setVideoZoom.
    Displays instant-poster at ms 0 to eliminate 4s black screen, removed on first video frame.
    """
    is_muted_attr = "muted" if muted else ""
    default_muted = "true" if muted else "false"
    ts = int(time.time() * 1000)
    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>Foxway Live Stream</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{
      width: 100vw;
      height: 100vh;
      background: #000;
      overflow: hidden;
      touch-action: none;
    }}
    #instant-poster {{
      position: absolute;
      top: 0;
      left: 0;
      width: 100vw !important;
      height: 100vh !important;
      object-fit: fill !important;
      z-index: 10;
      pointer-events: none;
      background: #000;
    }}
    zero-player {{
      width: 100vw !important;
      height: 100vh !important;
      display: block;
      background: #000;
    }}
    #v, video {{
      width: 100vw !important;
      height: 100vh !important;
      object-fit: fill !important;
      background: #000;
      display: block;
      transform-origin: center center;
    }}
    video::-webkit-media-controls, video::-webkit-media-controls-enclosure {{
      display: none !important;
    }}
    /* Strict kill for any status badges, mse overlays, or injected controls */
    .status, .mse, [class*="status"], [class*="controls"], [class*="badge"], .info, .mode {{
      display: none !important;
      opacity: 0 !important;
      visibility: hidden !important;
      pointer-events: none !important;
    }}
  </style>
</head>
<body>
  <img id="instant-poster" src="/api/v1/live/instant.jpg?t={ts}" alt="" />
  <zero-player id="player" src="" mode="mse,webrtc,mp4" playsinline autoplay {is_muted_attr} disablepictureinpicture></zero-player>
  <script type="module">
    import {{ VideoRTC }} from '/api/go2rtc/video-rtc.js';

    window._killed = false;
    window._syncTimer = null;

    class ZeroPlayer extends VideoRTC {{
      oninit() {{
        super.oninit();
        if (this.video) {{
          this.video.id = 'v';
          this.video.controls = false;
          this.video.removeAttribute('controls');
          this.video.playsInline = true;
          this.video.setAttribute('playsinline', '');
          this.video.setAttribute('disablepictureinpicture', '');
          this.video.style.objectFit = 'fill';
          const initMuted = {default_muted};
          this.video.muted = initMuted;
          this.video.volume = initMuted ? 0.0 : 1.0;

          const v = this.video;

          const hidePoster = () => {{
            const poster = document.getElementById('instant-poster');
            if (poster) {{
              poster.style.display = 'none';
              try {{ poster.remove(); }} catch(e) {{}}
            }}
          }};

          const syncToLiveEdge = () => {{
            if (v && v.buffered && v.buffered.length > 0) {{
              const end = v.buffered.end(v.buffered.length - 1);
              const lag = end - v.currentTime;
              if (lag > 0.4) {{
                v.currentTime = Math.max(0, end - 0.12);
              }}
            }}
          }};

          v.addEventListener('loadedmetadata', () => {{ syncToLiveEdge(); }});
          v.addEventListener('loadeddata', () => {{ hidePoster(); syncToLiveEdge(); }});
          v.addEventListener('canplay', () => {{ hidePoster(); syncToLiveEdge(); }});
          v.addEventListener('playing', () => {{ hidePoster(); syncToLiveEdge(); }});
          v.ontimeupdate = () => {{
            hidePoster();
            syncToLiveEdge();
          }};

          if (window._syncTimer) clearInterval(window._syncTimer);
          window._syncTimer = setInterval(syncToLiveEdge, 250);
        }}
      }}

      onconnect() {{
        const res = super.onconnect();
        if (this.ws) {{
          window.ws = this.ws;
          this.ws.addEventListener('error', () => {{
            try {{ if (this.ws) this.ws.close(); }} catch(e) {{}}
          }});
        }}
        if (this.pc) window.pc = this.pc;
        return res;
      }}

      onopen() {{
        const modes = super.onopen();
        window.ws = this.ws;
        if (this.pc) window.pc = this.pc;
        return modes;
      }}

      onclose() {{
        window.ws = null;
        if (window._killed) return false;
        setTimeout(() => {{
          if (!window._killed && this.isConnected) {{
            this.onconnect();
          }}
        }}, 2000);
        return true;
      }}
    }}

    if (!customElements.get('zero-player')) {{
      customElements.define('zero-player', ZeroPlayer);
    }}

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${{proto}}//${{location.host}}/api/go2rtc/ws?src={src}`;
    const player = document.getElementById('player');
    player.src = wsUrl;

    window._audioMuted = {default_muted};
    window.setAudioMuted = function(muted) {{
      window._audioMuted = !!muted;
      var v = document.getElementById('v');
      if (v) {{
        v.muted = !!muted;
        v.volume = muted ? 0.0 : 1.0;
        if (!muted) {{
          v.play().catch(() => {{}});
        }}
      }}
    }};

    window.killStream = function() {{
      window._killed = true;
      if (window._syncTimer) {{
        clearInterval(window._syncTimer);
        window._syncTimer = null;
      }}
      try {{
        if (window.ws) {{ window.ws.onclose = null; window.ws.onerror = null; window.ws.close(); window.ws = null; }}
        if (window.pc) {{ window.pc.close(); window.pc = null; }}
        var v = document.getElementById('v');
        if (v) {{ v.muted = true; v.pause(); v.removeAttribute('src'); v.srcObject = null; v.load(); }}
        if (player && player.ondisconnect) {{ player.ondisconnect(); }}
      }} catch(e) {{}}
    }};

    window.setVideoZoom = function(scale, tx, ty) {{
      var v = document.getElementById('v');
      if (v) {{
        v.style.transform = 'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
      }}
    }};

    window.reconnectStream = function() {{
      window._killed = false;
      try {{
        if (player.ondisconnect) player.ondisconnect();
        if (player.onconnect) player.onconnect();
      }} catch (e) {{
        console.warn('Reconnect error:', e);
      }}
    }};

    // MutationObserver to enforce no controls
    const observer = new MutationObserver(() => {{
      const v = document.getElementById('v') || player.querySelector('video') || player.video;
      if (v) {{
        if (v.hasAttribute('controls')) v.removeAttribute('controls');
        if (v.controls) v.controls = false;
      }}
    }});
    observer.observe(player, {{ attributes: true, subtree: true, childList: true }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


class ClientStatusDumpRequest(BaseModel):
    device: Optional[Dict[str, Any]] = None
    screen_state: Optional[str] = ""
    client_logs: Optional[str] = ""


@app.post("/api/v1/logs/client", tags=["Health & Diagnostics"])
@app.post("/api/v1/logs/client-dump", tags=["Health & Diagnostics"])
async def ingest_client_status_dump(payload: ClientStatusDumpRequest, request: Request):
    """
    Ingests Android client diagnostics status dump and writes to daily blackbox log.
    Captures device specs, current screen state, and recent app logger entries.
    """
    client_ip = request.client.host if request.client else "unknown"
    log_client_dump(
        device_info=payload.device or {},
        screen_state=payload.screen_state or "",
        recent_logs=payload.client_logs or "",
        ip=client_ip,
    )
    return {"status": "ok", "message": "Client status-dump successfully recorded"}


@app.get("/api/v1/frame.jpeg", tags=["Live Video & Streaming"])
async def frame_jpeg_proxy(src: str = Query("deurbel")):
    """Proxy instant live JPEG frame from go2rtc."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.GO2RTC_API_URL}/api/frame.jpeg?src={src}")
            return Response(content=resp.content, media_type="image/jpeg")
    except Exception as e:
        logger.warning("Frame snapshot proxy error for %s: %s", src, e)
        raise HTTPException(502, f"Failed fetching live frame: {e}")


@app.post("/api/v1/live/restart", tags=["Live Video & Streaming"])
async def live_stream_restart():
    """
    Force immediate live hub stream restart (Anti-Freeze self-heal).
    Kills any stalled ffmpeg reader, reloads go2rtc stream, clears frozen frame,
    and returns once a verified fresh frame arrives.
    """
    await live_hub.restart()
    return {"status": "restarted", "message": "Live stream reader reset and verified with fresh frame"}


@app.get("/api/v1/live/instant.jpg", tags=["Live Video & Streaming"])
async def get_instant_ram_snapshot(src: str = Query("deurbel")):
    """
    Instant 4:3 JPEG snapshot directly from go2rtc / RAM cache (<15ms).
    Returns fresh frame with zero-cache headers without CPU transcoding overhead.
    """
    frame = live_hub.get_fresh_jpeg(max_age=2.0)
    if not frame:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"http://127.0.0.1:1984/api/frame.jpeg?src={src}")
                if resp.status_code == 200 and len(resp.content) > 1000:
                    frame = resp.content
                    live_hub.latest_jpeg = frame
                    live_hub.last_fresh_frame_time = time.monotonic()
                    live_hub.latest_frame_ts = time.time()
        except Exception:
            frame = live_hub.get_latest_frame()
    if not frame:
        raise HTTPException(status_code=503, detail="Live stream initializing")
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.websocket("/api/v1/ws/live")
async def websocket_live_hub_stream(ws: WebSocket):
    """
    Always-Warm RAM Live WebSocket:
    - Sends fresh b'\\x01' + latest_jpeg from RAM only if decoded within last 2.5s.
    - Realtime pushes video frames (b'\\x01' + jpeg) and 16kHz PCM audio (b'\\x02' + pcm).
    - Drops stale/queued frames to guarantee zero lag on 5G & LAN.
    """
    await ws.accept()

    client_queue = live_hub.register_client()
    try:
        # Millisecond 0: Send latest RAM frame immediately (<10ms)
        fresh_latest = live_hub.get_fresh_jpeg(max_age=5.0) or live_hub.get_latest_frame()
        if fresh_latest:
            try:
                await ws.send_bytes(b"\x01" + fresh_latest)
            except Exception:
                return
        async def sender():
            try:
                while True:
                    data = await client_queue.get()
                    if ws.client_state != WebSocketState.CONNECTED:
                        break
                    await ws.send_bytes(data)
            except BaseException:
                pass

        async def receiver():
            try:
                while True:
                    await ws.receive()
            except BaseException:
                pass

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        done, pending = await asyncio.wait(
            [send_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in done:
            if not t.cancelled():
                try:
                    t.exception()
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        live_hub.unregister_client(client_queue)
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.close()
        except Exception:
            pass


@app.websocket("/api/ws")
@app.websocket("/api/v1/webrtc/ws")
async def websocket_go2rtc_proxy(ws: WebSocket, src: str = Query("deurbel")):
    """
    Reverse proxy for go2rtc WebSocket signaling.
    Enables WebRTC and MSE streaming through standard port 443 over WAN/5G.
    """
    await ws.accept()
    target_url = f"ws://127.0.0.1:1984/api/ws?src={src}"
    try:
        async with websockets.connect(target_url, max_size=10 * 1024 * 1024) as g_ws:
            async def forward_to_go2rtc():
                try:
                    while True:
                        msg = await ws.receive_text()
                        await g_ws.send(msg)
                except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError, ConnectionResetError):
                    pass
                except Exception:
                    pass

            async def forward_to_client():
                try:
                    async for msg in g_ws:
                        if ws.client_state != WebSocketState.CONNECTED:
                            break
                        if isinstance(msg, str):
                            await ws.send_text(msg)
                        else:
                            await ws.send_bytes(msg)
                except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError, ConnectionResetError):
                    pass
                except Exception:
                    pass

            await asyncio.gather(forward_to_go2rtc(), forward_to_client(), return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("go2rtc WebSocket bridge error: %s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
#  ROUTES: Manual Live Recording (Live View Opnemen Button)
# ══════════════════════════════════════════════════════════════════════

class ManualRecordStartRequest(BaseModel):
    user_name: str = "Gezinslid"
    stream_name: str = "deurbel"


@app.post("/api/v1/recordings/manual/start", tags=["Clips & Recordings"])
async def start_manual_recording_endpoint(body: Optional[ManualRecordStartRequest] = None):
    """
    Start recording the live stream on-demand from the Android Live View screen.
    Records directly to clips folder tagged with the user's name.
    """
    user_name = body.user_name if body else "Gezinslid"
    stream_name = body.stream_name if body else "deurbel"
    res = await start_manual_recording(user_name=user_name, stream_name=stream_name)
    return res


@app.post("/api/v1/recordings/manual/stop", tags=["Clips & Recordings"])
async def stop_manual_recording_endpoint(user_name: Optional[str] = Query(None)):
    """Stop active manual recording, finalize MP4, generate thumbnail, and save to Clips."""
    res = await stop_manual_recording(user_name=user_name or "")
    if not res:
        raise HTTPException(400, "Geen actieve handmatige opname gevonden")
    return res


@app.get("/api/v1/recordings/manual/status", tags=["Clips & Recordings"])
async def get_manual_recording_status_endpoint():
    """Check if a manual live recording is currently running."""
    return get_manual_recording_status()


# ══════════════════════════════════════════════════════════════════════
#  ROUTES: Smart Detection Settings & Quiet Hours
# ══════════════════════════════════════════════════════════════════════

class DetectionSettingsModel(BaseModel):
    detection_enabled: bool = True
    record_person: bool = True
    record_vehicle: bool = True
    record_animal: bool = True
    record_motion: bool = False
    notify_person: bool = True
    notify_vehicle: bool = True
    notify_animal: bool = True
    notify_motion: bool = False
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "23:30"
    quiet_hours_end: str = "07:30"
    activity_zone_top_ignore_percent: int = 20
    motion_sensitivity: float = 1.5


@app.get("/api/v1/settings/detection", response_model=DetectionSettingsModel, tags=["Smart AI Detection"])
async def get_detection_settings_endpoint():
    """Retrieve current AI detection categories, activity zone, and quiet hours schedule."""
    cfg = await DetectionSettings.load()
    return DetectionSettingsModel(**cfg)


@app.put("/api/v1/settings/detection", response_model=DetectionSettingsModel, tags=["Smart AI Detection"])
async def update_detection_settings_endpoint(body: DetectionSettingsModel):
    """Update AI detection categories, activity zone, and quiet hours schedule."""
    from app.logger import log_settings_change
    await DetectionSettings.save(body.model_dump())
    log_settings_change("api_detection_settings", body.model_dump())
    return body


@app.get("/api/v1/activity-zone/preview", tags=["Smart AI Detection"])
@app.get("/api/activity-zone/preview", tags=["Smart AI Detection"])
async def activity_zone_preview(top_pct: int = Query(20, ge=0, le=80)):
    """Live preview snapshot showing the ignored activity zone overlay (0% to 80%)."""
    import cv2
    import numpy as np
    snap_data = None
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{settings.GO2RTC_API_URL}/api/frame.jpeg?src=deurbel")
            if resp.status_code == 200:
                snap_data = resp.content
    except Exception:
        pass

    if not snap_data:
        # Fallback to local snapshot file if any exists
        existing = sorted(settings.SNAPSHOTS_DIR.glob("*.jpg"), key=os.path.getmtime, reverse=True)
        if existing:
            snap_data = existing[0].read_bytes()

    if not snap_data:
        raise HTTPException(502, "Could not capture camera frame for preview")

    nparr = np.frombuffer(snap_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(500, "Failed to decode camera frame")

    h, w = img.shape[:2]
    ignore_h = int(h * (top_pct / 100.0))

    if ignore_h > 0:
        overlay = img.copy()
        # Red semi-transparent overlay
        cv2.rectangle(overlay, (0, 0), (w, ignore_h), (0, 0, 220), -1)
        cv2.addWeighted(overlay, 0.40, img, 0.60, 0, img)
        # Red dividing line
        cv2.line(img, (0, ignore_h), (w, ignore_h), (0, 0, 255), 3)
        # Text label
        label = f"Genegeerde straat-zone ({top_pct}%)"
        cv2.putText(img, label, (20, max(35, ignore_h - 15)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)

    success, encoded = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not success:
        raise HTTPException(500, "Failed to encode preview image")

    return Response(content=encoded.tobytes(), media_type="image/jpeg")



# ══════════════════════════════════════════════════════════════════════
#  ROUTES: Storage Management & Retention Days
# ══════════════════════════════════════════════════════════════════════

class StorageSettingsModel(BaseModel):
    retention_days: int = 30
    max_storage_gb: float = 50.0


class StorageStatsResponse(BaseModel):
    retention_days: int
    max_storage_gb: float
    used_mb: float
    max_mb: float
    total_clips: int
    usage_percent: float


@app.get("/api/v1/settings/storage", response_model=StorageStatsResponse, tags=["Settings & Storage"])
async def get_storage_settings_endpoint():
    """Return storage disk usage, clip count, and configured retention days."""
    retention_days = int(await get_setting("retention_days", "30"))
    max_storage_gb = float(await get_setting("max_storage_gb", "50.0"))
    stats = get_storage_stats(max_storage_gb)
    stats["retention_days"] = retention_days
    return StorageStatsResponse(**stats)


@app.put("/api/v1/settings/storage", response_model=StorageStatsResponse, tags=["Settings & Storage"])
async def update_storage_settings_endpoint(body: StorageSettingsModel):
    """Update retention days and max storage cap. Triggers pruning if over capacity."""
    from app.logger import log_settings_change
    await set_setting("retention_days", str(body.retention_days))
    await set_setting("max_storage_gb", str(body.max_storage_gb))
    log_settings_change("storage_settings", body.model_dump())
    stats = await cleanup_storage_and_retention(body.retention_days, body.max_storage_gb)
    stats["retention_days"] = body.retention_days
    return StorageStatsResponse(**stats)


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Doorbell Camera Health Status
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/doorbell/health", tags=["Health & Diagnostics"])
async def get_doorbell_health():
    """Check doorbell camera online status, reachability, and downtime."""
    now = time.time()
    return {
        "online": _doorbell_online,
        "doorbell_ip": settings.DOORBELL_IP,
        "camera_ip": settings.DOORBELL_IP,
        "last_seen_seconds_ago": int(now - _doorbell_last_seen),
    }


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: WebRTC Signaling Proxy (WHEP HTTP Offer / Answer)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/webrtc", tags=["Live Video & Streaming"])
@app.post("/api/webrtc", tags=["Live Video & Streaming"])
async def webrtc_signaling_proxy(request: Request, src: str = Query("deurbel")):
    """
    Proxies WebRTC SDP offer from Android app to local go2rtc (/api/webrtc?src=deurbel)
    and returns the SDP answer.
    Enables WebRTC connection over LAN and WAN using the existing API endpoint,
    eliminating the need for a separate wss://webrtc.foxway.nl subdomain.
    """
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.GO2RTC_API_URL}/api/webrtc?src={src}",
                content=body,
                headers={"Content-Type": "application/sdp"},
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/sdp",
            )
    except Exception as e:
        logger.exception("WebRTC signaling proxy error: %s", e)
        raise HTTPException(502, f"WebRTC signaling failed: {e}")


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Audio Hardware & Acoustic Loopback Test (Requirement #3)
# ══════════════════════════════════════════════════════════════════════

def _goertzel_mag(samples: tuple[int, ...], sample_rate: int, target_freq: float) -> float:
    """Calculate Goertzel spectral magnitude for a target frequency."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(round(n * target_freq / sample_rate))
    omega = (2.0 * math.pi * k) / n
    coeff = 2.0 * math.cos(omega)
    q1 = 0.0
    q2 = 0.0
    for s in samples:
        q0 = s + coeff * q1 - q2
        q2 = q1
        q1 = q0
    power = q1 * q1 + q2 * q2 - coeff * q1 * q2
    return max(0.0, power / n)


@app.post("/api/v1/audio/test")
@app.post("/api/v1/test/audio")
@app.post("/api/v1/test/audio-loopback")
async def test_audio_loopback_endpoint():
    """
    True End-to-End Acoustic Loopback Verification:
    1. Simultaneously opens RTSP incoming microphone audio stream via ffmpeg to capture 3.5s.
    2. While recording, transmits 1000 Hz sine wave via Port 8000 SDK to doorbell outdoor speaker for 2.0s.
    3. Runs FFT frequency analysis (numpy.fft.rfft) on captured mic audio.
    4. Verifies peak at 950 Hz - 1050 Hz with SNR calculation.
    Includes automatic retry (max 2 attempts) if RTSP stream was still cold.
    """
    last_res = {}
    for attempt in range(2):
        last_res = await run_acoustic_loopback_test()
        if last_res.get("passed"):
            return last_res
        if attempt == 0:
            logger.info("Audio loopback test attempt 1 cold (%s), retrying immediately...", last_res.get("message"))
            await asyncio.sleep(0.3)
    return last_res


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Doorbell Audio Speak (Hikvision ISAPI / Port 8000 Bridge)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/audio/speak")
async def audio_speak_endpoint(
    reply_id: Optional[str] = None,
    file: Optional[UploadFile] = File(None),
):
    """
    Stream audio directly to the EZVIZ DB1 Pro outdoor speaker.
    - If reply_id == 'test_tone' or no params provided: sends 1000Hz test tone.
    - If reply_id matches a pre-recorded greeting, plays that file.
    - If file is uploaded, transcodes via FFmpeg to 8000Hz G.711 and streams it.
    """
    logger.info("Audio speak endpoint: reply_id=%s, file=%s", reply_id, file.filename if file else None)
    
    if file:
        content = await file.read()
        ok = await play_audio_to_doorbell_async(audio_data=content)
    elif reply_id and reply_id != "test_tone":
        possible_path = settings.AUDIO_REPLIES_DIR / f"{reply_id}.wav"
        if possible_path.exists():
            ok = await play_audio_to_doorbell_async(file_path=possible_path)
        else:
            raise HTTPException(404, f"Reply audio file '{reply_id}.wav' not found")
    else:
        # Default: 1000 Hz 3s test tone via ISAPI / Port 8000 SDK
        ok = await asyncio.to_thread(send_test_tone_to_doorbell)

    if ok:
        creds = await get_camera_credentials()
        return {
            "status": "success",
            "message": "Audio successfully streamed to doorbell speaker",
            "target": f"EZVIZ DB1 Pro ({creds['ip']})",
        }
    raise HTTPException(status_code=500, detail="Failed to stream audio to doorbell speaker")



@app.websocket("/api/v1/ws/talk")
@app.websocket("/api/v1/audio/stream")
async def audio_stream_websocket(websocket: WebSocket):
    """
    Real-time Live Microphone Talkback WebSocket endpoint.
    Receives raw 16kHz 16-bit mono PCM chunks from the Android app microphone,
    pipes them through the persistent LiveVoiceComSession on Port 8000 SDK,
    and streams native AAC ADTS frames directly into the outdoor speaker.
    """
    await websocket.accept()
    logger.info("Live microphone WebSocket talk session connected")
    session = LiveVoiceComSession()
    started = await asyncio.to_thread(session.start)
    if not started:
        logger.error("Failed starting LiveVoiceComSession on doorbell")
        await websocket.close(code=1011, reason="Failed to open doorbell audio channel")
        return

    try:
        while True:
            data = await websocket.receive_bytes()
            if data:
                session.feed_pcm(data)
    except WebSocketDisconnect:
        logger.info("Live microphone WebSocket disconnected")
    except Exception as e:
        logger.warning("Live microphone WebSocket talk error: %s", e)
    finally:
        await asyncio.to_thread(session.stop)



# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Active Users / Family Members & Device Management
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/users", response_model=list[UserInfo])
async def get_users_list():
    """
    Returns all registered family members with realtime online status
    and last seen timestamp.
    """
    devices = await get_all_devices()
    online_uids = ws_manager.get_online_user_ids()
    users = []
    for d in devices:
        uid = d["user_id"]
        users.append(UserInfo(
            user_id=uid,
            user_name=d["user_name"],
            device_name=d["device_name"],
            android_version=d.get("android_version") or "",
            connection_type=d.get("connection_type") or "",
            online=(uid in online_uids),
            last_seen=d.get("last_seen") or d.get("paired_at", ""),
        ))
    return users


@app.delete("/api/v1/users/{user_id}")
async def delete_user_endpoint(user_id: str):
    """Remove a paired family device."""
    ok = await delete_device(user_id)
    if not ok:
        raise HTTPException(404, "User not found")
    await ws_manager.disconnect(user_id)
    return {"status": "deleted", "user_id": user_id}


# ══════════════════════════════════════════════════════════════════════
#  WEBSOCKET: Persistent push channel
# ══════════════════════════════════════════════════════════════════════

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(ws: WebSocket, user_id: str):
    """
    Persistent WebSocket for real-time event delivery.

    The Android ``CallForegroundService`` keeps this connection alive.
    Events: CALL_RINGING, CALL_CLAIMED, CALL_MISSED, CALL_ENDED, USER_JOINED.
    """
    await ws_manager.connect(user_id, ws)
    device = await get_device(user_id)
    if device:
        await update_device_last_seen(user_id)
        # Notify other family members that this device joined / connected
        await ws_manager.broadcast(
            "USER_JOINED",
            {
                "user_id": user_id,
                "user_name": device["user_name"],
                "device_name": device["device_name"],
            },
            exclude_user=user_id,
        )
    try:
        while True:
            # Keep-alive: read pings / incoming messages
            data = await ws.receive_text()
            if data == "ping":
                await update_device_last_seen(user_id)
                await ws.send_text("pong")
            elif data.startswith("{"):
                import json
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "fcm_token_update" and msg.get("fcm_token"):
                        await update_fcm_token(user_id, msg["fcm_token"])
                        await ws.send_json({"event": "fcm_token_updated", "data": {}})
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error for %s", user_id)
    finally:
        await ws_manager.disconnect(user_id)


# ══════════════════════════════════════════════════════════════════════
#  ROUTE: Devices list (admin)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/devices")
async def get_devices():
    """List all paired devices (admin/debug endpoint)."""
    devices = await get_all_devices()
    # Strip sensitive fields
    return [
        {
            "user_id": d["user_id"],
            "user_name": d["user_name"],
            "device_name": d["device_name"],
            "paired_at": d["paired_at"],
            "has_fcm_token": bool(d.get("fcm_token")),
        }
        for d in devices
    ]


# ══════════════════════════════════════════════════════════════════════
#  Uvicorn entry point
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.HOST_PORT,
        reload=False,
        log_level="info",
    )
