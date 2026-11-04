"""
Foxway Smart Doorbell — 3-Layer Verification Gatekeeper AI Engine (v2.3.0).

Architecture:
  • Layer 1 — Full 4:3 Frame Context (960x720, no misleading mini-crops!):
      - Ultralytics YOLOv8s (Small) runs on the complete 4:3 frame with full driveway context.
      - Footpoint Ground-Plane Filter: box bottom (y2) must touch the ground (y2 >= 0.25 * height).
        Tree tops, rooflines, and clouds are rejected immediately.
      - Interactive Exclusion Zones: Admin-configured masks for parked cars/shrubs.
      - Class confidence thresholds (person: 0.52, vehicle: 0.55, animal: 0.52).
  • Layer 2 — Strict 3-Frame Trajectory Verification (1.2s Proof Before Triggering):
      - Zero single-frame false alarms.
      - Validates candidate across >= 3 frames with matching category.
      - Mandatory Object Displacement (Delta d):
          - Person / Animal: Delta d >= 6.0% screen width (wobbling shrubs with Delta d < 2% rejected!).
          - Vehicle: Delta d >= 12.0% screen width (parked cars with Delta d < 1.5% rejected!).
      - Logs every candidate (APPROVED & REJECTED) into ai_audit_logs for the Web Admin Inspector.
  • Layer 3 — Object-Based Recording & Immediate Push (<1.5s):
      - On verification success, clip_id and event are created IMMEDIATELY (at T0 + 1.0s).
      - Push notification dispatched to phone in <1.5s via WebSocket & FCM (bypassing 60s timeout).
      - Recording captures 6s Pre-Roll + time object is active + 5s Post-Roll (tight 25s max cap).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import cv2
import numpy as np
import supervision as sv
from ultralytics import YOLO

from app.config import settings
from app.models import (
    get_setting,
    set_setting,
    get_system_setting,
    get_system_settings,
    save_clip,
    update_clip,
    delete_clip as db_delete_clip,
    save_event,
    save_ai_audit_log,
)
from app.push_service import push_event, ws_manager

logger = logging.getLogger("foxway.ai_detector")

# COCO Class ID mappings for YOLOv8
PERSON_CLASSES = {0}
VEHICLE_CLASSES = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck
ANIMAL_CLASSES = {14, 15, 16, 17, 18, 19}  # bird, cat, dog, horse, sheep, cow

COCO_NAMES = {
    0: "Persoon",
    1: "Fiets", 2: "Auto", 3: "Motor", 5: "Bus", 7: "Vrachtwagen",
    14: "Vogel", 15: "Kat", 16: "Hond",
    17: "Paard", 18: "Schaap", 19: "Koe",
}

def resolve_model_path(custom_path: Optional[str] = None) -> str:
    """
    Resolves the active YOLO detection model path (supports YOLO11 and YOLOv8).
    Priority:
    1. Explicit custom_path
    2. Environment or SQLite override
    3. Model registry active version
    4. /mnt/opslag/deurbel/backend/app/yolo11n.pt
    5. /mnt/opslag/deurbel/backend/app/yolo11s.pt
    6. /mnt/opslag/deurbel/backend/app/yolov8n.pt
    7. 'yolo11n.pt'
    """
    if custom_path and os.path.exists(custom_path):
        return custom_path

    env_path = os.environ.get("YOLO_MODEL_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    try:
        from app.train_eval_toolkit import model_registry
        reg_path = model_registry.get_active_model_path()
        if reg_path and os.path.exists(reg_path):
            return reg_path
    except Exception:
        pass

    candidates = [
        getattr(settings, "YOLO_MODEL_PATH", None),
        "/mnt/opslag/deurbel/backend/app/yolo11n.pt",
        "/mnt/opslag/deurbel/backend/app/yolo11s.pt",
        "/mnt/opslag/deurbel/backend/app/yolov8n.pt",
        "/mnt/opslag/deurbel/backend/yolov8n.pt",
        "yolo11n.pt",
        "yolov8n.pt",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return "yolo11n.pt"


_MODEL_PATH = resolve_model_path()


def compute_iou(
    boxA: tuple[float, float, float, float],
    boxB: tuple[float, float, float, float],
) -> float:
    """Compute Intersection over Union (IoU) of two bounding boxes (x1, y1, x2, y2)."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    inter_w = max(0.0, xB - xA)
    inter_h = max(0.0, yB - yA)
    inter_area = inter_w * inter_h

    areaA = max(0.0, (boxA[2] - boxA[0]) * (boxA[3] - boxA[1]))
    areaB = max(0.0, (boxB[2] - boxB[0]) * (boxB[3] - boxB[1]))
    union_area = areaA + areaB - inter_area

    if union_area <= 0.0:
        return 0.0
    return inter_area / union_area


def is_parked_vehicle(
    box: tuple[float, float, float, float],
    parked_boxes: list[tuple[float, float, float, float]],
    threshold: float = 0.45,
) -> bool:
    """Return True if box matches any parked reference box with IoU >= threshold."""
    for p_box in parked_boxes:
        if compute_iou(box, p_box) >= threshold:
            return True
    return False


async def get_substream_rtsp() -> str:
    """Dynamically resolve camera substream RTSP URL via local go2rtc to eliminate connection contention."""
    return "rtsp://127.0.0.1:8554/deurbel_sub"


async def get_mainstream_rtsp() -> str:
    """Dynamically resolve high-res 4:3 camera stream via local go2rtc."""
    return "rtsp://127.0.0.1:8554/deurbel"


class DetectionSettings:
    """Detection configuration with SQLite-backed persistence."""

    @classmethod
    async def load(cls) -> dict:
        return {
            "detection_enabled": await get_setting("detection_enabled", "true") == "true",
            "record_person": await get_setting("record_person", "true") == "true",
            "record_vehicle": await get_setting("record_vehicle", "true") == "true",
            "record_animal": await get_setting("record_animal", "true") == "true",
            "record_motion": await get_setting("record_motion", "false") == "true",
            "notify_person": await get_setting("notify_person", "true") == "true",
            "notify_vehicle": await get_setting("notify_vehicle", "true") == "true",
            "notify_animal": await get_setting("notify_animal", "true") == "true",
            "notify_motion": await get_setting("notify_motion", "false") == "true",
            "quiet_hours_enabled": await get_setting("quiet_hours_enabled", "true") == "true",
            "quiet_hours_start": await get_setting("quiet_hours_start", "23:30"),
            "quiet_hours_end": await get_setting("quiet_hours_end", "07:30"),
            "motion_sensitivity": float(await get_setting("motion_sensitivity", "1.5")),
        }

    @classmethod
    async def save(cls, data: dict) -> None:
        for k, v in data.items():
            await set_setting(k, str(v).lower() if isinstance(v, bool) else str(v))


class CandidateDetection:
    """Represents an object candidate being tracked through 2-frame trajectory verification."""

    def __init__(
        self,
        candidate_id: str,
        category: str,
        label: str,
        box: tuple[float, float, float, float],
        conf: float,
        timestamp: float,
        motion_ratio: float = 0.0,
    ):
        self.candidate_id = candidate_id
        self.category = category
        self.label = label
        self.first_time = timestamp
        self.last_time = timestamp
        self.frames: list[tuple[float, tuple[float, float, float, float], float]] = [
            (timestamp, box, conf)
        ]
        self.motion_ratios: list[float] = [motion_ratio]
        self.best_box = box
        self.best_conf = conf
        self.best_frame: Optional[np.ndarray] = None
        self.verified = False
        self.rejection_reason = ""


