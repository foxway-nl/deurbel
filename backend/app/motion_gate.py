"""
Foxway Smart Doorbell — Motion-Gated Inference Module (v3.5.5 - FASE 1).

Features:
1. MOG2 Background Subtraction with Shadow Detection:
   - Filters out shadows (OpenCV shadow class = 127) so sunlight/cloud changes
     on parked cars do not trigger false motion.
   - Morphological opening/closing removes single-pixel noise.
2. Stationary Object Masking:
   - Motion confined strictly inside known stationary vehicle boxes is suppressed.
   - Moving or departing vehicles that cross box boundaries open the gate.
3. Static Camera Assumption & Global Motion Guard:
   - Assumes a static mounted camera (EZVIZ DB1). If >65% of the frame changes
     at once (IR cut filter switch, camera bump), a camera shift warning is flagged.
4. Configurable Parameters & Feature Flag:
   - 'enabled': Default False (safe feature flag).
   - 'min_area_ratio': Minimum area of foreground motion (default 0.003 = 0.3% of frame).
   - 'cooldown_sec': Gate hold time after motion ceases (default 1.5s).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("foxway.motion_gate")


@dataclass
class MotionROI:
    """Bounding box of an active motion region (x1, y1, x2, y2)."""
    x1: int
    y1: int
    x2: int
    y2: int
    area: float
    center: Tuple[float, float]


@dataclass
class MotionGateResult:
    """Output from MotionGate.process_frame()."""
    is_gate_open: bool
    has_active_motion: bool
    motion_ratio: float
    rois: List[MotionROI] = field(default_factory=list)
    motion_mask: Optional[np.ndarray] = None
    camera_shift_warning: bool = False
    reason: str = "INITIAL"


class MotionGater:
    """
    Pre-YOLO Motion Gatekeeper using MOG2 background subtraction.
    Ensures YOLO only processes frames containing genuine physical motion.
    """

    def __init__(
        self,
        enabled: bool = False,
        min_area_ratio: float = 0.003,
        cooldown_sec: float = 1.5,
        var_threshold: int = 25,
        history: int = 300,
        detect_shadows: bool = True,
        global_motion_threshold: float = 0.65,
    ):
        self.enabled = enabled
        self.min_area_ratio = min_area_ratio
        self.cooldown_sec = cooldown_sec
        self.var_threshold = var_threshold
        self.history = history
        self.detect_shadows = detect_shadows
        self.global_motion_threshold = global_motion_threshold

        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=self.history,
            varThreshold=self.var_threshold,
            detectShadows=self.detect_shadows,
        )
        self._kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        self._kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))

        self._last_active_motion_ts: float = 0.0
        self._frame_count: int = 0

    def set_enabled(self, enabled: bool) -> None:
        """Toggle feature flag on or off."""
        self.enabled = enabled
        logger.info("MotionGater enabled set to: %s", self.enabled)

    def reset(self) -> None:
        """Reset background model (e.g. after stream reconnect or day/night switch)."""
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=self.history,
            varThreshold=self.var_threshold,
            detectShadows=self.detect_shadows,
        )
        self._last_active_motion_ts = 0.0
        self._frame_count = 0
        logger.info("MotionGater background model reset")

    def update_config(
        self,
        min_area_ratio: Optional[float] = None,
        cooldown_sec: Optional[float] = None,
        var_threshold: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        """Update runtime configurable thresholds."""
        if enabled is not None:
            self.enabled = enabled
        if min_area_ratio is not None:
            self.min_area_ratio = max(0.0005, min(0.1, min_area_ratio))
        if cooldown_sec is not None:
            self.cooldown_sec = max(0.0, min(10.0, cooldown_sec))
        if var_threshold is not None and var_threshold != self.var_threshold:
            self.var_threshold = var_threshold
            self._subtractor.setVarThreshold(self.var_threshold)

    def process_frame(
        self,
        frame: np.ndarray,
        now: Optional[float] = None,
        stationary_boxes: Optional[List[Tuple[float, float, float, float]]] = None,
        force_open: bool = False,
    ) -> MotionGateResult:
        """
        Process incoming frame through MOG2 background subtractor.

        Args:
            frame: BGR numpy image (e.g. 640x480).
            now: Current timestamp (time.time() if None).
            stationary_boxes: List of (x1, y1, x2, y2) boxes of known parked vehicles.
            force_open: If True (e.g. hardware motion burst active), bypass gating.

        Returns:
            MotionGateResult with is_gate_open, has_active_motion, rois, etc.
        """
        now_ts = now if now is not None else time.time()
        self._frame_count += 1

        if frame is None or frame.size == 0:
            return MotionGateResult(
                is_gate_open=False,
                has_active_motion=False,
                motion_ratio=0.0,
                reason="EMPTY_FRAME",
            )

        h, w = frame.shape[:2]
        total_pixels = float(w * h)
        min_pixels = self.min_area_ratio * total_pixels

        # If feature flag is DISABLED, pass through unconditionally
        if not self.enabled:
            return MotionGateResult(
                is_gate_open=True,
                has_active_motion=True,
                motion_ratio=1.0,
                reason="FEATURE_DISABLED_PASS_THROUGH",
            )

        # If forced open (e.g. camera PIR / Port 8000 hardware motion), bypass
        if force_open:
            self._last_active_motion_ts = now_ts
            # Still apply frame to keep background model learning
            self._subtractor.apply(frame)
            return MotionGateResult(
                is_gate_open=True,
                has_active_motion=True,
                motion_ratio=1.0,
                reason="FORCE_OPEN_HARDWARE_MOTION",
            )

        # 1. Background subtraction
        fg_mask = self._subtractor.apply(frame)

        # 2. Shadow filtering: in MOG2, shadows are value 127; true foreground is 255
        # Threshold at 200 keeps ONLY true foreground motion, completely discarding shadows!
        _, binary_motion = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

        # 3. Morphological cleanup (remove isolated speckle noise, close object bodies)
        cleaned = cv2.morphologyEx(binary_motion, cv2.MORPH_OPEN, self._kernel_open)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, self._kernel_close)

        # 4. Check for global scene shift (statische camera waarschuwing)
        motion_pixel_count = int(np.count_nonzero(cleaned))
        motion_ratio = motion_pixel_count / total_pixels

        camera_shift_warning = False
        if motion_ratio >= self.global_motion_threshold:
            camera_shift_warning = True
            logger.warning(
                "⚠️ [MotionGate] Global camera scene shift detected (%.1f%% of frame moved). "
                "Possible IR-cut switch or camera vibration. Adapting background model.",
                motion_ratio * 100.0,
            )
            # Adapt background model aggressively to absorb global shift
            self._subtractor.apply(frame, learningRate=0.20)
            return MotionGateResult(
                is_gate_open=False,
                has_active_motion=False,
                motion_ratio=motion_ratio,
                motion_mask=cleaned,
                camera_shift_warning=True,
                reason="GLOBAL_CAMERA_SHIFT_SUPPRESSED",
            )

        # 5. Find motion contours
        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        active_rois: List[MotionROI] = []
        stationary_boxes = stationary_boxes or []

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_pixels:
                continue

            x, y, cw, ch = cv2.boundingRect(cnt)
            cx = float(x + cw / 2.0)
            cy = float(y + ch / 2.0)

            # Check if this motion is entirely confined inside a known stationary parked car
            # (e.g. minor sunlight glint on windshield).
            # If the motion expands beyond the parked box, it indicates departure or another object!
            is_inside_parked = False
            for p_box in stationary_boxes:
                px1, py1, px2, py2 = p_box
                # Bounding contour center and edges inside parked box with tolerance
                if px1 <= x and y >= py1 and (x + cw) <= px2 and (y + ch) <= py2:
                    is_inside_parked = True
                    break

            if is_inside_parked:
                # Suppress motion occurring entirely inside stationary car
                continue

            # Add padding to ROI (15px) to give YOLO full context for detection
            pad = 15
            rx1 = max(0, x - pad)
            ry1 = max(0, y - pad)
            rx2 = min(w, x + cw + pad)
            ry2 = min(h, y + ch + pad)

            active_rois.append(MotionROI(
                x1=rx1,
                y1=ry1,
                x2=rx2,
                y2=ry2,
                area=float(area),
                center=(cx, cy),
            ))

        has_active_motion = len(active_rois) > 0

        if has_active_motion:
            self._last_active_motion_ts = now_ts
            is_gate_open = True
            reason = f"MOTION_DETECTED_{len(active_rois)}_ROIS"
        else:
            # Check cooldown hold
            time_since_motion = now_ts - self._last_active_motion_ts
            if time_since_motion < self.cooldown_sec and self._last_active_motion_ts > 0.0:
                is_gate_open = True
                reason = f"COOLDOWN_ACTIVE_{round(self.cooldown_sec - time_since_motion, 1)}s"
            else:
                is_gate_open = False
                reason = "STATIC_SCENE_GATE_CLOSED"

        return MotionGateResult(
            is_gate_open=is_gate_open,
            has_active_motion=has_active_motion,
            motion_ratio=motion_ratio,
            rois=active_rois,
            motion_mask=cleaned,
            camera_shift_warning=camera_shift_warning,
            reason=reason,
        )


# Singleton instance
motion_gater = MotionGater(enabled=False)
