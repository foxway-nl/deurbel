"""
Foxway Smart Doorbell — Atomic Multi-User Call State Machine.

Uses a single ``asyncio.Lock()`` to guarantee that exactly one family
member can claim any given ring, regardless of how many phones are
ringing simultaneously.

States: IDLE → RINGING → IN_CALL → ENDED → (auto) IDLE
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.models import CallState, save_call_log, update_call_log

logger = logging.getLogger("foxway.state_machine")


class ActiveCall:
    """In-memory representation of the current live call."""

    __slots__ = (
        "call_id",
        "state",
        "started_at",
        "claimed_by",
        "claimed_by_user_id",
        "snapshot_path",
        "clip_path",
        "timeout_task",
        "recorder_task",
        "trigger_source",
    )

    def __init__(self, call_id: str, trigger_source: str = "UNKNOWN") -> None:
        self.call_id = call_id
        self.state: CallState = CallState.RINGING
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.claimed_by: Optional[str] = None
        self.claimed_by_user_id: Optional[str] = None
        self.snapshot_path: Optional[str] = None
        self.clip_path: Optional[str] = None
        self.timeout_task: Optional[asyncio.Task] = None
        self.recorder_task: Optional[asyncio.Task] = None
        self.trigger_source: str = trigger_source

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "claimed_by": self.claimed_by,
            "claimed_by_user_id": self.claimed_by_user_id,
            "snapshot_path": self.snapshot_path,
            "clip_path": self.clip_path,
            "trigger_source": self.trigger_source,
        }


class CallStateMachine:
    """
    Thread-safe (asyncio-safe) state machine for doorbell calls.

    Only one call can be active at a time.  The ``asyncio.Lock()``
    serialises every state mutation so that concurrent ``/claim`` or
    ``/ring`` requests are handled atomically.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current: Optional[ActiveCall] = None
        self._last_ring_ts: float = 0.0

        # Callbacks — wired up by main.py at startup
        self.on_ringing: Optional[callable] = None   # (call: ActiveCall) -> Coroutine
        self.on_claimed: Optional[callable] = None   # (call: ActiveCall) -> Coroutine
        self.on_missed: Optional[callable] = None    # (call: ActiveCall) -> Coroutine
        self.on_ended: Optional[callable] = None     # (call: ActiveCall) -> Coroutine
        self.on_start_recording: Optional[callable] = None  # (call_id, clip_path) -> Coroutine => returns Task
        self.on_stop_recording: Optional[callable] = None   # (task) -> Coroutine
        self.on_fetch_snapshot: Optional[callable] = None   # (call_id) -> Coroutine => snapshot path

    @property
    def current_call(self) -> Optional[ActiveCall]:
        return self._current

    # ── RING ──────────────────────────────────────────────────────────

    async def ring(self, force: bool = False, trigger_source: str = "UNKNOWN") -> Optional[ActiveCall]:
        """
        Called by Shelly webhook, C-SDK, Shazam detector, or test button.
        If force=True, bypasses the 15s cooldown (used by the Test Aanbel Trigger button).

        Returns the new ``ActiveCall`` or ``None`` if the ring was
        suppressed by cooldown / existing call.
        """
        import time

        async with self._lock:
            now = time.monotonic()

            if force:
                logger.info("Force ring requested (source=%s) — bypassing all cooldowns and resetting active call", trigger_source)
                if self._current:
                    if self._current.timeout_task and not self._current.timeout_task.done():
                        self._current.timeout_task.cancel()
                    if self._current.recorder_task and not self._current.recorder_task.done():
                        self._current.recorder_task.cancel()
                    self._current = None
                self._last_ring_ts = 0.0

            # If already RINGING (and not forced), retrigger the notification and extend timeout!
            elif self._current and self._current.state == CallState.RINGING:
                logger.info("Call %s is already RINGING — retriggering notification & extending timeout (source=%s)", self._current.call_id, trigger_source)
                call = self._current
                call.trigger_source = trigger_source
                call.started_at = datetime.now(timezone.utc).isoformat()
                self._last_ring_ts = now
                if call.timeout_task and not call.timeout_task.done():
                    call.timeout_task.cancel()
                call.timeout_task = asyncio.create_task(self._ring_timeout(call))
                if self.on_ringing:
                    asyncio.create_task(self.on_ringing(call))
                return call

            # Ignore duplicate presses during cooldown (unless force=True)
            elif (now - self._last_ring_ts < settings.RING_COOLDOWN_SECONDS):
                logger.info("Ring suppressed (cooldown %.1fs remaining, source=%s)", settings.RING_COOLDOWN_SECONDS - (now - self._last_ring_ts), trigger_source)
                return None

            # Already in an active conversation (unless force=True)
            elif self._current and self._current.state == CallState.IN_CALL:
                logger.info("Ring suppressed (active call %s in progress, source=%s)", self._current.call_id, trigger_source)
                return None

            call_id = uuid.uuid4().hex[:12]
            call = ActiveCall(call_id, trigger_source=trigger_source)
            self._current = call
            self._last_ring_ts = now

            logger.info("New call %s — RINGING (source=%s)", call_id, trigger_source)

            # Persist initial call log
            await save_call_log(call_id, CallState.RINGING.value)

        from app.recorder import get_day_clip_dir
        day_dir = get_day_clip_dir()
        clip_path = str(day_dir / f"{call_id}.mp4")
        call.clip_path = clip_path
        call.snapshot_path = str(day_dir / f"thumb_{call_id}.jpg")

        # 1. Fire RINGING callback IMMEDIATELY (broadcast WS + FCM with zero latency)
        if self.on_ringing:
            asyncio.create_task(self.on_ringing(call))

        # 2. Concurrently fetch snapshot and start recorder in background
        async def _async_media_init():
            if self.on_fetch_snapshot:
                try:
                    await self.on_fetch_snapshot(call_id)
                except Exception:
                    logger.exception("Failed to fetch snapshot for %s", call_id)

            from app.recorder import is_recording_active, _active_recording_info
            already_recording = is_recording_active()

            if already_recording:
                active_cid = _active_recording_info.get("clip_id")
                logger.info(
                    "🔔 Doorbell ring while recording active (clip=%s) — merging call %s into existing clip",
                    active_cid, call_id,
                )
                if self.on_start_recording:
                    try:
                        call.recorder_task = await self.on_start_recording(call_id, clip_path, trigger_source=trigger_source)
                    except Exception:
                        logger.exception("Failed to merge recorder for %s", call_id)

                if _active_recording_info.get("video_path"):
                    call.clip_path = _active_recording_info["video_path"]
            else:
                # No recording active: start single record_smart_clip task and create single initial clip row
                try:
                    from app.models import save_clip
                    await save_clip(
                        clip_id=call_id,
                        call_id=call_id,
                        video_path=clip_path,
                        snapshot_path=call.snapshot_path or "",
                        status="RECORDING",
                        category="ring",
                        answered_by=f"Aanbellen [{trigger_source}]",
                        created_at=call.started_at,
                    )
                except Exception:
                    logger.exception("Failed to create initial RECORDING clip for %s", call_id)

                if self.on_start_recording:
                    try:
                        call.recorder_task = await self.on_start_recording(call_id, clip_path, trigger_source=trigger_source)
                    except Exception:
                        logger.exception("Failed to start recorder for %s", call_id)

            await save_call_log(
                call_id, CallState.RINGING.value,
                snapshot_path=call.snapshot_path or "",
                clip_path=call.clip_path or clip_path,
            )

        asyncio.create_task(_async_media_init())

        # Start 35-second timeout
        call.timeout_task = asyncio.create_task(self._ring_timeout(call))

        return call

    async def _ring_timeout(self, call: ActiveCall) -> None:
        """Auto-transition to IDLE after RING_TIMEOUT_SECONDS if nobody claims."""
        try:
            await asyncio.sleep(settings.RING_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return

        async with self._lock:
            if self._current is call and call.state == CallState.RINGING:
                call.state = CallState.ENDED
                self._current = None
                logger.info("Call %s timed out — MISSED", call.call_id)
                await update_call_log(call.call_id, state=CallState.ENDED.value)

        # In v3.5.4, call recordings run autonomously to completion with dynamic motion tracking
        # Recorder task is NOT stopped or cancelled on ring timeout
        if self.on_missed:
            await self.on_missed(call)

    # ── CLAIM ─────────────────────────────────────────────────────────

    async def claim(self, call_id: str, user_id: str, user_name: str) -> dict:
        """
        Atomically claim the ringing call.

        Returns a dict with ``status`` = ``"CLAIMED"`` or ``"ALREADY_CLAIMED"``.
        """
        async with self._lock:
            call = self._current
            if not call or call.call_id != call_id:
                return {"status": "NOT_FOUND"}

            if call.state == CallState.IN_CALL:
                return {
                    "status": "ALREADY_CLAIMED",
                    "answered_by": call.claimed_by,
                }

            if call.state != CallState.RINGING:
                return {"status": "INVALID_STATE", "current_state": call.state.value}

            # ── Transition: RINGING → IN_CALL ──
            call.state = CallState.IN_CALL
            call.claimed_by = user_name
            call.claimed_by_user_id = user_id

            # Cancel the timeout
            if call.timeout_task:
                call.timeout_task.cancel()
                call.timeout_task = None

            logger.info("Call %s claimed by %s (%s) — IN_CALL", call_id, user_name, user_id)
            await update_call_log(
                call_id,
                state=CallState.IN_CALL.value,
                answered_by=user_name,
                answered_by_user_id=user_id,
            )

        # Fire callback OUTSIDE lock
        if self.on_claimed:
            await self.on_claimed(call)

        return {
            "status": "CLAIMED",
            "webrtc_src": settings.GO2RTC_STREAM_NAME,
        }

    # ── HANGUP ────────────────────────────────────────────────────────

    async def hangup(self, call_id: str) -> dict:
        """End the active call and return to IDLE."""
        async with self._lock:
            call = self._current
            if not call or call.call_id != call_id:
                return {"status": "NOT_FOUND"}

            call.state = CallState.ENDED
            self._current = None

            if call.timeout_task:
                call.timeout_task.cancel()
                call.timeout_task = None

            logger.info("Call %s — ENDED (hangup)", call_id)
            await update_call_log(
                call_id,
                state=CallState.ENDED.value,
                ended_at=datetime.now(timezone.utc).isoformat(),
            )

        # In v3.5.4, call recordings run autonomously to completion with dynamic motion tracking
        # Hanging up or rejecting the call on client NEVER stops or cancels the video recording
        if self.on_ended:
            await self.on_ended(call)

        return {"status": "ENDED"}

    # ── QUICK-REPLY (claims + sends audio) ────────────────────────────

    async def quick_reply(self, call_id: str, user_id: str, user_name: str, reply_id: str) -> dict:
        """
        Claim the call on behalf of the user and queue the pre-recorded
        audio reply for backchannel injection.
        """
        result = await self.claim(call_id, user_id, user_name)
        if result["status"] not in ("CLAIMED", "ALREADY_CLAIMED"):
            return result
        # Actual audio injection is handled by the caller (main.py routes)
        return {
            "status": "QUICK_REPLY_SENT",
            "reply_id": reply_id,
            "claimed_by": user_name,
        }

    # ── STATUS ────────────────────────────────────────────────────────

    async def status(self) -> dict:
        """Return current call state snapshot."""
        async with self._lock:
            if self._current:
                return self._current.to_dict()
            return {"state": CallState.IDLE.value}
