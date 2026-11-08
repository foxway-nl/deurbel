"""
Foxway Smart Doorbell — Central Ring Coordinator (v3.6.2).

Strict Zero-Phantom Architecture:
- ONLY a 100% verified 2-tone Ding-Dong melody ('SHAZAM_CHIME'), 'TEST_BUTTON', or 'SHELLY_WEBHOOK'
  can ever trigger a call.
- Tier 2 ('SHAZAM + SNIFF' degraded coincidence) and Tier 3 ('MOTION + SNIFF') are PERMANENTLY
  REMOVED so that network traffic + children playing/talking outside can NEVER trigger a phantom ring.
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
    """Central doorbell ring coordinator — strict verified-chime only."""

    def __init__(self, verification_window_sec: float = 2.0, cooldown_sec: float = 5.0):
        self.verification_window_sec = verification_window_sec
        self.cooldown_sec = cooldown_sec

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.RLock()

        self._last_ring_ts: float = 0.0
        self._active_window: Optional[Dict[str, Any]] = None
        self._on_ring_callback = None

        # Ring verification history (max 50)
        self.recent_verifications: Deque[Dict[str, Any]] = collections.deque(maxlen=50)

        # Passive telemetry buffers only (NEVER used to trigger a call!)
        self._recent_acoustic_candidates: Deque[Dict[str, Any]] = collections.deque(maxlen=30)
        self._recent_cloud_bursts: Deque[Dict[str, Any]] = collections.deque(maxlen=30)

    def reset(self) -> None:
        """Reset internal state (for testing)."""
        with self._lock:
            self._last_ring_ts = 0.0
            self._active_window = None
            self.recent_verifications.clear()
            self._recent_acoustic_candidates.clear()
            self._recent_cloud_bursts.clear()
            self._on_ring_callback = None

    def set_on_ring_callback(self, callback) -> None:
        """Allow custom ring callback for testing."""
        self._on_ring_callback = callback

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register_acoustic_candidate(self, ncc: float, details: str = "") -> None:
        """Passive telemetry logging ONLY. Never triggers a call."""
        now = time.time()
        with self._lock:
            self._recent_acoustic_candidates.append(
                {"ts": now, "ncc": round(float(ncc), 4), "details": str(details)}
            )

    def register_cloud_burst(self, burst_details: Dict[str, Any]) -> None:
        """Passive telemetry logging ONLY. Network sniffer NEVER triggers or co-triggers a call."""
        now = time.time()
        with self._lock:
            self._recent_cloud_bursts.append({"ts": now, "details": burst_details})

    def register_signal(self, source: str, details: Optional[Dict[str, Any]] = None) -> None:
        """
        Thread-safe entry point to register a ring signal.
        ONLY accepts:
        - 'SHAZAM_CHIME' (after passing all strict Ding-Dong + Fingerprint gates)
        - 'TEST_BUTTON'
        - 'SHELLY_WEBHOOK'
        """
        now = time.time()
        details = details or {}

        if source == "NETWORK_SNIFF":
            self.register_cloud_burst(details)
            return

        if source not in ("SHAZAM_CHIME", "TEST_BUTTON", "SHELLY_WEBHOOK"):
            logger.warning("[RingCoordinator] Ignored unauthorized ring source: %s", source)
            return

        with self._lock:
            if (now - self._last_ring_ts) < self.cooldown_sec:
                logger.info(
                    "[RingCoordinator] Ring signal %s ignored due to cooldown (<%.1fs)",
                    source,
                    self.cooldown_sec,
                )
                return

            status = "STANDALONE" if source in ("TEST_BUTTON", "SHELLY_WEBHOOK") else "VERIFIED_CHIME"
            self._fire_ring_locked(source=source, details=details, status=status)

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

        if source == "SHAZAM_CHIME":
            try:
                from app.network_sniffer import network_sniffer
                network_sniffer.on_shazam_chime_triggered(now)
            except Exception:
                pass

        self._dispatch_ring_to_state_machine(source)

    def _dispatch_ring_to_state_machine(self, source: str) -> None:
        """Invokes call_sm.ring(trigger_source=source) immediately."""
        if self._on_ring_callback:
            try:
                res = self._on_ring_callback(source, {})
                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        asyncio.run(res)
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
                    target_loop.create_task(_async_trigger())
                else:
                    asyncio.run_coroutine_threadsafe(_async_trigger(), target_loop)
            except Exception as run_err:
                logger.error("[RingCoordinator] task dispatch error: %s", run_err)
        else:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_async_trigger())
            except Exception as loop_err:
                logger.error("[RingCoordinator] Could not dispatch ring: no running loop (%s)", loop_err)

    def get_recent_verifications(self) -> List[Dict[str, Any]]:
        """Returns the recent ring verification history."""
        with self._lock:
            return list(reversed(self.recent_verifications))


ring_coordinator = RingCoordinator()


def register_ring_signal(source: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Public helper to register ring signal with the coordinator."""
    ring_coordinator.register_signal(source, details)
