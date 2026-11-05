"""
Foxway Smart Doorbell — Central Ring Coordinator & Coincidence Engine (v3.6.0).

Architecture:
1. Tier 1: Zero-Latency Immediate Trigger (SHAZAM_CHIME only):
   When Shazam Cross-Correlation verifies the 2-tone Ding-Dong melody (728 Hz -> 570 Hz),
   dispatches call_sm.ring(...) immediately at millisecond 0 (trigger_source="SHAZAM_CHIME").
2. Tier 2: Degraded Acoustic + EZVIZ Cloud Burst Coincidence:
   When Shazam detects deurbel frequencies with candidate correlation (NCC >= 0.48) AND
   the Cloud Sniffer detects an EZVIZ Cloud Burst within a [-1.5s, +1.5s] coincidence window,
   dispatches call_sm.ring(trigger_source="SHAZAM + SNIFF").
3. Anti-Motion / Anti-Phantom Protection:
   Network sniffer cloud bursts NEVER trigger a ring on motion or camera activity alone
   (Tier 3 MOTION + SNIFF is permanently eliminated). Sniffer runs in passive shadow mode.
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
    """Central doorbell ring coordinator with 3-tier multi-sensor coincidence engine."""

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

        # Recent acoustic candidates (NCC >= 0.48)
        self._recent_acoustic_candidates: Deque[Dict[str, Any]] = collections.deque(maxlen=30)
        # Recent cloud bursts
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
        """Thread-safe registration of an acoustic candidate with NCC >= 0.48."""
        now = time.time()
        cand = {"ts": now, "ncc": round(float(ncc), 4), "details": str(details)}
        with self._lock:
            self._recent_acoustic_candidates.append(cand)

            # Check if there is a recent cloud burst within last 1.5s waiting for acoustic corroboration
            if (now - self._last_ring_ts) >= self.cooldown_sec:
                for burst in reversed(self._recent_cloud_bursts):
                    if (now - burst["ts"]) <= 1.5:
                        logger.info(
                            "🎉 [RingCoordinator] Tier 2 MATCH (Acoustic arrived after burst): NCC=%.4f + Cloud Burst (delta=%.1fms)",
                            ncc,
                            (now - burst["ts"]) * 1000.0,
                        )
                        self._fire_ring_locked(
                            source="SHAZAM + SNIFF",
                            details={"ncc": ncc, "acoustic_details": details, "burst": burst["details"]},
                            status="COINCIDENCE_TIER2",
                        )
                        break

    def register_cloud_burst(self, burst_details: Dict[str, Any]) -> None:
        """Thread-safe registration of an EZVIZ cloud ring burst from NetworkSniffer."""
        now = time.time()
        burst_record = {"ts": now, "details": burst_details}

        with self._lock:
            self._recent_cloud_bursts.append(burst_record)

            if (now - self._last_ring_ts) < self.cooldown_sec:
                logger.debug("[RingCoordinator] Cloud Burst debounced (in cooldown <%.1fs)", self.cooldown_sec)
                return

            # ── Tier 2: Check Acoustic Coincidence within last 1.5s ──
            matching_cand = None
            for cand in reversed(self._recent_acoustic_candidates):
                if (now - cand["ts"]) <= 1.5 and cand["ncc"] >= 0.48:
                    matching_cand = cand
                    break

            if not matching_cand:
                try:
                    from app.ring_detector import ring_detector
                    live_ncc, live_det = ring_detector.get_recent_acoustic_energy(window_sec=1.5)
                    if live_ncc >= 0.48:
                        matching_cand = {"ts": now, "ncc": live_ncc, "details": live_det}
                except Exception:
                    pass

            if matching_cand:
                logger.info(
                    "🎉 [RingCoordinator] Tier 2 MATCH: Shazam NCC=%.4f + Cloud Burst within 1.5s! Dispatched at ms 0.",
                    matching_cand["ncc"],
                )
                self._fire_ring_locked(
                    source="SHAZAM + SNIFF",
                    details={"ncc": matching_cand["ncc"], "acoustic_details": matching_cand["details"], "burst": burst_details},
                    status="COINCIDENCE_TIER2",
                )
                return

            # ── Motion & Standalone Burst Rejection (Zero Phantom Rings on Motion) ──
            # In accordance with AGENTS.md, cloud packet bursts alone or during motion
            # MUST NEVER trigger a ring without an acoustic chime candidate (Tier 3 eliminated).
            logger.debug(
                "🛡️ [RingCoordinator] Cloud Burst rejected: No acoustic chime candidate within 1.5s (0% phantom rings on motion)."
            )

    def register_signal(self, source: str, details: Optional[Dict[str, Any]] = None) -> None:
        """
        Thread-safe entry point to register a ring signal from either:
        - 'SHAZAM_CHIME' (Tier 1 instant)
        - 'TEST_BUTTON' / 'SHELLY_WEBHOOK'
        """
        now = time.time()
        details = details or {}

        # Network sniffer calls register_cloud_burst directly
        if source == "NETWORK_SNIFF":
            self.register_cloud_burst(details)
            return

        with self._lock:
            # Check cooldown for firing a brand new ring
            if (now - self._last_ring_ts) < self.cooldown_sec:
                logger.info("[RingCoordinator] Ring signal %s ignored due to cooldown (<%.1fs)", source, self.cooldown_sec)
                return

            status = "STANDALONE" if source in ("TEST_BUTTON", "SHELLY_WEBHOOK") else "COMPLETED"
            self._fire_ring_locked(source=source, details=details, status=status)

    def _fire_ring_locked(self, source: str, details: Dict[str, Any], status: str) -> None:
        """Internal helper to dispatch ring while holding self._lock."""
        now = time.time()
        self._last_ring_ts = now
        window = {
            "start_time": now,
            "first_source": source,
            "first_details": details,
            "second_source": None,
            "second_details": None,
            "verified": True,
        }
        self._active_window = window

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
        """Returns the recent ring cross-verification history."""
        with self._lock:
            return list(reversed(self.recent_verifications))


ring_coordinator = RingCoordinator()


def register_ring_signal(source: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Public helper to register ring signal with the coordinator."""
    ring_coordinator.register_signal(source, details)
