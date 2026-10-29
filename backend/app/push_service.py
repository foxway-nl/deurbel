"""
Foxway Smart Doorbell — Real-Time Push Service.

Two delivery channels running in parallel:
  1. **WebSocket manager** — persistent connections from Android foreground
     services; lowest latency, used when the app is alive.
  2. **Firebase Cloud Messaging** — high-priority data-only push that wakes
     the device from Doze mode.  Fallback when the app is killed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.config import settings
from app.models import get_all_devices

logger = logging.getLogger("foxway.push")

# ──────────────────────────────────────────────────────────────────────
#  WebSocket Connection Manager
# ──────────────────────────────────────────────────────────────────────


class WebSocketManager:
    """
    Maintains a dict of ``user_id → WebSocket`` connections.
    Supports broadcast to all connected users or targeted send.
    """

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            # Close any existing connection for this user (reconnect scenario)
            old = self._connections.get(user_id)
            if old:
                try:
                    await old.close(code=1012, reason="Replaced by new connection")
                except Exception:
                    pass
            self._connections[user_id] = ws
        logger.info("WS connected: %s (total: %d)", user_id, len(self._connections))

    async def disconnect(self, user_id: str) -> None:
        async with self._lock:
            self._connections.pop(user_id, None)
        logger.info("WS disconnected: %s (total: %d)", user_id, len(self._connections))

    def get_online_user_ids(self) -> set[str]:
        """Return set of currently connected user IDs."""
        return set(self._connections.keys())

    def is_user_online(self, user_id: str) -> bool:
        """Check if a specific user is connected."""
        return user_id in self._connections

    async def send_to_user(self, user_id: str, event: str, data: dict) -> bool:
        """Send a JSON event to a specific user. Returns True if delivered."""
        async with self._lock:
            ws = self._connections.get(user_id)
        if not ws:
            return False
        try:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.send_json({"event": event, "data": data})
                return True
        except Exception:
            logger.warning("Failed to send WS to %s, removing", user_id)
            await self.disconnect(user_id)
        return False

    async def broadcast(self, event: str, data: dict, exclude_user: Optional[str] = None) -> int:
        """
        Broadcast a JSON event to all connected WebSocket clients.
        Returns the number of successful deliveries.
        """
        async with self._lock:
            snapshot = list(self._connections.items())

        delivered = 0
        stale: list[str] = []

        for uid, ws in snapshot:
            if uid == exclude_user:
                continue
            try:
                if ws.application_state == WebSocketState.CONNECTED:
                    await ws.send_json({"event": event, "data": data})
                    delivered += 1
                else:
                    stale.append(uid)
            except Exception:
                stale.append(uid)

        # Clean up dead sockets
        if stale:
            async with self._lock:
                for uid in stale:
                    self._connections.pop(uid, None)

        return delivered

    @property
    def connected_user_ids(self) -> list[str]:
        return list(self._connections.keys())


# Module-level singleton
ws_manager = WebSocketManager()


# ──────────────────────────────────────────────────────────────────────
#  Firebase Cloud Messaging (FCM) — High-Priority Data Push
# ──────────────────────────────────────────────────────────────────────

_firebase_app = None


def _init_firebase() -> bool:
    """Lazily initialise the Firebase Admin SDK. Returns True if ready."""
    global _firebase_app

    if _firebase_app is not None:
        return True

    if not settings.FCM_CREDENTIALS_PATH:
        logger.info("FCM disabled (no credentials path configured)")
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(settings.FCM_CREDENTIALS_PATH)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialised")
        return True
    except Exception:
        logger.exception("Failed to initialise Firebase Admin SDK")
        return False


async def send_fcm_to_all(event: str, data: dict, exclude_user: Optional[str] = None) -> int:
    """
    Send a high-priority FCM **data-only** message to every registered
    device that has a non-empty ``fcm_token``.  Data-only pushes bypass
    Android notification channels and wake ``FirebaseMessagingService``
    directly, which is critical for reliable ring delivery.

    Returns the number of messages sent.
    """
    if not _init_firebase():
        return 0

    try:
        from firebase_admin import messaging
    except ImportError:
        logger.error("firebase_admin.messaging not available")
        return 0

    devices = await get_all_devices()
    tokens = [
        d["fcm_token"]
        for d in devices
        if d["fcm_token"] and d.get("user_id") != exclude_user
    ]

    if not tokens:
        return 0

    # Suppress push notifications during quiet hours or when marked silent / notifiable=False
    # Doorbell ring calls (CALL_RINGING) are ALWAYS dispatched regardless of quiet hours!
    if event != "CALL_RINGING":
        if data.get("in_quiet_hours") or data.get("silent") or data.get("should_notify") is False:
            logger.info("FCM push suppressed for event %s (quiet hours / silent / should_notify=False)", event)
            return 0

    # Convert all values to strings (FCM data payloads are string-only)
    str_data = {k: json.dumps(v) if not isinstance(v, str) else v for k, v in data.items()}
    str_data["event"] = event

    sent = 0
    for token in tokens:
        try:
            message = messaging.Message(
                data=str_data,
                token=token,
                android=messaging.AndroidConfig(
                    priority="high",
                    ttl=30,
                ),
            )
            # Run in executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, messaging.send, message)
            sent += 1
        except Exception:
            logger.warning("FCM send failed for token %s…", token[:20])

    logger.info("FCM sent %d/%d messages for event %s", sent, len(tokens), event)
    return sent


# ──────────────────────────────────────────────────────────────────────
#  Unified push: WS broadcast + FCM fallback
# ──────────────────────────────────────────────────────────────────────


async def push_event(event: str, data: dict, exclude_user: Optional[str] = None) -> None:
    """
    Broadcast *event* to every registered family device via both
    WebSocket (instant) and FCM (wakeup fallback) in parallel.
    """
    ws_count, fcm_count = await asyncio.gather(
        ws_manager.broadcast(event, data, exclude_user=exclude_user),
        send_fcm_to_all(event, data, exclude_user=exclude_user),
    )
    logger.info(
        "Push %s → WS=%d, FCM=%d (excl=%s)",
        event, ws_count, fcm_count, exclude_user,
    )
