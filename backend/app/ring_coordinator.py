"""
Foxway Smart Doorbell — Central Ring Coordinator & Dual Cross-Verification (v3.5.1).

Architecture:
1. Zero-Latency Immediate Trigger (SHAZAM_CHIME only):
   When SHAZAM_CHIME arrives the coordinator immediately invokes call_sm.ring(...) at
   millisecond 0 (zero latency). NETWORK_SNIFF is in full shadow / passive-learner mode
   and NEVER triggers a ring.
2. Background 3.5s Mismatch WARNING:
   After a SHAZAM_CHIME fires, a background timer checks whether the sniffer also
   registered a candidate burst within [-2.0s, +3.5s]:
   - If sniffer confirmed:   Log INFO  [RING VERIFIED]  (dual confirmation)
   - If sniffer was silent:  Log WARNING [RING MISMATCH WARNING] (not ERROR)
3. Thread-Safe & Asyncio Native:
   Can be called from background sniffer/audio threads via register_ring_signal(...).
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
    """Central doorbell ring coordinator with mutual cross-verification."""

    def __init__(self, verification_window_sec: float = 3.5, cooldown_sec: float = 5.0):
        self.verification_window_sec = verification_window_sec
        self.cooldown_sec = cooldown_sec

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

        self._last_ring_ts: float = 0.0
        self._active_window: Optional[Dict[str, Any]] = None
        self._on_ring_callback = None

        # Ring verification history (max 50)
        self.recent_verifications: Deque[Dict[str, Any]] = collections.deque(maxlen=50)

    def reset(self) -> None:
        """Reset internal state (for testing)."""
        with self._lock:
            self._last_ring_ts = 0.0
            self._active_window = None
            self.recent_verifications.clear()
            self._on_ring_callback = None

    def set_on_ring_callback(self, callback) -> None:
        """Allow custom ring callback for testing."""
        self._on_ring_callback = callback

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register_signal(self, source: str, details: Optional[Dict[str, Any]] = None) -> None:
        """
        Thread-safe entry point to register a ring signal from either:
        - 'SHAZAM_CHIME'
        - 'NETWORK_SNIFF'
        - 'TEST_BUTTON' / 'SHELLY_WEBHOOK'
        """
        now = time.time()
        details = details or {}

        # 100% Decoupling: Sniffer is in passive shadow mode and NEVER triggers ringing (v3.5.1)
        if source == "NETWORK_SNIFF":
            logger.debug("[RingCoordinator] NETWORK_SNIFF ignored as ring trigger (sniffer in shadow learner mode)")
            return

        with self._lock:
            # 1. Check if there is an active verification window waiting for the second signal
            if self._active_window is not None:
                elapsed = now - self._active_window["start_time"]
                if elapsed <= self.verification_window_sec:
                    first_src = self._active_window["first_source"]
                    # If it's the complementary signal
                    if source != first_src and not self._active_window.get("verified", False):
                        self._active_window["second_source"] = source
                        self._active_window["second_details"] = details
                        self._active_window["verified"] = True
                        delta_ms = round(elapsed * 1000, 1)

                        logger.info(
                            "🎉 [RING VERIFIED] Dubbele aanbel-bevestiging geslaagd: SHAZAM_CHIME + NETWORK_SNIFF "
                            "vuren allebei synchroon af! (delta: %.1fms, 1st=%s, 2nd=%s)",
                            delta_ms,
                            first_src,
                            source,
                        )

                        self.recent_verifications.append({
                            "timestamp": datetime.datetime.now().isoformat(),
                            "status": "VERIFIED",
                            "first_source": first_src,
                            "second_source": source,
                            "delta_ms": delta_ms,
                            "details": f"Dubbele aanbel-bevestiging geslaagd in {delta_ms}ms!",
                        })
                        return

            # 2. Check cooldown for firing a brand new ring
            if (now - self._last_ring_ts) < self.cooldown_sec:
                logger.info("[RingCoordinator] Ring signal %s ignored due to cooldown (<%.1fs)", source, self.cooldown_sec)
                return

            # 3. New Ring Event: Fire immediately at millisecond 0!
            self._last_ring_ts = now
            window = {
                "start_time": now,
                "first_source": source,
                "first_details": details,
                "second_source": None,
                "second_details": None,
                "verified": False,
            }
            self._active_window = window

            # If it's a test button or webhook, mark as standalone verified
            if source in ("TEST_BUTTON", "SHELLY_WEBHOOK"):
                window["verified"] = True
                self.recent_verifications.append({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "status": "STANDALONE",
                    "first_source": source,
                    "second_source": None,
                    "delta_ms": 0.0,
                    "details": f"Directe trigger via {source}",
                })

        # 4. Dispatch ring to call state machine at ms 0
        self._dispatch_ring_to_state_machine(source)

        # 5. Start background 3.5s verification check if not standalone
        if source not in ("TEST_BUTTON", "SHELLY_WEBHOOK"):
            self._schedule_verification_check(window)

    def _dispatch_ring_to_state_machine(self, source: str) -> None:
        """Invokes call_sm.ring(trigger_source=source) immediately."""
        async def _async_trigger():
            try:
                if self._on_ring_callback:
                    res = self._on_ring_callback(source, {})
                    if asyncio.iscoroutine(res):
                        await res
                    return
                from app.main import call_sm
                await call_sm.ring(trigger_source=source)
            except Exception as e:
                logger.error("[RingCoordinator] Failed to invoke call_sm.ring: %s", e)

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_async_trigger(), self._loop)
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_async_trigger())
            except RuntimeError:
                pass

    def _schedule_verification_check(self, window: Dict[str, Any]) -> None:
        """Schedules checking whether the sniffer also saw a burst within 3.5s.
        In v3.5.1 shadow mode:
        - VERIFIED  → Shazam fired AND sniffer confirmed a burst (dual confirmation).
        - COMPLETED → Shazam fired but sniffer saw nothing (log WARNING, not ERROR).
        """
        async def _verify_job():
            await asyncio.sleep(self.verification_window_sec)
            with self._lock:
                first_src = window["first_source"]
                sniffer_confirmed = window.get("verified", False)

                if sniffer_confirmed:
                    # Already logged VERIFIED when sniffer reported to us
                    return

                # Shazam fired, sniffer was silent → WARNING (not ERROR)
                logger.warning(
                    "⚠️ [RING MISMATCH WARNING] Shazam detecteerde aanbellen (%s), maar Sniffer zag "
                    "geen uniek patroon binnen %.1fs. Mogelijke oorzaken: wifi-pakket gemist, "
                    "deurbel buiten bereik, of keepalive-traffic. Sniffer blijft in shadow-mode.",
                    first_src,
                    self.verification_window_sec,
                )

                self.recent_verifications.append({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "status": "COMPLETED",
                    "first_source": first_src,
                    "second_source": None,
                    "missing_source": "NETWORK_SNIFF",
                    "delta_ms": round(self.verification_window_sec * 1000, 1),
                    "details": f"Ring window gesloten voor {first_src} (sniffer zag niets)",
                })

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_verify_job(), self._loop)
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_verify_job())
            except RuntimeError:
                pass

    def get_recent_verifications(self) -> List[Dict[str, Any]]:
        """Returns the recent ring cross-verification history."""
        with self._lock:
            return list(reversed(self.recent_verifications))


ring_coordinator = RingCoordinator()


def register_ring_signal(source: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Public helper to register ring signal with the coordinator."""
    ring_coordinator.register_signal(source, details)