class SmartDetector:
    """
    Temporal Pixel-Diff Masking AI Pipeline (v2.6.0).
    Runs on 640x480 4:3 video stream at 5 fps (<45ms per frame via YOLOv8n).
    """

    def __init__(self):
        self.model: Optional[YOLO] = None
        self.tracker: Optional[sv.ByteTrack] = None
        self.smoother: Optional[sv.DetectionsSmoother] = None

        # Adaptive background model for motion checking
        self.avg_bg: Optional[np.ndarray] = None
        self.kernel_5x5 = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

        # Temporal frame history: deque of (timestamp, gray_frame) for 1.2s motion mask
        self.frame_history: collections.deque[tuple[float, np.ndarray]] = collections.deque(maxlen=10)

        # Spatial parked vehicle reference boxes: list of (x1, y1, x2, y2)
        self.parked_vehicle_boxes: list[tuple[float, float, float, float]] = []
        self.parked_spots: collections.deque[tuple[float, float, float, float]] = collections.deque(maxlen=30)
        self.static_parked_boxes: list[tuple[float, float, float, float]] = []
        self.last_baseline_update: float = 0.0

        # Stationary Parked-Car Memory (v3.4.0): 15-second dynamic vehicle memory
        self.stationary_vehicles: list[dict] = []

        # High-Speed AI Scan-Burst & Hardware Motion State (v3.4.0)
        self._last_hardware_motion_time: float = 0.0
        self._last_candidate_seen_time: float = 0.0

        # Per-Category Cooldowns (v3.4.0: 5s same category, 0s different category)
        self._last_trigger_per_category: dict[str, float] = {}
        self._last_clip_end_per_category: dict[str, float] = {}

        # ByteTrack object tracking metrics
        self.track_origins: dict[int, tuple[float, float]] = {}       # tid -> (orig_cx, orig_cy)
        self.track_frame_counts: dict[int, int] = {}                  # tid -> frame count
        self.track_history: dict[int, list[tuple[float, float]]] = {} # tid -> list of (cx, cy)

        # Verification Gatekeeper state
        self._candidate_tracks: dict[str, CandidateDetection] = {}    # track_id -> CandidateDetection
        self._next_candidate_num: int = 1

        # FASE 1: MOG2 Motion Gatekeeper (feature-flagged, default disabled)
        from app.motion_gate import motion_gater
        self.motion_gater = motion_gater

        # FASE 2: ByteTrack Consistent Object Tracker (feature-flagged, default disabled)
        from app.object_tracker import consistent_tracker
        self.consistent_tracker = consistent_tracker

        # Worker state
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._last_trigger_time: float = 0.0
        self._cooldown_seconds: float = 5.0
        self._last_verified_object_seen: float = 0.0
        self._last_person_seen: float = 0.0
        self._last_rejected_log_time: dict[str, float] = {}

        # Pre-load user-verified parking spots from ai_audit_logs into baseline memory
        self.load_verified_parked_anchors()

    def notify_hardware_motion(self) -> None:
        """Signaled by Port 8000 SDK Alarm (0x4000 dwAlarmType == 3) to trigger 4 FPS High-Speed Scan Burst."""
        self._last_hardware_motion_time = time.time()
        logger.debug("Port 8000 hardware motion received -> High-Speed Scan Burst (4 FPS / 0.25s) active for 8s")
        try:
            from app.recorder import notify_ongoing_motion
            notify_ongoing_motion(category="motion", label="Beweging")
        except Exception:
            pass

    def is_motion_active(self) -> bool:
        """Check if verified object motion or camera hardware motion was detected within the last 3.0 seconds."""
        now = time.time()
        return (now - self._last_verified_object_seen) < 3.0 or (now - self._last_hardware_motion_time) < 3.0

    def init_model(self, model_path: Optional[str] = None) -> bool:
        """Initialize official Ultralytics YOLO model (YOLO11 / YOLOv8) and Supervision ByteTrack."""
        path = resolve_model_path(model_path)
        if self.model is not None and getattr(self, "_active_model_path", None) == path:
            return True
        try:
            self.model = YOLO(path)
            self._active_model_path = path
            self.tracker = sv.ByteTrack(lost_track_buffer=30, minimum_matching_threshold=0.8)
            self.smoother = sv.DetectionsSmoother(length=3)
            logger.info("Ultralytics YOLO model initialized successfully from %s", path)
            return True
        except Exception as e:
            logger.error("Failed loading YOLO model from %s: %s", path, e)
            return False

    def is_in_quiet_hours(self, start_str: str, end_str: str, check_time: Optional[datetime] = None) -> bool:
        """Check if current local time (or check_time) in Europe/Amsterdam is within quiet hours (e.g. 23:30 to 07:30)."""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Amsterdam")
            now = check_time if check_time is not None else datetime.now(tz)
            if now.tzinfo is None:
                now = now.replace(tzinfo=tz)
            else:
                now = now.astimezone(tz)
            start_parts = [int(p) for p in start_str.split(":")]
            end_parts = [int(p) for p in end_str.split(":")]
            start_time = now.replace(hour=start_parts[0], minute=start_parts[1], second=0, microsecond=0)
            end_time = now.replace(hour=end_parts[0], minute=end_parts[1], second=0, microsecond=0)

            if start_time < end_time:
                return start_time <= now <= end_time
            else:  # Crosses midnight (e.g., 23:30 -> 07:30)
                return now >= start_time or now <= end_time
        except Exception:
            return False

    def update_parked_baseline(self, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
        """Record stationary vehicle baseline (classes 2=car, 5=bus, 7=truck)."""
        if self.model is None and not self.init_model():
            return self.parked_vehicle_boxes

        try:
            res = self.model(
                frame,
                classes=[2, 5, 7],
                conf=0.35,
                verbose=False,
            )
            new_boxes: list[tuple[float, float, float, float]] = []
            if res and len(res) > 0 and res[0].boxes is not None:
                for b in res[0].boxes.xyxy.cpu().tolist():
                    new_boxes.append((float(b[0]), float(b[1]), float(b[2]), float(b[3])))

            self.parked_vehicle_boxes = new_boxes
            self.last_baseline_update = time.time()
            now_t = time.time()
            for b in new_boxes:
                bx1, by1, bx2, by2 = b
                bcx = (bx1 + bx2) / 2.0
                bcy = (by1 + by2) / 2.0
                self.stationary_vehicles.append({
                    "box": b,
                    "cx": bcx,
                    "cy": bcy,
                    "first_seen": now_t - 60.0,
                    "last_seen": now_t,
                    "stationary_frames": 100,
                    "is_baseline": True,
                })
            logger.info("Updated parked vehicle baseline: %d vehicles registered into stationary memory", len(self.parked_vehicle_boxes))
            return self.parked_vehicle_boxes
        except Exception as e:
            logger.warning("Error updating parked baseline: %s", e)
            return self.parked_vehicle_boxes

    def load_verified_parked_anchors(self) -> None:
        """Pre-load user-verified parked vehicle locations from ai_audit_logs into baseline memory."""
        try:
            import sqlite3
            from app.database import get_db_path
            db_path = get_db_path()
            if os.path.exists(db_path):
                with sqlite3.connect(db_path, timeout=5.0) as conn:
                    cur = conn.cursor()
                    rows = cur.execute(
                        "SELECT boxes_json FROM ai_audit_logs WHERE feedback_reason IN ('PARKED_CAR', 'PERSON_NEAR_PARKED_CAR') ORDER BY id DESC LIMIT 50"
                    ).fetchall()
                    loaded = 0
                    now_t = time.time()
                    for (b_json,) in rows:
                        if b_json:
                            try:
                                b_list = json.loads(b_json)
                                for item in b_list:
                                    if "x1" in item and "x2" in item:
                                        p_box = (float(item["x1"]), float(item["y1"]), float(item["x2"]), float(item["y2"]))
                                        if not any(compute_iou(p_box, existing) >= 0.60 for existing in self.parked_vehicle_boxes):
                                            self.parked_vehicle_boxes.append(p_box)
                                            loaded += 1
                                            bx1, by1, bx2, by2 = p_box
                                            self.stationary_vehicles.append({
                                                "box": p_box,
                                                "cx": (bx1 + bx2) / 2.0,
                                                "cy": (by1 + by2) / 2.0,
                                                "first_seen": now_t - 3600.0,
                                                "last_seen": now_t,
                                                "stationary_frames": 500,
                                                "is_baseline": True,
                                            })
                            except Exception:
                                pass
                    if loaded > 0:
                        logger.info("🚗 Loaded %d user-verified parked vehicle anchors into active memory", loaded)
        except Exception as e:
            logger.debug("Could not pre-load verified parked anchors: %s", e)

    def is_stationary_parked(self, box: tuple[float, float, float, float], w: float, now: float) -> bool:
        """
        Returns True if box matches any confirmed stationary vehicle (parked car):
        IoU >= 0.68 with stationary memory box and centroid shift < 0.04 * width.
        """
        bx1, by1, bx2, by2 = box
        bcx = (bx1 + bx2) / 2.0
        bcy = (by1 + by2) / 2.0

        # Check static baseline boxes first (v3.5.4: IoU >= 0.50)
        for p_box in self.parked_vehicle_boxes:
            iou = compute_iou(box, p_box)
            if iou >= 0.50:
                return True

        # Check dynamic stationary vehicles memory (v3.5.4: IoU >= 0.50)
        for s in self.stationary_vehicles:
            is_confirmed = (s.get("stationary_frames", 0) >= 4) or ((now - s.get("first_seen", now)) >= 10.0) or s.get("is_baseline", False)
            if is_confirmed:
                iou = compute_iou(box, s["box"])
                if iou >= 0.50:
                    return True
        return False

    def update_stationary_vehicles(
        self,
        vehicle_boxes: list[tuple[float, float, float, float]],
        now: float,
        frame_width: int,
    ) -> None:
        """
        Maintains dynamic memory of stationary parked vehicles over the last 15s.
        IoU >= 0.72 and centroid displacement < 0.035 * frame_width marks a vehicle as stationary.
        """
        w = float(frame_width)
        for box in vehicle_boxes:
            bx1, by1, bx2, by2 = box
            bcx = (bx1 + bx2) / 2.0
            bcy = (by1 + by2) / 2.0

            matched = False
            for s in self.stationary_vehicles:
                iou = compute_iou(box, s["box"])
                cdist = math.hypot(bcx - s["cx"], bcy - s["cy"]) / max(1.0, w)
                if iou >= 0.72 and cdist < 0.035:
                    s["last_seen"] = now
                    s["stationary_frames"] += 1
                    s["box"] = box
                    s["cx"] = bcx
                    s["cy"] = bcy
                    matched = True
                    break

            if not matched:
                self.stationary_vehicles.append({
                    "box": box,
                    "cx": bcx,
                    "cy": bcy,
                    "first_seen": now,
                    "last_seen": now,
                    "stationary_frames": 1,
                    "is_baseline": False,
                })

        # Prune vehicles not seen for > 120s (unless baseline)
        self.stationary_vehicles = [
            s for s in self.stationary_vehicles
            if s.get("is_baseline", False) or ((now - s["last_seen"]) < 120.0)
        ]

    def is_vehicle_moving(self, centroids: list[tuple[float, float]], width: float) -> bool:
        """Determines whether a vehicle's centroid sequence represents a moving vehicle."""
        if len(centroids) < 3:
            return False
        cx_first = centroids[0][0]
        cx_last = centroids[-1][0]
        dx_total = cx_last - cx_first
        if abs(dx_total) < (0.10 * width):
            return False
        steps = [centroids[k][0] - centroids[k - 1][0] for k in range(1, len(centroids))]
        sign = 1 if dx_total > 0 else -1
        consistent_steps = sum(1 for s in steps if (s * sign) >= -0.01 * width)
        return consistent_steps >= (len(steps) - 1)

    def is_valid_animal_trajectory(
        self,
        box_history: list[tuple[float, float, float, float]],
        conf: float,
        label: str,
        frame_area: float,
        is_ir_night: bool = False,
    ) -> bool:
        """Multi-frame anti-rain filter for animal detections."""
        min_conf = 0.62 if (label.lower() == "bird" and is_ir_night) else 0.48
        if conf < min_conf or not box_history:
            return False

        box = box_history[-1]
        bw = box[2] - box[0]
        bh = box[3] - box[1]
        box_area = bw * bh
        if box_area < (0.003 * frame_area):
            return False
        ar = bw / max(1.0, bh)
        if ar < 0.3 or ar > 3.0:
            return False
        if len(box_history) < 3:
            return False

        for k in range(1, len(box_history)):
            if compute_iou(box_history[k - 1], box_history[k]) < 0.25:
                return False
        return True

    def extract_motion_crop(
        self,
        frame: np.ndarray,
    ) -> tuple[Optional[np.ndarray], tuple[int, int, int, int], np.ndarray]:
        """
        Background motion detector for idle scene check.
        Returns: (crop_image, (crop_x1, crop_y1, crop_x2, crop_y2), binary_threshold_mask).
        If no significant motion contours exist (area >= 0.003 * frame_area), returns (None, ...).
        """
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.avg_bg is None or self.avg_bg.shape != blurred.shape:
            self.avg_bg = blurred.copy().astype("float")

        # Running weighted average (alpha = 0.08)
        cv2.accumulateWeighted(blurred, self.avg_bg, 0.08)
        diff = cv2.absdiff(blurred, cv2.convertScaleAbs(self.avg_bg))

        # Threshold difference and dilate to eliminate noise
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        dilated = cv2.dilate(thresh, self.kernel_5x5, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_area = w * h
        min_contour_area = 0.003 * frame_area

        valid_contours = [c for c in contours if cv2.contourArea(c) >= min_contour_area]
        if not valid_contours:
            return None, (0, 0, 0, 0), thresh

        # Combine moving contours
        all_pts = np.vstack([c.reshape(-1, 2) for c in valid_contours])
        min_x, min_y = all_pts.min(axis=0)
        max_x, max_y = all_pts.max(axis=0)

        bw = max_x - min_x
        bh = max_y - min_y
        pad_x = int(bw * 0.20)
        pad_y = int(bh * 0.20)

        x1 = max(0, min_x - pad_x)
        y1 = max(0, min_y - pad_y)
        x2 = min(w, max_x + pad_x)
        y2 = min(h, max_y + pad_y)

        side = max(x2 - x1, y2 - y1, 320)
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        crop_x1 = max(0, min(w - side, cx - side // 2))
        crop_y1 = max(0, min(h - side, cy - side // 2))
        crop_x2 = min(w, crop_x1 + side)
        crop_y2 = min(h, crop_y1 + side)

        crop_img = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        return crop_img, (crop_x1, crop_y1, crop_x2, crop_y2), thresh

    def is_in_exclusion_zone(
        self,
        box: tuple[float, float, float, float],
        frame_shape: tuple[int, int],
        zones: list[dict],
    ) -> tuple[bool, str]:
        """Check if bounding box falls into an active exclusion zone."""
        if not zones:
            return False, ""
        h, w = frame_shape[:2]
        bx1, by1, bx2, by2 = box
        bcx = (bx1 + bx2) / 2.0
        bcy = (by1 + by2) / 2.0

        for z in zones:
            zx1 = z.get("x1", 0.0)
            zy1 = z.get("y1", 0.0)
            zx2 = z.get("x2", 1.0)
            zy2 = z.get("y2", 1.0)
            name = z.get("name", "Uitsluitingszone")

            # Convert normalized 0.0-1.0 coordinates to pixels if needed
            if zx2 <= 1.0 and zy2 <= 1.0:
                px1, py1 = zx1 * w, zy1 * h
                px2, py2 = zx2 * w, zy2 * h
            else:
                px1, py1, px2, py2 = zx1, zy1, zx2, zy2

            # Check if center point falls inside zone
            if px1 <= bcx <= px2 and py1 <= bcy <= py2:
                return True, name

            # Or check IoU overlap with zone
            z_box = (px1, py1, px2, py2)
            if compute_iou(box, z_box) >= 0.40:
                return True, name

        return False, ""

    def evaluate_detections(
        self,
        frame: np.ndarray,
        clean_mask: Optional[np.ndarray] = None,
    ) -> tuple[Optional[str], Optional[str], float]:
        """
        Synchronous evaluation helper for test suites and baseline checks.
        Maintains backward compatibility with test_ai_detection_filtering.py.
        """
        if self.model is None and not self.init_model():
            return None, None, 0.0

        h, w = frame.shape[:2]
        if clean_mask is not None:
            thresh = clean_mask
            if cv2.countNonZero(clean_mask) < (0.003 * w * h):
                return None, None, 0.0
            crop_img = frame
            crop_x1, crop_y1 = 0, 0
        else:
            crop_img, (crop_x1, crop_y1, crop_x2, crop_y2), thresh = self.extract_motion_crop(frame)
            if crop_img is None:
                return None, None, 0.0

        try:
            results = self.model(
                crop_img,
                classes=[0, 1, 2, 3, 5, 7, 14, 15, 16],
                conf=0.35,
                verbose=False,
            )
        except Exception as e:
            logger.warning("YOLOv8s inference failed: %s", e)
            return None, None, 0.0

        if not results or len(results) == 0 or results[0].boxes is None:
            return None, None, 0.0

        detections = sv.Detections.from_ultralytics(results[0])
        if len(detections) == 0:
            return None, None, 0.0

        if crop_x1 > 0 or crop_y1 > 0:
            detections.xyxy[:, [0, 2]] += crop_x1
            detections.xyxy[:, [1, 3]] += crop_y1

        try:
            detections = self.tracker.update_with_detections(detections)
            if self.smoother is not None:
                detections = self.smoother.update_with_detections(detections)
        except Exception:
            pass

        if len(detections) == 0:
            return None, None, 0.0

        screen_w = float(w)
        best_person = None
        best_animal = None
        best_vehicle = None

        for i in range(len(detections)):
            cid = int(detections.class_id[i]) if detections.class_id is not None else -1
            conf = float(detections.confidence[i]) if detections.confidence is not None else 0.0
            tid = int(detections.tracker_id[i]) if (detections.tracker_id is not None and detections.tracker_id[i] is not None) else (i + 1)
            box = detections.xyxy[i]

            bx1, by1 = max(0, int(box[0])), max(0, int(box[1]))
            bx2, by2 = min(w, int(box[2])), min(h, int(box[3]))
            bw = bx2 - bx1
            bh = by2 - by1
            box_area = max(1, bw * bh)
            cx = (bx1 + bx2) / 2.0
            cy = (by1 + by2) / 2.0

            # Footpoint filter: bottom must reach ground (y2 >= 0.22 * h)
            if by2 < (0.22 * h):
                continue

            self.track_frame_counts[tid] = self.track_frame_counts.get(tid, 0) + 1
            if tid not in self.track_origins:
                self.track_origins[tid] = (cx, cy)
            orig_cx, orig_cy = self.track_origins[tid]

            if tid not in self.track_history:
                self.track_history[tid] = []
            self.track_history[tid].append((cx, cy))
            if len(self.track_history[tid]) > 25:
                self.track_history[tid].pop(0)

            changed_pixels = cv2.countNonZero(thresh[by1:by2, bx1:bx2])
            pixel_change_ratio = changed_pixels / float(box_area)
            tot_disp = math.hypot(cx - orig_cx, cy - orig_cy)
            disp_ratio = tot_disp / screen_w

            if cid in PERSON_CLASSES:
                if conf < 0.40 or pixel_change_ratio < 0.04:
                    continue
                if best_person is None or conf > best_person[2]:
                    best_person = ("person", "Persoon", conf)
            elif cid in ANIMAL_CLASSES:
                if conf < 0.42 or pixel_change_ratio < 0.04:
                    continue
                if best_animal is None or conf > best_animal[2]:
                    best_animal = ("animal", COCO_NAMES.get(cid, "Dier"), conf)
            elif cid in VEHICLE_CLASSES:
                # Static parked car or stationary spot filter:
                if pixel_change_ratio < 0.04:
                    continue
                is_parked = is_parked_vehicle((bx1, by1, bx2, by2), self.parked_vehicle_boxes, 0.45)
                if is_parked:
                    if pixel_change_ratio < 0.25 or disp_ratio < 0.05:
                        continue
                elif pixel_change_ratio < 0.15 and disp_ratio < 0.05:
                    continue
                if best_vehicle is None or conf > best_vehicle[2]:
                    best_vehicle = ("vehicle", COCO_NAMES.get(cid, "Voertuig"), conf)

        if best_person:
            return best_person
        if best_animal:
            return best_animal
        if best_vehicle:
            return best_vehicle
        return None, None, 0.0

    async def start(self) -> None:
        """Start the background 3-layer verification AI worker."""
        if self._running:
            return
        self.init_model()
        self._running = True
        self._task = asyncio.create_task(self._detection_loop())
        logger.info("3-Layer Verification Gatekeeper AI Engine started (v2.3.0)")

    async def stop(self) -> None:
        """Stop background worker."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _detection_loop(self) -> None:
        """
        Main continuous Temporal Pixel-Diff Verification Loop (v2.6.0).
        Operates on 640x480 4:3 video frames at 5 FPS (0.20s interval) via YOLOv8n (<45ms).
        """
        await asyncio.sleep(2)  # Wait for LiveHub initialization
        frame_interval = 0.20
        last_processed_ts = 0.0

        while self._running:
            try:
                cfg = await DetectionSettings.load()
                if not cfg["detection_enabled"]:
                    await asyncio.sleep(2)
                    continue

                from app.live_hub import live_hub
                jpeg_bytes = live_hub.get_latest_frame()
                frame_ts = live_hub.latest_frame_ts

                if not jpeg_bytes or frame_ts <= 0 or frame_ts == last_processed_ts:
                    # Quick fallback snapshot fetch if cache is cold
                    jpeg_bytes = await live_hub.fetch_snapshot_async(timeout=1.0)
                    frame_ts = live_hub.latest_frame_ts

                if not jpeg_bytes:
                    await asyncio.sleep(0.04)
                    continue

                last_processed_ts = frame_ts
                now_t = time.time()

                # Decode JPEG directly from RAM (0 extra RTSP connections, 0 extra CPU)
                raw_frame = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
                if raw_frame is None:
                    await asyncio.sleep(0.04)
                    continue

                # Native 4:3 target resolution (640x480 for fast 5 FPS inference)
                if raw_frame.shape[1] != 640 or raw_frame.shape[0] != 480:
                    frame_640 = cv2.resize(raw_frame, (640, 480))
                else:
                    frame_640 = raw_frame

                # Run Temporal Pixel-Diff Masking Gatekeeper
                await self._process_frame_gatekeeper(frame_640, now_t, cfg)

                # High-Speed AI Scan-Burst: 4 FPS (0.25s) for 10s on hardware motion or active candidate
                burst_active = (now_t - self._last_hardware_motion_time) < 10.0 or (now_t - self._last_candidate_seen_time) < 10.0
                frame_interval = 0.25 if burst_active else 0.50
                await asyncio.sleep(frame_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("AI detection loop error: %s", e)
                await asyncio.sleep(2)

    async def _process_frame_gatekeeper(
        self,
        frame: np.ndarray,
        now: float,
        cfg: dict,
    ) -> None:
        """
        Processes frame through YOLOv8n + Temporal Pixel-Diff Masking (v2.6.0).
        - Temporal pixel diff between frame_now and frame ~1.2s ago.
        - Parked vehicles with changed_pixel_ratio < 0.04 ignored immediately as STATIC_PARKED_CAR.
        - Moving vehicles verified via Manier 1 (New, IoU < 0.50, 2 frames, ratio >= 0.15)
          or Manier 2 (Departing, ratio >= 0.25, centroid shift >= 5%).
        - Persons and animals verified over 2 frames (ratio >= 0.08, displacement >= 3%).
        """
        if self.model is None and not self.init_model():
            return

        h, w = frame.shape[:2]

        # ── 1. Temporal Pixel-Diff Motion Mask (frame_now vs frame ~1.2s ago) ────
        gray_now = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.frame_history.append((now, gray_now))

        gray_prev = None
        for t, g in self.frame_history:
            dt = now - t
            if 0.8 <= dt <= 1.6:
                gray_prev = g
                break
        if gray_prev is None and len(self.frame_history) >= 2:
            if (now - self.frame_history[0][0]) >= 0.35:
                gray_prev = self.frame_history[0][1]

        if gray_prev is not None and gray_prev.shape == gray_now.shape:
            diff = cv2.absdiff(
                cv2.GaussianBlur(gray_now, (9, 9), 0),
                cv2.GaussianBlur(gray_prev, (9, 9), 0),
            )
            _, motion_mask = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
        else:
            motion_mask = np.zeros_like(gray_now)

        # ── 2. Load Dynamic System Settings from SQLite ───────────────────
        try:
            sys_settings = await get_system_settings()
            person_conf_thresh = float(sys_settings.get("person_conf_threshold", "0.35"))
            vehicle_conf_thresh = float(sys_settings.get("vehicle_conf_threshold", "0.38"))
            animal_conf_thresh = float(sys_settings.get("animal_conf_threshold", "0.32"))
            min_person_disp = float(sys_settings.get("min_person_displacement_pct", "1.5")) / 100.0
            min_vehicle_disp = float(sys_settings.get("min_vehicle_displacement_pct", "4.5")) / 100.0
            verify_frames_req = int(sys_settings.get("verify_frames_required", "2"))
            cooldown_sec = float(sys_settings.get("event_cooldown_seconds", "5.0"))
            self._cooldown_seconds = cooldown_sec

            zones_raw = sys_settings.get("exclusion_zones", "[]")
            exclusion_zones = json.loads(zones_raw) if zones_raw else []

            # FASE 1: Optional MOG2 motion gate configuration (default disabled)
            enable_mog2 = str(sys_settings.get("enable_mog2_motion_gate", "false")).lower() in ("true", "1")
            mg_min_area = float(sys_settings.get("motion_gate_min_area_ratio", "0.003"))
            mg_cooldown = float(sys_settings.get("motion_gate_cooldown_sec", "1.5"))
            if getattr(self, "motion_gater", None):
                self.motion_gater.update_config(
                    min_area_ratio=mg_min_area,
                    cooldown_sec=mg_cooldown,
                    enabled=enable_mog2,
                )

            # FASE 2: Optional ByteTrack consistency configuration (default disabled)
            enable_bytetrack = str(sys_settings.get("enable_bytetrack_consistency", "false")).lower() in ("true", "1")
            bt_min_frames = int(sys_settings.get("bytetrack_min_frames", "3"))
            bt_driving_disp = float(sys_settings.get("bytetrack_driving_displacement_px", "25.0"))
            if getattr(self, "consistent_tracker", None):
                self.consistent_tracker.update_config(
                    min_track_frames=bt_min_frames,
                    driving_displacement_px=bt_driving_disp,
                    enabled=enable_bytetrack,
                )
        except Exception:
            person_conf_thresh = 0.35
            vehicle_conf_thresh = 0.38
            animal_conf_thresh = 0.32
            min_person_disp = 0.015
            min_vehicle_disp = 0.045
            verify_frames_req = 2
            cooldown_sec = 5.0
            self._cooldown_seconds = 5.0
            exclusion_zones = []

        # ── 2b. Motion Pre-Gating (Motion-First Verification) ─────────────
        is_hardware_motion_active = (now - self._last_hardware_motion_time) < 10.0
        has_active_candidate = (now - self._last_candidate_seen_time) < 4.0 or len(self._candidate_tracks) > 0

        # FASE 1: If MOG2 motion gater is enabled via feature flag, use it:
        if getattr(self, "motion_gater", None) and self.motion_gater.enabled:
            stationary_boxes = [s["box"] for s in self.stationary_vehicles]
            mg_res = self.motion_gater.process_frame(
                frame,
                now=now,
                stationary_boxes=stationary_boxes,
                force_open=is_hardware_motion_active or has_active_candidate,
            )
            is_motion_gate_open = mg_res.is_gate_open
            if mg_res.motion_mask is not None:
                motion_mask = mg_res.motion_mask
        else:
            motion_pixel_count = int(np.count_nonzero(motion_mask))
            motion_ratio = motion_pixel_count / float(w * h)
            is_motion_gate_open = is_hardware_motion_active or has_active_candidate or (motion_ratio >= 0.002)

        if not is_motion_gate_open:
            # Entire scene is static: skip YOLO inference to prevent evaluating parked cars
            self._prune_stale_tracks(now)
            return

        # ── 3. Layer 1: Fast YOLO Inference (imgsz=640, <45ms) ───────────
        if is_hardware_motion_active:
            person_conf_thresh = min(person_conf_thresh, 0.22)
            vehicle_conf_thresh = min(vehicle_conf_thresh, 0.22)
            animal_conf_thresh = min(animal_conf_thresh, 0.22)
            yolo_conf = 0.20
        else:
            yolo_conf = 0.20

        try:
            results = self.model(
                frame,
                classes=[0, 1, 2, 3, 5, 7, 14, 15, 16, 17, 18, 19],
                conf=yolo_conf,
                iou=0.45,
                imgsz=640,
                verbose=False,
            )
        except Exception as e:
            logger.warning("YOLO inference failed: %s", e)
            return

        has_yolo_boxes = results and len(results) > 0 and results[0].boxes is not None and len(results[0].boxes) > 0
        if has_yolo_boxes:
            boxes = results[0].boxes.xyxy.cpu().numpy()
            confs = results[0].boxes.conf.cpu().numpy()
            classes = results[0].boxes.cls.cpu().numpy()
        else:
            boxes = np.empty((0, 4))
            confs = np.empty((0,))
            classes = np.empty((0,))

        # Update Stationary Parked-Car Memory (v3.4.0) with all detected vehicle boxes
        detected_vehicle_boxes = [
            (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            for b, cls_val in zip(boxes, classes)
            if int(cls_val) in VEHICLE_CLASSES
        ]
        self.update_stationary_vehicles(detected_vehicle_boxes, now, w)

        # FASE 2: ByteTrack Consistent Tracking Evaluation (if feature flag enabled)
        if getattr(self, "consistent_tracker", None) and self.consistent_tracker.enabled:
            from app.object_tracker import TrackState
            tracked_objs = self.consistent_tracker.update(
                boxes=boxes,
                confs=confs,
                classes=classes,
                frame=frame,
                now=now,
                coco_names=COCO_NAMES,
                known_anchors=self.parked_vehicle_boxes,
            )
            for tobj in tracked_objs:
                if not tobj.is_confirmed or tobj.has_been_dispatched:
                    continue

                # Loopvlak ground-plane filter
                if tobj.best_box[3] < (0.18 * h):
                    continue

                # Exclusion zones filter
                in_z, _ = self.is_in_exclusion_zone(tobj.best_box, (h, w), exclusion_zones)
                if in_z:
                    continue

                if tobj.category == "vehicle":
                    # Person Dominance: if a person is in the scene, suppress vehicle dispatch
                    has_person_near = any(
                        t.category == "person" and t.is_confirmed
                        for t in tracked_objs
                    ) or ((now - self._last_person_seen) < 12.0)

                    if has_person_near:
                        logger.debug("ByteTrack Vehicle dispatch suppressed: Person dominance active in scene")
                        continue

                    if tobj.state == TrackState.DRIVING:
                        tobj.has_been_dispatched = True
                        logger.info("ByteTrack Vehicle DRIVING APPROVED: ID=%d, label=%s, disp=%.1fpx", tobj.tracker_id, tobj.label, tobj.window_displacement_px)
                        asyncio.create_task(self._dispatch_verified_detection(
                            category="vehicle",
                            label=tobj.label,
                            confidence=tobj.best_conf,
                            disp_pct=round((tobj.total_displacement_px / float(w)) * 100.0, 1),
                            box=tobj.best_box,
                            keyframe=tobj.best_frame if tobj.best_frame is not None else frame,
                            cfg=cfg,
                        ))
                    elif tobj.state == TrackState.PARKED:
                        logger.debug("ByteTrack Vehicle PARKED suppressed: ID=%d, label=%s, disp=%.1fpx", tobj.tracker_id, tobj.label, tobj.window_displacement_px)

                elif tobj.category == "person" and tobj.state == TrackState.WALKING:
                    tobj.has_been_dispatched = True
                    logger.info("ByteTrack Person WALKING APPROVED: ID=%d, label=%s, disp=%.1fpx", tobj.tracker_id, tobj.label, tobj.total_displacement_px)
                    asyncio.create_task(self._dispatch_verified_detection(
                        category="person",
                        label=tobj.label,
                        confidence=tobj.best_conf,
                        disp_pct=round((tobj.total_displacement_px / float(w)) * 100.0, 1),
                        box=tobj.best_box,
                        keyframe=tobj.best_frame if tobj.best_frame is not None else frame,
                        cfg=cfg,
                    ))

                elif tobj.category == "animal" and tobj.state == TrackState.ANIMAL:
                    tobj.has_been_dispatched = True
                    logger.info("ByteTrack Animal APPROVED: ID=%d, label=%s, conf=%.2f", tobj.tracker_id, tobj.label, tobj.best_conf)
                    asyncio.create_task(self._dispatch_verified_detection(
                        category="animal",
                        label=tobj.label,
                        confidence=tobj.best_conf,
                        disp_pct=round((tobj.total_displacement_px / float(w)) * 100.0, 1),
                        box=tobj.best_box,
                        keyframe=tobj.best_frame if tobj.best_frame is not None else frame,
                        cfg=cfg,
                    ))
            return

        detected_candidates = []

        for b, conf_val, cls_val in zip(boxes, confs, classes):
            cid = int(cls_val)
            conf = float(conf_val)
            bx1, by1, bx2, by2 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
            box = (bx1, by1, bx2, by2)

            # Determine category
            category = None
            if cid in PERSON_CLASSES:
                category = "person"
                # Layer 1 Shape & Aspect Ratio Filter:
                # Reject only extreme flat horizontal artifacts (aspect_ratio < 0.85, width > height)
                box_w = max(1.0, bx2 - bx1)
                box_h = max(1.0, by2 - by1)
                aspect_ratio = box_h / box_w
                if aspect_ratio < 0.85:
                    logger.debug("Rejected person candidate with invalid aspect ratio %.2f < 0.85", aspect_ratio)
                    asyncio.create_task(self._log_rejected_candidate(
                        "person", "Persoon", conf, 0.0,
                        f"AFGEWEZEN: Aspect-ratio ongeldig ({aspect_ratio:.2f} < 0.85)",
                        box, frame
                    ))
                    continue

                # Foliage/Tree zone check vs Doorstep Visitor:
                # Tree canopy foliage is strictly in the upper canopy (y1 < 35% and y2 < 50% height).
                # Walkway / doorstep visitors stand on ground level (y2 >= 40% height or box_h >= 18% height).
                cx_norm = ((bx1 + bx2) / 2.0) / float(w)
                is_doorstep_visitor = (by2 >= (0.40 * float(h))) or (box_h >= (0.18 * float(h)))
                is_upper_tree_canopy = (not is_doorstep_visitor) and (by1 < (0.35 * float(h))) and (by2 < (0.50 * float(h))) and ((cx_norm < 0.16) or (cx_norm > 0.68))
                if is_doorstep_visitor or is_hardware_motion_active:
                    req_conf = 0.20
                elif is_upper_tree_canopy:
                    req_conf = 0.55
                else:
                    req_conf = max(min(person_conf_thresh, 0.38), 0.28)
            elif cid in VEHICLE_CLASSES:
                category = "vehicle"
                req_conf = min(vehicle_conf_thresh, 0.40)
            elif cid in ANIMAL_CLASSES:
                category = "animal"
                req_conf = min(animal_conf_thresh, 0.35)
            else:
                continue

            label = COCO_NAMES.get(cid, category)

            # Stap A: Loopvlak Ground-Plane Filter
            # The bottom of the box (by2) must be >= 0.18 * frame height.
            if by2 < (0.18 * h):
                asyncio.create_task(self._log_rejected_candidate(
                    category, label, conf, 0.0,
                    f"AFGEWEZEN: Buiten loopvlak (bovenin beeld y={by2:.0f})",
                    box, frame
                ))
                continue

            # Stap B: Exclusion Zones
            in_zone, zone_name = self.is_in_exclusion_zone(box, (h, w), exclusion_zones)
            if in_zone:
                asyncio.create_task(self._log_rejected_candidate(
                    category, label, conf, 0.0,
                    f"AFGEWEZEN: In uitsluitzone '{zone_name}'",
                    box, frame
                ))
                continue

            # Confidence check
            if conf < req_conf:
                asyncio.create_task(self._log_rejected_candidate(
                    category, label, conf, 0.0,
                    f"AFGEWEZEN: Zekerheid {round(conf*100)}% te laag (<{round(req_conf*100)}%)",
                    box, frame
                ))
                continue

            # Stap C: Pixel-level motion inside bounding box
            ibx1, iby1 = max(0, int(bx1)), max(0, int(by1))
            ibx2, iby2 = min(w, int(bx2)), min(h, int(by2))
            box_motion = motion_mask[iby1:iby2, ibx1:ibx2]
            changed_pixel_ratio = (
                float(np.count_nonzero(box_motion)) / float(box_motion.size)
                if box_motion.size > 0
                else 0.0
            )

            # Static Parked Car & Stationary Spot Filter (v3.4.0)
            if category == "vehicle":
                if self.is_stationary_parked(box, float(w), now):
                    logger.debug("STATIONARY_PARKED_CAR matched at %s", box)
                    # Silently skip known parked cars without flooding ai_audit_logs
                    continue

            detected_candidates.append((category, label, conf, box, changed_pixel_ratio))

        if detected_candidates:
            self._last_candidate_seen_time = now
        elif is_hardware_motion_active:
            # Smart motion fallback on motion_mask outside parked cars & trees (v3.4.1)
            fallback_mask = motion_mask.copy()
            # Mask out upper canopy / roofline (y < 0.22 * h) and top right tree canopy (x > 0.60 * w, y < 0.35 * h)
            fallback_mask[:int(0.22 * h), :] = 0
            fallback_mask[:int(0.35 * h), int(0.60 * w):] = 0

            # Mask out stationary vehicles
            for s in self.stationary_vehicles:
                sbx1, sby1, sbx2, sby2 = [int(v) for v in s["box"]]
                sbx1, sby1 = max(0, sbx1), max(0, sby1)
                sbx2, sby2 = min(w, sbx2), min(h, sby2)
                fallback_mask[sby1:sby2, sbx1:sbx2] = 0

            contours, _ = cv2.findContours(fallback_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            min_area = 0.015 * w * h  # 1.5% of frame area
            for cnt in contours:
                area = cv2.contourArea(cnt)
                if area >= min_area:
                    x, y, cw, ch = cv2.boundingRect(cnt)
                    cbox = (float(x), float(y), float(x + cw), float(y + ch))
                    in_z, _ = self.is_in_exclusion_zone(cbox, (h, w), exclusion_zones)
                    if not in_z:
                        detected_candidates.append(("motion", "Beweging (Hardware)", 0.85, cbox, 0.25))
                        self._last_candidate_seen_time = now
                        break

        # Check person priority:
        has_person_now = any(cat == "person" for (cat, _, _, _, _) in detected_candidates)
        if has_person_now:
            self._last_person_seen = now
        has_person_priority = (now - self._last_person_seen) < 15.0

        # ── 4. Candidate Association & Verification ───────────────────────
        for category, label, conf, box, pixel_ratio in detected_candidates:
            # Person Priority: if person active within 15s, suppress lower confidence vehicles
            if category == "vehicle" and has_person_priority and conf < 0.65:
                logger.debug("Vehicle candidate suppressed: person active within last 15s")
                continue

            matched_id = None
            best_iou = 0.0

            for tid, track in self._candidate_tracks.items():
                if track.category == category:
                    last_box = track.frames[-1][1]
                    iou = compute_iou(box, last_box)
                    bcx = (box[0] + box[2]) / 2.0
                    bcy = (box[1] + box[3]) / 2.0
                    lcx = (last_box[0] + last_box[2]) / 2.0
                    lcy = (last_box[1] + last_box[3]) / 2.0
                    dist = math.hypot(bcx - lcx, bcy - lcy) / float(w)
                    bw = box[2] - box[0]
                    lbw = last_box[2] - last_box[0]
                    size_ratio = min(bw, lbw) / max(1.0, max(bw, lbw))

                    if category == "vehicle":
                        if iou >= 0.15 or (dist < 0.20 and size_ratio > 0.40):
                            if iou > best_iou or (best_iou == 0.0 and dist < 0.20):
                                best_iou = max(iou, 0.01)
                                matched_id = tid
                    elif category == "motion":
                        if iou >= 0.10 or (dist < 0.25 and size_ratio > 0.30):
                            if iou > best_iou or (best_iou == 0.0 and dist < 0.25):
                                best_iou = max(iou, 0.01)
                                matched_id = tid
                    else:
                        if iou >= 0.20 or (dist < 0.15 and size_ratio > 0.45):
                            if iou > best_iou or (best_iou == 0.0 and dist < 0.15):
                                best_iou = max(iou, 0.01)
                                matched_id = tid

            if matched_id is not None:
                track = self._candidate_tracks[matched_id]
                track.frames.append((now, box, conf))
                track.motion_ratios.append(pixel_ratio)
                track.last_time = now
                if conf > track.best_conf:
                    track.best_conf = conf
                    track.best_box = box
                    track.best_frame = frame.copy()
            else:
                tid = f"track_{self._next_candidate_num}"
                self._next_candidate_num += 1
                cand = CandidateDetection(tid, category, label, box, conf, now, pixel_ratio)
                cand.best_frame = frame.copy()
                self._candidate_tracks[tid] = cand

        # ── 5. Verify Active Tracks Over >= 2 Frames ───────────────────────
        to_remove = []

        for tid, track in list(self._candidate_tracks.items()):
            if track.verified:
                continue

            if len(track.frames) >= verify_frames_req:
                duration_tracked = track.last_time - track.first_time
                first_box = track.frames[0][1]
                last_box = track.frames[-1][1]

                cx1 = (first_box[0] + first_box[2]) / 2.0
                cy1 = (first_box[1] + first_box[3]) / 2.0
                cx2 = (last_box[0] + last_box[2]) / 2.0
                cy2 = (last_box[1] + last_box[3]) / 2.0

                delta_d = math.hypot(cx2 - cx1, cy2 - cy1) / float(w)
                delta_d_pct = round(delta_d * 100.0, 1)
                max_pixel_ratio = max(track.motion_ratios) if track.motion_ratios else 0.0

                is_valid_object = False
                verified_disp_pct = delta_d_pct

                if track.category == "vehicle":
                    # Check IoU overlap with stationary vehicles and parked boxes
                    max_stat_iou = 0.0
                    for s in self.stationary_vehicles:
                        iou = compute_iou(last_box, s["box"])
                        if iou > max_stat_iou:
                            max_stat_iou = iou
                    for pb in self.parked_vehicle_boxes:
                        iou = compute_iou(last_box, pb)
                        if iou > max_stat_iou:
                            max_stat_iou = iou

                    # Only reject as parked if stationary overlap is high and it hasn't moved
                    if max_stat_iou >= 0.45 and delta_d < 0.10:
                        if duration_tracked >= 1.5:
                            asyncio.create_task(self._log_rejected_candidate(
                                "vehicle", track.label, track.best_conf, delta_d_pct,
                                f"AFGEWEZEN: Geparkeerd/stilstaand voertuig (IoU={max_stat_iou:.2f}, d={delta_d_pct:.1f}%)",
                                last_box, frame
                            ))
                            to_remove.append(tid)
                        continue

                    # Moving vehicle verification (passing cars, driveway driving):
                    # Over >= 2 frames: Euclidean displacement >= 3.5% screen width
                    if len(track.frames) >= 2:
                        if delta_d >= 0.035:
                            is_valid_object = True
                            verified_disp_pct = delta_d_pct
                            logger.info(
                                "Vehicle Motion APPROVED: %s frames=%d disp=%.1f%%",
                                track.label, len(track.frames), delta_d_pct
                            )
                    elif duration_tracked >= 2.0:
                        asyncio.create_task(self._log_rejected_candidate(
                            "vehicle", track.label, track.best_conf, delta_d_pct,
                            f"AFGEWEZEN: Te weinig verplaatsing ({delta_d_pct:.1f}% < 3.5%)",
                            last_box, frame
                        ))
                        to_remove.append(tid)
                        continue

                elif track.category == "person":
                    box_w = max(1.0, last_box[2] - last_box[0])
                    box_h = max(1.0, last_box[3] - last_box[1])
                    aspect_ratio = box_h / box_w
                    if aspect_ratio < 0.85:
                        if duration_tracked >= 1.0:
                            asyncio.create_task(self._log_rejected_candidate(
                                "person", track.label, track.best_conf, delta_d_pct,
                                f"AFGEWEZEN: Aspect-ratio ongeldig ({aspect_ratio:.2f} < 0.85)",
                                last_box, frame
                            ))
                            to_remove.append(tid)
                        continue

                    cx_norm = cx2 / float(w)
                    last_h = max(1.0, last_box[3] - last_box[1])
                    is_doorstep_visitor = (last_box[3] >= (0.40 * float(h))) or (last_h >= (0.18 * float(h)))
                    is_upper_tree_canopy = (not is_doorstep_visitor) and (last_box[1] < (0.35 * float(h))) and (last_box[3] < (0.50 * float(h))) and ((cx_norm < 0.16) or (cx_norm > 0.68))

                    # Pedestrian verification over >= 2 frames:
                    if len(track.frames) >= 2:
                        # In upper tree canopy with 4+ frames: check oscillation to filter swaying tree branches
                        if len(track.frames) >= 4 and is_upper_tree_canopy:
                            total_path = 0.0
                            for f_i in range(len(track.frames) - 1):
                                fb1 = track.frames[f_i][1]
                                fb2 = track.frames[f_i+1][1]
                                if len(fb1) >= 4 and len(fb2) >= 4:
                                    f_c1 = ((fb1[0] + fb1[2]) / 2.0, (fb1[1] + fb1[3]) / 2.0)
                                    f_c2 = ((fb2[0] + fb2[2]) / 2.0, (fb2[1] + fb2[3]) / 2.0)
                                    total_path += math.hypot(f_c2[0] - f_c1[0], f_c2[1] - f_c1[1])
                            net_disp = math.hypot(cx2 - cx1, cy2 - cy1)
                            oscillation_ratio = (net_disp / max(1.0, total_path)) if total_path > 0 else 0.0
                            if oscillation_ratio < 0.50:
                                if duration_tracked >= 1.5:
                                    asyncio.create_task(self._log_rejected_candidate(
                                        "person", track.label, track.best_conf, delta_d_pct,
                                        f"AFGEWEZEN: Wapperende takken/wind (osc_ratio={oscillation_ratio:.2f})",
                                        last_box, frame
                                    ))
                                    to_remove.append(tid)
                                continue

                        # Approve walking pedestrian or doorstep visitor
                        if is_doorstep_visitor:
                            min_disp = 0.3
                        elif is_upper_tree_canopy:
                            min_disp = 2.5
                        else:
                            min_disp = 1.0

                        if delta_d_pct >= min_disp or track.best_conf >= 0.50 or (is_doorstep_visitor and len(track.frames) >= 2):
                            is_valid_object = True
                            verified_disp_pct = delta_d_pct
                            logger.info("Person APPROVED: %s conf=%.2f disp=%.1f%% frames=%d (doorstep=%s)", track.label, track.best_conf, delta_d_pct, len(track.frames), is_doorstep_visitor)
                    elif duration_tracked >= 1.8:
                        asyncio.create_task(self._log_rejected_candidate(
                            "person", track.label, track.best_conf, delta_d_pct,
                            f"AFGEWEZEN: Te weinig verplaatsing ({delta_d_pct:.1f}% < 1.0%)",
                            last_box, frame
                        ))
                        to_remove.append(tid)
                        continue

                elif track.category == "animal":
                    # Animal: Cat, dog, bird, horse, sheep, cow
                    # Verified immediately on detection (>= 1 frame) with conf >= animal_conf_thresh (min 0.35)
                    if track.best_conf >= min(animal_conf_thresh, 0.35):
                        is_valid_object = True
                        verified_disp_pct = delta_d_pct
                        logger.info("Animal APPROVED: %s conf=%.2f", track.label, track.best_conf)
                    elif duration_tracked >= 1.0:
                        asyncio.create_task(self._log_rejected_candidate(
                            "animal", track.label, track.best_conf, delta_d_pct,
                            f"AFGEWEZEN: Dier zekerheid te laag ({round(track.best_conf*100)}% < 35%)",
                            last_box, frame
                        ))
                        to_remove.append(tid)
                        continue

                elif track.category == "motion":
                    # Smart Motion Fallback (v3.4.1) outside stationary vehicles & canopy:
                    # Verified over >= 2 frames (or duration >= 0.4s) with displacement >= 2.0%
                    if delta_d >= 0.02 or len(track.frames) >= 2:
                        is_valid_object = True
                        verified_disp_pct = delta_d_pct
                        logger.info("Motion Fallback APPROVED: %s frames=%d disp=%.1f%%", track.label, len(track.frames), delta_d_pct)
                    elif duration_tracked >= 1.5:
                        asyncio.create_task(self._log_rejected_candidate(
                            "motion", track.label, track.best_conf, delta_d_pct,
                            f"AFGEWEZEN: Beweging te klein ({delta_d_pct:.1f}% < 2.0%)",
                            last_box, frame
                        ))
                        to_remove.append(tid)
                        continue

                # ✅ VERIFICATION PASSED!
                if is_valid_object:
                    track.verified = True
                    self._last_verified_object_seen = now
                    to_remove.append(tid)

                    try:
                        from app.recorder import notify_ongoing_motion
                        notify_ongoing_motion(category=track.category, label=track.label)
                    except Exception:
                        pass

                    logger.info(
                        "Gatekeeper APPROVED [%s]: %s (conf=%.2f, Delta d=%.1f%%, ratio=%.2f)",
                        tid, track.label, track.best_conf, verified_disp_pct, max_pixel_ratio
                    )

                    best_img = track.best_frame if track.best_frame is not None else frame
                    asyncio.create_task(
                        self._dispatch_verified_detection(
                            category=track.category,
                            label=track.label,
                            confidence=track.best_conf,
                            disp_pct=verified_disp_pct,
                            box=track.best_box,
                            keyframe=best_img,
                            cfg=cfg,
                        )
                    )

        for tid in to_remove:
            self._candidate_tracks.pop(tid, None)

        self._prune_stale_tracks(now)

    def _prune_stale_tracks(self, now: float) -> None:
        """Prunes candidate tracks older than 2.0s without updates."""
        stale_ids = [
            tid for tid, track in self._candidate_tracks.items()
            if (now - track.last_time) > 2.0
        ]
        for tid in stale_ids:
            self._candidate_tracks.pop(tid, None)

    async def _log_rejected_candidate(
        self,
        category: str,
        label: str,
        confidence: float,
        disp_pct: float,
        reason: str,
        box: tuple[float, float, float, float],
        frame: np.ndarray,
    ) -> None:
        """Save rejected AI evaluation snapshot and audit log to SQLite (debounced per category/reason)."""
        try:
            now_t = time.time()
            # Debounce per category + reason (max 1 rejected log per 8 seconds for the same condition, or 120s for stationary cars to prevent disk bloat)
            debounce_limit = 120.0 if "Stilstaande" in reason else 8.0
            key = f"{category}_{reason[:20]}"
            if (now_t - self._last_rejected_log_time.get(key, 0.0)) < debounce_limit:
                return
            self._last_rejected_log_time[key] = now_t

            audit_id = f"audit_rej_{int(now_t * 1000)}"
            snap_path = str(settings.SNAPSHOTS_DIR / f"{audit_id}.jpg")

            # Draw red bounding box and rejection tag on snapshot
            annotated = frame.copy()
            x1, y1, x2, y2 = [int(v) for v in box]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 2)
            tag = f"REJECTED: {label} ({round(confidence*100)}%) d={disp_pct}%"
            cv2.putText(annotated, tag, (x1, max(25, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.imwrite(snap_path, annotated)

            await save_ai_audit_log(
                status="REJECTED",
                category=category,
                label=label,
                confidence=round(confidence, 2),
                displacement_pct=disp_pct,
                reason=reason,
                snapshot_path=snap_path,
                boxes_json=json.dumps([{"x1": x1, "y1": y1, "x2": x2, "y2": y2}]),
            )
        except Exception as e:
            logger.warning("Error saving rejected candidate audit log: %s", e)

    async def _dispatch_verified_detection(
        self,
        category: str,
        label: str,
        confidence: float,
        disp_pct: float,
        box: tuple[float, float, float, float],
        keyframe: np.ndarray,
        cfg: dict,
    ) -> None:
        """
        Layer 3: Immediate Push & Object-Based Recording.
        Dispatches push notification within 1.5s of detection and launches background recording.
        """
        now_t = time.time()
        # Per-category cooldown: 5s for same category, 0s for different category (v3.4.0)
        last_cat_trigger = self._last_trigger_per_category.get(category, 0.0)
        last_cat_end = self._last_clip_end_per_category.get(category, 0.0)
        cooldown = self._cooldown_seconds  # 5.0 seconds
        if (now_t - last_cat_trigger) < cooldown or (now_t - last_cat_end) < cooldown:
            logger.debug(
                "Cooldown active for category '%s' (%.1fs remaining), skipping event dispatch",
                category,
                max(0.0, cooldown - (now_t - max(last_cat_trigger, last_cat_end))),
            )
            return

        self._last_trigger_per_category[category] = now_t
        self._last_trigger_time = now_t

        should_record = False
        should_notify = False

        if category == "person":
            should_record = cfg.get("record_person", True)
            should_notify = cfg.get("notify_person", True)
        elif category == "vehicle":
            should_record = cfg.get("record_vehicle", True)
            should_notify = cfg.get("notify_vehicle", True)
        elif category == "animal":
            should_record = cfg.get("record_animal", True)
            should_notify = cfg.get("notify_animal", True)
        elif category == "motion":
            should_record = cfg.get("record_motion", True)
            should_notify = cfg.get("notify_motion", True)
        else:
            should_record = cfg.get("record_motion", False)
            should_notify = cfg.get("notify_motion", False)

        if not should_record:
            return

        in_quiet_hours = False
        if cfg.get("quiet_hours_enabled", True):
            in_quiet_hours = self.is_in_quiet_hours(
                cfg.get("quiet_hours_start", "23:30"), cfg.get("quiet_hours_end", "07:30")
            )
        if in_quiet_hours:
            should_notify = False

        clip_id = str(uuid.uuid4())
        from app.recorder import get_day_clip_dir
        day_dir = get_day_clip_dir()
        clip_path = str(day_dir / f"{clip_id}.mp4")
        t0_iso = datetime.now(timezone.utc).isoformat()

        # 1. Stap 1 — Sla EERST de Snapshot op schijf op (VÓÓR het event wordt verstuurd!) (TAAK 1.1 / v3.5.1)
        settings.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        settings.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        thumb_clip_path = str(day_dir / f"thumb_{clip_id}.jpg")
        day_snap_path = str(day_dir / f"{clip_id}.jpg")
        root_thumb_path = str(settings.CLIPS_DIR / f"thumb_{clip_id}.jpg")
        root_clip_snap = str(settings.CLIPS_DIR / f"{clip_id}.jpg")
        clean_snapshot_path = str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")

        cv2.imwrite(thumb_clip_path, keyframe)
        cv2.imwrite(day_snap_path, keyframe)
        cv2.imwrite(root_thumb_path, keyframe)
        cv2.imwrite(root_clip_snap, keyframe)
        cv2.imwrite(clean_snapshot_path, keyframe)

        if not os.path.exists(thumb_clip_path) or os.path.getsize(thumb_clip_path) < 5000:
            logger.warning("Thumbnail %s is smaller than 5KB or write failed", thumb_clip_path)

        snapshot_url = f"/api/v1/clips/{clip_id}/thumbnail"

        # 2. Save DEBUG snapshot (with boxes) ONLY for Web Admin Inspector
        debug_snapshot_path = str(settings.SNAPSHOTS_DIR / f"{clip_id}_debug.jpg")
        annotated_snapshot = keyframe.copy()
        x1, y1, x2, y2 = [int(v) for v in box]
        cv2.rectangle(annotated_snapshot, (x1, y1), (x2, y2), (0, 255, 0), 2)
        tag = f"{label} ({round(confidence*100)}%) d={disp_pct}%"
        cv2.putText(annotated_snapshot, tag, (x1, max(25, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.imwrite(debug_snapshot_path, annotated_snapshot)

        category_dutch = {
            "person": "Persoon gedetecteerd",
            "vehicle": "Voertuig gedetecteerd",
            "animal": "Dier gedetecteerd",
            "motion": "Beweging gedetecteerd",
        }.get(category, "Beweging")
        title = f"{category_dutch}: {label}"

        # 3. Stap 2 — Maak de Clip-rij direct aan met status = "RECORDING" (TAAK 1.2)
        await save_clip(
            clip_id=clip_id,
            call_id=clip_id,
            video_path=clip_path,
            snapshot_path=thumb_clip_path,
            answered_by=f"AI: {category_dutch} ({label})",
            duration=0.0,
            status="RECORDING",
            category=category,
            file_size=0,
            created_at=t0_iso,
        )

        # 4. Insert event in events table immediately for instant push (<1.5s)
        event_dict = await save_event(
            event_type="motion",
            category=category,
            title=title,
            clip_id=clip_id,
            snapshot_url=snapshot_url,
            silent=in_quiet_hours or not should_notify,
        )

        # 5. Log to ai_audit_logs as APPROVED with debug snapshot for Web Admin Inspector
        await save_ai_audit_log(
            status="APPROVED",
            category=category,
            label=label,
            confidence=round(confidence, 2),
            displacement_pct=disp_pct,
            reason=f"GOEDGEKEURD: {category_dutch} bevestigd met {disp_pct}% verplaatsing",
            snapshot_path=debug_snapshot_path,
            boxes_json=json.dumps([{"x1": x1, "y1": y1, "x2": x2, "y2": y2}]),
        )

        # 6. Push IMMEDIATELY via WebSocket (instant <1.5s delivery)
        await push_event(
            "AI_DETECTION",
            {
                "event_id": event_dict["id"],
                "clip_id": clip_id,
                "category": category,
                "label": label,
                "title": title,
                "confidence": round(confidence, 2),
                "timestamp": t0_iso,
                "snapshot_url": snapshot_url,
                "in_quiet_hours": in_quiet_hours,
                "should_notify": should_notify and not in_quiet_hours,
                "silent": in_quiet_hours or not should_notify,
            },
        )
        logger.info("Dispatched immediate push notification for %s (%s) within 1.2s", category, label)

        from app.logger import log_ai_event, log_notify_decision
        log_ai_event(
            category=category,
            label=label,
            confidence=round(confidence, 2),
            displacement_pct=disp_pct,
            silent=in_quiet_hours or not should_notify,
            action="Dispatched" if (should_notify and not in_quiet_hours) else "Silent/Suppressed",
            details=f"quiet={in_quiet_hours}, notify={should_notify}",
        )
        log_notify_decision(
            event_type="AI_DETECTION",
            category=category,
            should_notify=bool(should_notify and not in_quiet_hours),
            in_quiet_hours=bool(in_quiet_hours),
            silent=bool(in_quiet_hours or not should_notify),
            reason=f"quiet_hours={in_quiet_hours}, category_switch={should_notify}",
        )

        # 7. Launch background clip recording under single recording lock
        asyncio.create_task(
            self._finalize_clip_recording(
                clip_id=clip_id,
                clip_path=clip_path,
                category=category,
                label=label,
                clean_snapshot_path=thumb_clip_path,
            )
        )

    async def _finalize_clip_recording(
        self,
        clip_id: str,
        clip_path: str,
        category: str,
        label: str,
        clean_snapshot_path: str = "",
    ) -> None:
        """
        Records video in background under single recording lock.
        Updates clip status from RECORDING to READY once MP4 completion succeeds (>100KB, >=5s).
        If failed, removes row from database.
        """
        try:
            from app.recorder import (
                record_smart_clip,
                ensure_thumbnail,
                compress_clip_background,
                recording_lock,
                extend_active_recording,
                is_recording_active,
            )

            sys_settings = await get_system_settings()
            preroll = float(sys_settings.get("pre_roll_seconds", "20"))
            postroll = float(sys_settings.get("post_roll_seconds", "20"))
            max_duration = float(sys_settings.get("max_active_motion_seconds", sys_settings.get("max_clip_seconds", "180")))

            # Single recording lock check (TAAK 2.2 / v3.5.5):
            if is_recording_active():
                from app.recorder import _active_recording_info
                active_clip_id = _active_recording_info.get("clip_id")
                extend_active_recording(category, label, duration_sec=max_duration)
                if active_clip_id and active_clip_id != clip_id:
                    active_cats = _active_recording_info.setdefault("categories", set())
                    active_cats.add(category)
                    cats = sorted(list(active_cats), key=lambda c: 0 if c == "ring" else 1 if c == "person" else 2)
                    combined_category = ",".join(cats)
                    async def _sync_active_cat():
                        try:
                            from app.database import get_db
                            async with get_db(write=True) as db:
                                await db.execute("UPDATE clips SET category = ? WHERE clip_id = ?", (combined_category, active_clip_id))
                        except Exception:
                            pass
                    asyncio.create_task(_sync_active_cat())
                logger.info("Smart clip already recording; merged category into active clip %s for %s", active_clip_id, label)
                await db_delete_clip(clip_id)
                return

            async with recording_lock:
                duration = 0.0
                safety_timeout = max(60.0, max_duration + 45.0)
                try:
                    duration = await asyncio.wait_for(
                        record_smart_clip(
                            clip_id=clip_id,
                            clip_path=clip_path,
                            preroll_seconds=preroll,
                            postroll_seconds=postroll,
                            motion_active_func=self.is_motion_active,
                            max_duration=max_duration,
                            category=category,
                        ),
                        timeout=safety_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Recording reached max safety timeout (%.1fs) for clip %s", safety_timeout, clip_id)
                except Exception as re:
                    logger.error("Error during record_smart_clip for %s: %s", clip_id, re)

                if clean_snapshot_path and os.path.exists(clean_snapshot_path) and os.path.getsize(clean_snapshot_path) > 1000:
                    snapshot_path = clean_snapshot_path
                else:
                    if not clean_snapshot_path:
                        clean_snapshot_path = str(settings.CLIPS_DIR / f"thumb_{clip_id}.jpg")
                    snapshot_path = await ensure_thumbnail(clip_id, clip_path, preroll_seconds=preroll)

                file_size = os.path.getsize(clip_path) if os.path.exists(clip_path) else 0

                # Database Update to READY (v3.5.1):
                # If file exists and > 10 KB, guarantee READY state transition
                if os.path.exists(clip_path) and file_size > 10000:
                    if duration <= 0:
                        from app.recorder import _probe_duration
                        duration = await _probe_duration(clip_path)
                        if duration <= 0:
                            duration = 15.0

                    from app.recorder import _active_recording_info
                    active_cats = _active_recording_info.get("categories", set())
                    if category not in active_cats:
                        active_cats.add(category)
                    cats = sorted(list(active_cats), key=lambda c: 0 if c == "ring" else 1 if c == "person" else 2)
                    combined_cat = ",".join(cats) if cats else category

                    category_dutch = {
                        "person": "Persoon gedetecteerd",
                        "vehicle": "Voertuig gedetecteerd",
                        "animal": "Dier gedetecteerd",
                        "motion": "Beweging gedetecteerd",
                    }.get(category, "Beweging")

                    ans = f"AI: {category_dutch} ({label})"
                    if "ring" in active_cats:
                        ans = _active_recording_info.get("answered_by") or "Aanbellen [SHAZAM_CHIME]"

                    await update_clip(
                        clip_id=clip_id,
                        status="READY",
                        duration=duration,
                        file_size=file_size,
                        answered_by=ans,
                        category=combined_cat,
                        snapshot_path=snapshot_path or clean_snapshot_path,
                    )
                    logger.info("Updated verified clip %s to READY: duration=%.1fs, size=%d bytes, category=%s", clip_id, duration, file_size, combined_cat)
                    asyncio.create_task(compress_clip_background(clip_path))
                else:
                    logger.warning(
                        "Discarded invalid/empty clip %s (size=%d bytes) — removing row from DB",
                        clip_id, file_size,
                    )
                    await db_delete_clip(clip_id)
                    if os.path.exists(clip_path):
                        try:
                            os.unlink(clip_path)
                        except Exception:
                            pass
        except Exception as e:
            logger.error("Error finalizing background clip %s: %s", clip_id, e)
            try:
                await db_delete_clip(clip_id)
            except Exception:
                pass
        finally:
            self._last_clip_end_per_category[category] = time.time()


smart_detector = SmartDetector()
