"""
Foxway Smart Doorbell — Central Ring Coordinator & 50-Triggers Benchmark Engine (v3.6.6).

Architectural Modes:
- 'SHAZAM_PRIMARY' (Default):
  * Only a 100% verified 2-tone Ding-Dong melody ('SHAZAM_CHIME'), 'TEST_BUTTON', or 'SHELLY_WEBHOOK'
    can trigger a call.
  * An isolated Sniffer trigger never rings the phone (0 false calls).
  * Measures precise time differences (delta_ms) and stores 'BOTH_MATCHED', 'SHAZAM_ONLY', or
    'SNIFFER_ONLY' records in `ring_comparison_trials` toward the 50-triggers goal.
- 'SNIFFER_PRIMARY':
  * Sniffer starts the call immediately on MQTT :8820 / LAN :8000 ring signatures.
  * Shazam confirms within 3.5s.
- 'DUAL_CONFIRM':
  * Requires both Shazam and Sniffer to match within 2.0s to trigger a call.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import logging
import threading
import time
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("foxway.ring_coordinator")


class RingCoordinator:
    """Central doorbell ring coordinator and benchmark engine."""

    def __init__(self, verification_window_sec: float = 3.5, cooldown_sec: float = 5.0):
        self.verification_window_sec = verification_window_sec
        self.cooldown_sec = cooldown_sec
        self.mode: str = "SHAZAM_PRIMARY"

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.RLock()

        self._last_ring_ts: float = 0.0
        self._active_window: Optional[Dict[str, Any]] = None
        self._on_ring_callback = None

        # Ring verification history (max 50)
        self.recent_verifications: Deque[Dict[str, Any]] = collections.deque(maxlen=50)

        # Sniffer and correlation buffers
        self._recent_sniffer_events: Deque[Dict[str, Any]] = collections.deque(maxlen=50)
        self._active_trial_window: Optional[Dict[str, Any]] = None
        self._active_trial_timer: Optional[asyncio.TimerHandle] = None

        # Passive telemetry buffers
        self._recent_acoustic_candidates: Deque[Dict[str, Any]] = collections.deque(maxlen=30)
        self._recent_cloud_bursts: Deque[Dict[str, Any]] = collections.deque(maxlen=30)

    def reset(self) -> None:
        """Reset internal state (for testing)."""
        with self._lock:
            self._last_ring_ts = 0.0
            self._active_window = None
            if self._active_trial_timer:
                try:
                    self._active_trial_timer.cancel()
                except Exception:
                    pass
                self._active_trial_timer = None
            self._active_trial_window = None
            self._recent_sniffer_events.clear()
            self.recent_verifications.clear()
            self._recent_acoustic_candidates.clear()
            self._recent_cloud_bursts.clear()
            self._on_ring_callback = None
            self.mode = "SHAZAM_PRIMARY"

    def set_mode(self, mode: str) -> None:
        """Set active trigger mode: SHAZAM_PRIMARY, SNIFFER_PRIMARY, or DUAL_CONFIRM."""
        with self._lock:
            if mode in ("SHAZAM_PRIMARY", "SNIFFER_PRIMARY", "DUAL_CONFIRM"):
                self.mode = mode
                logger.info("RingCoordinator mode set to %s", mode)

    def set_on_ring_callback(self, callback) -> None:
        """Allow custom ring callback for testing."""
        self._on_ring_callback = callback

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register_acoustic_candidate(self, ncc: float = 1.0, details: str = "", **kwargs) -> None:
        """Passive telemetry logging or forward to register_signal if candidate verified."""
        score = kwargs.get("score", ncc)
        reason = kwargs.get("reason", details)
        now = kwargs.get("timestamp", time.time())
        with self._lock:
            self._recent_acoustic_candidates.append(
                {"ts": now, "ncc": round(float(score), 4), "details": str(reason)}
            )

    def register_cloud_burst(self, burst_details: Dict[str, Any]) -> None:
        """Passive telemetry logging."""
        now = time.time()
        with self._lock:
            self._recent_cloud_bursts.append({"ts": now, "details": burst_details})

    def register_sniffer_trigger(self, sniffer_event: Dict[str, Any]) -> None:
        """
        Invoked by network_sniffer when an EZVIZ MQTT :8820 or LAN :8000 ring signature matches.
        """
        now = sniffer_event.get("timestamp", time.time())
        sniffer_event["timestamp"] = now

        with self._lock:
            self._recent_sniffer_events.append(sniffer_event)
            mode = self.mode

            # Check if an active trial window was started by Shazam and is waiting for Sniffer
            if self._active_trial_window is not None:
                tw = self._active_trial_window
                if tw.get("shazam") is not None and tw.get("sniffer") is None:
                    shazam_ts = tw["shazam"]["timestamp"]
                    if (now - shazam_ts) <= self.verification_window_sec:
                        tw["sniffer"] = sniffer_event
                        if self._active_trial_timer:
                            self._active_trial_timer.cancel()
                            self._active_trial_timer = None
                        self._complete_trial_locked("BOTH_MATCHED", tw)
                        self._active_trial_window = None

                        if mode == "DUAL_CONFIRM" and (now - self._last_ring_ts) >= self.cooldown_sec:
                            self._fire_ring_locked(source="SHAZAM + SNIFF", details=sniffer_event, status="DUAL_CONFIRMED")
                        return

            # If mode is SNIFFER_PRIMARY, start the call immediately!
            if mode == "SNIFFER_PRIMARY":
                if (now - self._last_ring_ts) >= self.cooldown_sec:
                    self._fire_ring_locked(source="NETWORK_SNIFF", details=sniffer_event, status="SNIFFER_PRIMARY")

            # Check if Shazam already triggered within the last 2.0s
            recent_shazam = None
            if self._active_window and self._active_window.get("first_source") == "SHAZAM_CHIME":
                st = self._active_window.get("start_time", 0.0)
                if 0.0 <= (now - st) <= 2.0:
                    recent_shazam = {
                        "timestamp": st,
                        "score": self._active_window.get("first_details", {}).get("ncc", 1.0),
                        "details": self._active_window.get("first_details", {}).get("reason", "SHAZAM_CHIME"),
                    }

            if recent_shazam is not None:
                tw = {
                    "shazam": recent_shazam,
                    "sniffer": sniffer_event,
                    "start_ts": now,
                    "mode": mode,
                }
                self._complete_trial_locked("BOTH_MATCHED", tw)
                return

            # Open correlation window waiting for possible Shazam
            tw = {
                "sniffer": sniffer_event,
                "shazam": None,
                "start_ts": now,
                "mode": mode,
            }
            self._active_trial_window = tw
            self._schedule_trial_timeout(tw, timeout_sec=self.verification_window_sec, expire_type="SNIFFER_ONLY")

    def register_signal(self, source: str, details: Optional[Dict[str, Any]] = None) -> None:
        """
        Thread-safe entry point to register a ring signal.
        Accepts 'SHAZAM_CHIME', 'TEST_BUTTON', 'SHELLY_WEBHOOK', 'NETWORK_SNIFF'.
        """
        now = time.time()
        details = details or {}

        if source == "NETWORK_SNIFF":
            self.register_sniffer_trigger(details)
            return

        if source not in ("SHAZAM_CHIME", "TEST_BUTTON", "SHELLY_WEBHOOK"):
            logger.warning("[RingCoordinator] Ignored unauthorized ring source: %s", source)
            return

        with self._lock:
            mode = self.mode

            if source in ("TEST_BUTTON", "SHELLY_WEBHOOK"):
                if (now - self._last_ring_ts) < self.cooldown_sec:
                    return
                self._fire_ring_locked(source=source, details=details, status="STANDALONE")
                return

            # Handle SHAZAM_CHIME
            ts = details.get("timestamp", now)
            shazam_event = {
                "timestamp": ts,
                "score": details.get("ncc", details.get("score", 1.0)),
                "freq": details.get("freq", 728.0),
                "details": details.get("reason", "SHAZAM_CHIME"),
            }

            # If active trial window was started by sniffer and waiting for Shazam:
            if self._active_trial_window is not None:
                tw = self._active_trial_window
                if tw.get("sniffer") is not None and tw.get("shazam") is None:
                    sniffer_ts = tw["sniffer"]["timestamp"]
                    if (ts - sniffer_ts) <= self.verification_window_sec:
                        tw["shazam"] = shazam_event
                        if self._active_trial_timer:
                            self._active_trial_timer.cancel()
                            self._active_trial_timer = None
                        self._complete_trial_locked("BOTH_MATCHED", tw)
                        self._active_trial_window = None

                        if mode in ("SHAZAM_PRIMARY", "DUAL_CONFIRM") and (now - self._last_ring_ts) >= self.cooldown_sec:
                            src = "SHAZAM + SNIFF" if mode == "DUAL_CONFIRM" else "SHAZAM_CHIME"
                            self._fire_ring_locked(source=src, details=details, status="BOTH_MATCHED")
                        return

            # In SHAZAM_PRIMARY mode, ring immediately on millisecond 0!
            if mode == "SHAZAM_PRIMARY":
                if (now - self._last_ring_ts) >= self.cooldown_sec:
                    self._fire_ring_locked(source="SHAZAM_CHIME", details=details, status="VERIFIED_CHIME")

            # Check if sniffer triggered within recent [-2.0s, 0.0s]
            recent_sniff = None
            for se in reversed(self._recent_sniffer_events):
                if 0.0 <= (now - se["timestamp"]) <= 2.0:
                    recent_sniff = se
                    break

            if recent_sniff is not None:
                tw = {
                    "shazam": shazam_event,
                    "sniffer": recent_sniff,
                    "start_ts": now,
                    "mode": mode,
                }
                self._complete_trial_locked("BOTH_MATCHED", tw)
            else:
                # Open window waiting for sniffer
                tw = {
                    "shazam": shazam_event,
                    "sniffer": None,
                    "start_ts": now,
                    "mode": mode,
                }
                self._active_trial_window = tw
                self._schedule_trial_timeout(tw, timeout_sec=self.verification_window_sec, expire_type="SHAZAM_ONLY")

    def _schedule_trial_timeout(self, tw: dict, timeout_sec: float, expire_type: str) -> None:
        """Schedules timeout to finalize an unpaired trial."""
        def _timeout_cb():
            with self._lock:
                if self._active_trial_window is tw:
                    self._complete_trial_locked(expire_type, tw)
                    self._active_trial_window = None
                    self._active_trial_timer = None

        target_loop = self._loop
        if target_loop is None or not target_loop.is_running():
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        if target_loop and target_loop.is_running():
            self._active_trial_timer = target_loop.call_later(timeout_sec, _timeout_cb)

    def _complete_trial_locked(self, event_type: str, tw: dict) -> None:
        """Completes and persists a comparison trial."""
        shazam = tw.get("shazam") or {}
        sniffer = tw.get("sniffer") or {}
        shazam_ts = shazam.get("timestamp", 0.0)
        sniffer_ts = sniffer.get("timestamp", 0.0)
        delta_ms = 0.0
        faster = "N/A"

        if event_type == "BOTH_MATCHED" and shazam_ts > 0.0 and sniffer_ts > 0.0:
            delta_ms = round((sniffer_ts - shazam_ts) * 1000.0, 1)
            faster = "SNIFFER" if delta_ms < 0 else "SHAZAM"

        active_mode = tw.get("mode", self.mode)

        async def _save():
            try:
                from app.models import save_ring_comparison_trial
                await save_ring_comparison_trial(
                    event_type=event_type,
                    shazam_triggered=1 if shazam else 0,
                    shazam_ts=shazam_ts,
                    shazam_score=shazam.get("score", 0.0),
                    shazam_details=shazam.get("details", ""),
                    sniffer_triggered=1 if sniffer else 0,
                    sniffer_ts=sniffer_ts,
                    sniffer_sig=sniffer.get("sig_name") or sniffer.get("signature", ""),
                    sniffer_topic=sniffer.get("topic", ""),
                    sniffer_plen=sniffer.get("plen") or sniffer.get("bytes_len", 0),
                    sniffer_hex=(sniffer.get("hex") or sniffer.get("hex_preview", ""))[:64],
                    delta_ms=delta_ms,
                    faster_method=faster,
                    active_mode=active_mode,
                )
                logger.info(
                    "🏆 [Ring Trial Saved] Type=%s, Delta=%.1fms, Faster=%s, Mode=%s",
                    event_type, delta_ms, faster, active_mode,
                )
            except Exception as e:
                logger.error("Failed saving ring comparison trial: %s", e)

        self._dispatch_async_task(_save())

    def _fire_ring_locked(self, source: str, details: Dict[str, Any], status: str) -> None:
        """Internal helper to dispatch ring while holding self._lock."""
        now = time.time()
        self._last_ring_ts = now
        self._active_window = {
            "start_time": now,
            "first_source": source,
            "first_details": details,
            "second_source": None,
            "second_details": None,
            "verified": True,
        }

        self.recent_verifications.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "status": status,
            "first_source": source,
            "second_source": None,
            "delta_ms": 0.0,
            "details": f"Geactiveerd via {source} ({status})",
        })

        if source in ("SHAZAM_CHIME", "SHAZAM + SNIFF"):
            try:
                from app.network_sniffer import network_sniffer
                network_sniffer.on_shazam_chime_triggered(now)
            except Exception:
                pass

        self._dispatch_ring_to_state_machine(source)

    def _dispatch_async_task(self, coro) -> None:
        """Dispatches an async coroutine to the coordinator event loop."""
        target_loop = self._loop
        if target_loop is None or not target_loop.is_running():
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        if target_loop and target_loop.is_running():
            try:
                current_loop = None
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass

                if current_loop is target_loop:
                    target_loop.create_task(coro)
                else:
                    asyncio.run_coroutine_threadsafe(coro, target_loop)
            except Exception as e:
                logger.debug("Failed dispatching async task: %s", e)
        else:
            try:
                asyncio.run(coro)
            except Exception:
                pass

    def _dispatch_ring_to_state_machine(self, source: str) -> None:
        """Invokes call_sm.ring(trigger_source=source) immediately."""
        if self._on_ring_callback:
            try:
                res = self._on_ring_callback(source, {})
                if asyncio.iscoroutine(res):
                    self._dispatch_async_task(res)
                return
            except Exception as e:
                logger.error("[RingCoordinator] Callback error: %s", e)
                return

        async def _async_trigger():
            try:
                from app.main import call_sm
                await call_sm.ring(trigger_source=source)
            except Exception as e:
                logger.error("[RingCoordinator] Failed to invoke call_sm.ring: %s", e)

        self._dispatch_async_task(_async_trigger())

    def get_recent_verifications(self) -> List[Dict[str, Any]]:
        """Returns the recent ring verification history."""
        with self._lock:
            return list(reversed(self.recent_verifications))


ring_coordinator = RingCoordinator()


def register_ring_signal(source: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Public helper to register ring signal with the coordinator."""
    ring_coordinator.register_signal(source, details)
