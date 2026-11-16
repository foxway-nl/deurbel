"""Foxway Smart Doorbell — Central Ring Coordinator & Isolated Sniffer Logger (v3.7.1)."""
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
    def __init__(self, cooldown_sec: float = 6.0):
        self.cooldown_sec = cooldown_sec
        self.mode: str = "SHAZAM_PRIMARY"
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.RLock()
        self._last_ring_ts: float = 0.0
        self._on_ring_callback = None
        self._active_window: Optional[Dict[str, Any]] = None
        self.verification_window_sec: float = 3.5
        self.recent_verifications: Deque[Dict[str, Any]] = collections.deque(maxlen=50)

    def reset(self) -> None:
        """Reset coordinator state between tests."""
        with self._lock:
            self._last_ring_ts = 0.0
            self.recent_verifications.clear()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_mode(self, mode: str) -> None:
        self.mode = "SHAZAM_PRIMARY" # Geen sniffer mode toegestaan

    def set_on_ring_callback(self, callback) -> None:
        self._on_ring_callback = callback

    def register_cloud_burst(self, burst_details: Dict[str, Any]) -> None:
        """Passive cloud burst tracker stub."""
        pass

    def register_sniffer_trigger(self, sniffer_event: Dict[str, Any]) -> None:
        """PASSIEVE DATA LOGGER: Mag NOOIT een oproep starten!"""
        now = sniffer_event.get("timestamp", time.time())
        sniffer_sig = sniffer_event.get("sig_name", "")
        sniffer_topic = sniffer_event.get("topic", "")
        sniffer_plen = sniffer_event.get("plen", 0)
        sniffer_hex = sniffer_event.get("hex", "")[:64]

        is_both = False
        delta_ms = 0.0
        shazam_ts = 0.0
        if (now - self._last_ring_ts) < 3.5 and self._last_ring_ts > 0:
            is_both = True
            shazam_ts = self._last_ring_ts
            delta_ms = round((now - shazam_ts) * 1000.0, 1)

        event_type = "BOTH_MATCHED" if is_both else "SNIFFER_ONLY"
        faster_method = "SNIFFER" if delta_ms < 0 else "SHAZAM" if is_both else "N/A"

        async def _save():
            try:
                from app.models import save_ring_comparison_trial
                await save_ring_comparison_trial(
                    event_type=event_type, shazam_triggered=1 if is_both else 0, shazam_ts=shazam_ts,
                    sniffer_triggered=1, sniffer_ts=now, sniffer_sig=sniffer_sig, sniffer_topic=sniffer_topic,
                    sniffer_plen=sniffer_plen, sniffer_hex=sniffer_hex, delta_ms=delta_ms,
                    faster_method=faster_method, active_mode="SHAZAM_PRIMARY"
                )
            except Exception as e:
                logger.error("Failed saving sniffer log: %s", e)
        self._dispatch_async_task(_save())

    def register_signal(self, source: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Enige toegangspoort voor ECHTE aanbel-oproepen."""
        now = time.time()
        details = details or {}

        if source == "NETWORK_SNIFF":
            self.register_sniffer_trigger(details)
            return

        if source not in ("SHAZAM_CHIME", "TEST_BUTTON", "SHELLY_WEBHOOK"):
            return

        with self._lock:
            if (now - self._last_ring_ts) < self.cooldown_sec:
                return
            self._last_ring_ts = now
            self.recent_verifications.append({
                "timestamp": datetime.datetime.now().isoformat(),
                "status": "VERIFIED_CHIME",
                "first_source": source,
                "details": f"Geactiveerd via {source}"
            })

            async def _save_shazam():
                try:
                    from app.models import save_ring_comparison_trial
                    await save_ring_comparison_trial(
                        event_type="SHAZAM_ONLY", shazam_triggered=1, shazam_ts=now,
                        shazam_score=details.get("ncc", 0.0), shazam_details=details.get("reason", ""),
                        active_mode="SHAZAM_PRIMARY"
                    )
                except Exception: pass
            self._dispatch_async_task(_save_shazam())

        self._dispatch_ring_to_state_machine(source)

    def _dispatch_async_task(self, coro) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            try: asyncio.run(coro)
            except Exception: pass

    def _dispatch_ring_to_state_machine(self, source: str) -> None:
        if self._on_ring_callback:
            try:
                res = self._on_ring_callback(source, {})
                if asyncio.iscoroutine(res): self._dispatch_async_task(res)
                return
            except Exception: pass

        async def _async_trigger():
            try:
                from app.main import call_sm
                await call_sm.ring(trigger_source=source)
            except Exception as e:
                logger.error("Failed to invoke call_sm.ring: %s", e)
        self._dispatch_async_task(_async_trigger())

ring_coordinator = RingCoordinator()
def register_ring_signal(source: str, details: Optional[Dict[str, Any]] = None) -> None:
    ring_coordinator.register_signal(source, details)
