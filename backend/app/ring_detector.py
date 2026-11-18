"""
Foxway Smart Doorbell — Doorbell Detection Adapter.
Legacy FFT / 2D-fingerprinting sound logic has been completely eradicated
and replaced by bell_service.py using doorbell_detector.py.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

from app.bell_service import bell_service, BellService

logger = logging.getLogger("foxway.ring_detector")

# Direct alias to modern bell_service
ring_detector = bell_service
chime_detector = bell_service


class ShazamChimeDetector:
    """Backward-compatibility adapter delegating to bell_service."""

    def __init__(self, *args, **kwargs):
        self._delegate = bell_service

    @property
    def _running(self) -> bool:
        return self._delegate._running

    @property
    def cooldown_seconds(self) -> float:
        return self._delegate.cooldown

    @property
    def ncc_trigger_threshold(self) -> float:
        return self._delegate.threshold

    @property
    def fingerprint_path(self) -> str:
        return self._delegate.ref_path

    def get_recent_detections(self) -> List[Dict[str, Any]]:
        return self._delegate.get_recent_detections()

    async def start(self) -> None:
        await self._delegate.start()

    async def stop(self) -> None:
        await self._delegate.stop()


def verify_ding_dong_chime(
    audio_window: np.ndarray,
    sample_rate: int = 16000,
    **kwargs: Any,
) -> Tuple[bool, float, str]:
    """Deprecated FFT stub."""
    return False, 0.0, "Deprecated: replaced by doorbell_detector.py"
