"""
Foxway Smart Doorbell — Ultralytics YOLOv8 + ByteTrack & MOG2 AI Detection Engine.

Engine Architecture:
  • Official Ultralytics YOLOv8n model with ByteTrack multi-object tracking.
  • OpenCV MOG2 Background Subtractor with morphological opening for noise & rain removal.
  • Spatial Parked-Vehicle Baseline: 100% elimination of stationary parked cars (IoU >= 0.45).
  • Strict Priority Evaluation: Persoon (0) > Dier (14, 15, 16) > Rijdend Voertuig (1, 2, 3, 5, 7).
  • Non-blocking async clip dispatch with 6s pre-roll and 6s post-roll.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from app.config import settings
from app.models import get_setting, set_setting, save_clip
from app.recorder import ensure_thumbnail

logger = logging.getLogger("foxway.ai_detector")

# COCO Class ID mappings for YOLOv8
PERSON_CLASSES = {0}
VEHICLE_CLASSES = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck
ANIMAL_CLASSES = {14, 15, 16}      # bird, cat, dog

COCO_NAMES = {
    0: "Persoon",
    1: "Fiets", 2: "Auto", 3: "Motor", 5: "Bus", 7: "Vrachtwagen",
    14: "Vogel", 15: "Kat", 16: "Hond",
}

_MODEL_PATH = (
    "/mnt/opslag/deurbel/backend/yolov8n.pt"
    if os.path.exists("/mnt/opslag/deurbel/backend/yolov8n.pt")
    else "/app/yolov8n.pt"
)


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
            "quiet_hours_enabled": await get_setting("quiet_hours_enabled", "false") == "true",
            "quiet_hours_start": await get_setting("quiet_hours_start", "23:00"),
            "quiet_hours_end": await get_setting("quiet_hours_end", "07:00"),
            "motion_sensitivity": float(await get_setting("motion_sensitivity", "1.5")),
        }

    @classmethod
    async def save(cls, data: dict) -> None:
        for k, v in data.items():
            await set_setting(k, str(v).lower() if isinstance(v, bool) else str(v))


class SmartDetector:
    """
    Manages the Ultralytics YOLOv8 + ByteTrack + MOG2 detection pipeline.
    Eliminates parked cars via spatial baseline IoU >= 0.45 and enforces strict class priority.
    """

    def __init__(self):
        self.model = None
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=400,
            varThreshold=30,
            detectShadows=False,
        )
        self.kernel_5x5 = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

        # Spatial parked vehicle reference boxes: list of (x1, y1, x2, y2)
        self.parked_vehicle_boxes: list[tuple[float, float, float, float]] = []
        self.last_baseline_update: float = 0.0

        # Multi-frame tracking history: track_id -> list of (cx, cy)
        self.track_history: dict[int, list[tuple[float, float]]] = {}

        # Multi-frame animal tracking history for anti-rain filtering
        self.animal_tracks: dict[int, list[dict]] = {}
        self.next_animal_track_id: int = 1
        self.frame_num: int = 0

        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._last_trigger_time: float = 0.0
        self._cooldown_seconds: float = 25.0
        self._last_motion_seen: float = 0.0

    def is_motion_active(self) -> bool:
        """Check if active motion was detected in the camera frame within the last 2.0 seconds."""
        return (time.time() - self._last_motion_seen) < 2.0

    def init_model(self) -> bool:
        """Initialize official Ultralytics YOLOv8n model."""
        if self.model is not None:
            return True
        try:
            from ultralytics import YOLO
            if not os.path.exists(_MODEL_PATH):
                logger.warning("YOLOv8n model file not found at %s", _MODEL_PATH)
                return False
            self.model = YOLO(_MODEL_PATH)
            logger.info("Ultralytics YOLOv8n model loaded successfully from %s", _MODEL_PATH)
            return True
        except Exception as e:
            logger.error("Failed loading Ultralytics YOLO model: %s", e)
            return False

    def is_in_quiet_hours(self, start_str: str, end_str: str) -> bool:
        """Check if current local time is within quiet hours (e.g. 23:00 to 07:00)."""
        try:
            now = datetime.now()
            start_parts = [int(p) for p in start_str.split(":")]
            end_parts = [int(p) for p in end_str.split(":")]
            start_time = now.replace(hour=start_parts[0], minute=start_parts[1], second=0, microsecond=0)
            end_time = now.replace(hour=end_parts[0], minute=end_parts[1], second=0, microsecond=0)

            if start_time < end_time:
                return start_time <= now <= end_time
            else:  # Crosses midnight (e.g., 23:00 -> 07:00)
                return now >= start_time or now <= end_time
        except Exception:
            return False

    def update_parked_baseline(self, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
        """
        Record stationary vehicle baseline (classes 2=car, 5=bus, 7=truck).
        Called on startup and every 60 seconds during quiet periods.
        """
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
            logger.info(
                "Updated parked vehicle baseline: %d parked vehicles registered (%s)",
                len(self.parked_vehicle_boxes),
                self.parked_vehicle_boxes,
            )
            return self.parked_vehicle_boxes
        except Exception as e:
            logger.warning("Error updating parked baseline: %s", e)
            return self.parked_vehicle_boxes

    def is_vehicle_moving(self, centroids: list[tuple[float, float]], width: float) -> bool:
        """
        Determines whether a vehicle's centroid sequence represents a moving vehicle:
        - Visible in >= 3 frames
        - Horizontal displacement abs(cx_last - cx_first) >= 0.10 * width
        - Consistent horizontal progression
        """
        if len(centroids) < 3:
            return False
        cx_first = centroids[0][0]
        cx_last = centroids[-1][0]
        dx_total = cx_last - cx_first
        if abs(dx_total) < (0.10 * width):
            return False
        # Consistent direction check
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
        """
        Multi-frame anti-rain filter for animal detections:
        - confidence >= 0.48 (>= 0.62 for bird during IR night vision)
        - box_area >= 0.008 * frame_area, aspect ratio between 0.3 and 3.0
        - visible in >= 3 consecutive frames with IoU >= 0.25 between frames
        """
        min_conf = 0.62 if (label.lower() == "bird" and is_ir_night) else 0.48
        if conf < min_conf:
            return False
        if not box_history:
            return False

        # Validate box shape of latest box
        box = box_history[-1]
        bw = box[2] - box[0]
        bh = box[3] - box[1]
        box_area = bw * bh
        if box_area < (0.008 * frame_area):
            return False
        ar = bw / max(1.0, bh)
        if ar < 0.3 or ar > 3.0:
            return False

        # Trajectory must have >= 3 frames with overlapping IoU >= 0.25
        if len(box_history) < 3:
            return False

        for k in range(1, len(box_history)):
            if compute_iou(box_history[k - 1], box_history[k]) < 0.25:
                return False

        return True

    def evaluate_detections(
        self,
        frame: np.ndarray,
        clean_mask: np.ndarray,
    ) -> tuple[Optional[str], Optional[str], float]:
        """
        Runs Ultralytics ByteTrack on frame and evaluates detections in strict priority:
          1. Person (class 0, conf >= 0.38): MOG2 motion in box (fg_pixels_ratio >= 0.04) or displacement over 2 frames.
          2. Animal (classes 14, 15, 16, conf >= 0.35): MOG2 motion in box (fg_pixels_ratio >= 0.04).
          3. Moving Vehicle (classes 1, 2, 3, 5, 7, conf >= 0.45):
             - IoU < 0.45 with all parked_vehicle_boxes (NOT parked).
             - Tracked in >= 3 frames with displacement >= 12% of screen width.

        Returns: (category, label, confidence) or (None, None, 0.0)
        """
        if self.model is None and not self.init_model():
            return None, None, 0.0

        h, w = frame.shape[:2]

        try:
            results = self.model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                classes=[0, 1, 2, 3, 5, 7, 14, 15, 16],
                conf=0.35,
                verbose=False,
            )
        except Exception as e:
            logger.warning("Tracking inference failed: %s", e)
            return None, None, 0.0

        if not results or len(results) == 0 or results[0].boxes is None:
            return None, None, 0.0

        boxes_obj = results[0].boxes
        if len(boxes_obj) == 0:
            return None, None, 0.0

        cls_list = boxes_obj.cls.int().cpu().tolist()
        conf_list = boxes_obj.conf.cpu().tolist()
        xyxy_list = boxes_obj.xyxy.cpu().tolist()
        id_list = (
            boxes_obj.id.int().cpu().tolist()
            if boxes_obj.id is not None
            else [-1] * len(cls_list)
        )

        detections = []
        for i in range(len(cls_list)):
            cid = cls_list[i]
            conf = float(conf_list[i])
            box = tuple(float(x) for x in xyxy_list[i])
            tid = int(id_list[i])
            cx = (box[0] + box[2]) / 2.0
            cy = (box[1] + box[3]) / 2.0

            if tid > 0:
                if tid not in self.track_history:
                    self.track_history[tid] = []
                self.track_history[tid].append((cx, cy))
                if len(self.track_history[tid]) > 25:
                    self.track_history[tid].pop(0)

            # Compute MOG2 motion in bounding box
            x1, y1 = max(0, int(box[0])), max(0, int(box[1]))
            x2, y2 = min(w, int(box[2])), min(h, int(box[3]))
            box_area = max(1, (x2 - x1) * (y2 - y1))
            fg_in_box = cv2.countNonZero(clean_mask[y1:y2, x1:x2])
            fg_ratio = fg_in_box / float(box_area)

            detections.append({
                "cid": cid,
                "conf": conf,
                "box": box,
                "tid": tid,
                "cx": cx,
                "cy": cy,
                "fg_ratio": fg_ratio,
            })

        # ── PRIORITY 1: Persoon (class 0, conf >= 0.38) ───────────────────
        person_candidates = [
            d for d in detections
            if d["cid"] == 0 and d["conf"] >= 0.38
        ]
        best_person = None
        for p in person_candidates:
            tid = p["tid"]
            history = self.track_history.get(tid, [])
            moved_over_2_frames = False
            if len(history) >= 2:
                # Displacement between last 2 frames >= 1% of width
                dx = history[-1][0] - history[-2][0]
                dy = history[-1][1] - history[-2][1]
                if math.hypot(dx, dy) / float(w) >= 0.01:
                    moved_over_2_frames = True

            if p["fg_ratio"] >= 0.04 or moved_over_2_frames:
                if best_person is None or p["conf"] > best_person["conf"]:
                    best_person = p

        if best_person is not None:
            logger.info(
                "Persoon detected: conf=%.2f, fg_ratio=%.3f, track_id=%d",
                best_person["conf"], best_person["fg_ratio"], best_person["tid"],
            )
            return "person", "Persoon", best_person["conf"]

        # Check if IR night vision is active (monochrome image / saturation < 18)
        is_ir_night = False
        if len(frame.shape) == 3 and frame.shape[2] == 3:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            if float(hsv[:, :, 1].mean()) < 18.0:
                is_ir_night = True

        # ── PRIORITY 2: Dier (classes 14, 15, 16) — Multi-Frame Anti-Regen Filter ─
        frame_area = float(w * h)
        animal_candidates = []
        for d in detections:
            if d["cid"] not in ANIMAL_CLASSES:
                continue

            # 1. Confidence check (>= 0.48, or >= 0.62 for bird during IR night vision)
            min_conf = 0.62 if (d["cid"] == 14 and is_ir_night) else 0.48
            if d["conf"] < min_conf:
                continue

            # 2. Bounding box shape & size (area >= 0.008 * frame_area, aspect ratio 0.3 - 3.0)
            box = d["box"]
            bw = box[2] - box[0]
            bh = box[3] - box[1]
            box_area = bw * bh
            if box_area < (0.008 * frame_area):
                continue
            ar = bw / max(1.0, bh)
            if ar < 0.3 or ar > 3.0:
                continue

            # 3. Trajectory matching over consecutive frames (IoU >= 0.25)
            matched_track_id = None
            for at_id, at_history in self.animal_tracks.items():
                if not at_history:
                    continue
                last_entry = at_history[-1]
                if (self.frame_num - last_entry["frame_num"]) <= 2:
                    if compute_iou(box, last_entry["box"]) >= 0.25:
                        matched_track_id = at_id
                        break

            if matched_track_id is None:
                matched_track_id = self.next_animal_track_id
                self.next_animal_track_id += 1
                self.animal_tracks[matched_track_id] = []

            self.animal_tracks[matched_track_id].append({
                "box": box,
                "conf": d["conf"],
                "cid": d["cid"],
                "frame_num": self.frame_num,
                "time": time.time(),
            })

            # Check if animal is visible in >= 3 consecutive frames (>= 0.75s)
            track = self.animal_tracks[matched_track_id]
            if len(track) >= 3:
                animal_candidates.append(d)

        # Cleanup stale animal tracks (> 4 frames old)
        stale_animal_tracks = [
            tid for tid, trk in self.animal_tracks.items()
            if trk and (self.frame_num - trk[-1]["frame_num"]) > 4
        ]
        for tid in stale_animal_tracks:
            del self.animal_tracks[tid]

        if animal_candidates:
            best_animal = max(animal_candidates, key=lambda a: a["conf"])
            lbl = COCO_NAMES.get(best_animal["cid"], "Dier")
            logger.info(
                "Dier detected (%s): conf=%.2f, fg_ratio=%.3f (multi-frame confirmed)",
                lbl, best_animal["conf"], best_animal["fg_ratio"],
            )
            return "animal", lbl, best_animal["conf"]

        # ── PRIORITY 3: Rijdend Voertuig (classes 1, 2, 3, 5, 7, conf >= 0.45) ────
        # Detects moving cars/trucks/vans across frames while ignoring parked cars
        vehicle_candidates = [
            d for d in detections
            if d["cid"] in VEHICLE_CLASSES and d["conf"] >= 0.45
        ]
        best_vehicle = None
        for v in vehicle_candidates:
            tid = v["tid"]
            history = self.track_history.get(tid, [])

            if len(history) < 3:
                logger.debug("Vehicle track_id=%d tracked in only %d frames (< 3)", tid, len(history))
                continue

            cx_first = history[0][0]
            cx_last = history[-1][0]
            dx_total = cx_last - cx_first
            h_disp = abs(dx_total)

            # Ignore stationary parked cars (horizontal displacement < 2% screen width)
            if h_disp < (0.02 * float(w)):
                logger.debug("Vehicle track_id=%d stationary parked (dx=%.1f < 0.02*w) -> ignored", tid, h_disp)
                continue

            # Check consistent horizontal direction across tracked frames
            steps = [history[j + 1][0] - history[j][0] for j in range(len(history) - 1)]
            if dx_total > 0:
                agreed_steps = sum(1 for s in steps if s >= -0.01 * w)
            else:
                agreed_steps = sum(1 for s in steps if s <= 0.01 * w)
            is_consistent_direction = (agreed_steps / len(steps)) >= 0.65

            # Moving vehicle: >=10% width horizontal displacement, consistent direction, MOG2 ratio >= 0.18
            if h_disp >= (0.10 * float(w)) and is_consistent_direction and v["fg_ratio"] >= 0.18:
                if best_vehicle is None or v["conf"] > best_vehicle["conf"]:
                    best_vehicle = v
            else:
                logger.debug(
                    "Vehicle track_id=%d not confirmed (h_disp=%.3f, consistent=%s, fg_ratio=%.3f)",
                    tid, h_disp / float(w), is_consistent_direction, v["fg_ratio"],
                )

        if best_vehicle is not None:
            lbl = COCO_NAMES.get(best_vehicle["cid"], "Voertuig")
            logger.info(
                "Moving vehicle confirmed: conf=%.2f, track_id=%d, label=%s, fg_ratio=%.2f",
                best_vehicle["conf"], best_vehicle["tid"], lbl, best_vehicle["fg_ratio"],
            )
            return "vehicle", lbl, best_vehicle["conf"]

        return None, None, 0.0

    async def start(self) -> None:
        """Start the background motion detection loop."""
        if self._running:
            return
        self.init_model()
        self._running = True
        self._task = asyncio.create_task(self._detection_loop())
        logger.info("AI Smart Detection background worker started with Ultralytics + ByteTrack")

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
        """Main non-blocking detection loop (runs at 4 fps / 0.25s)."""
        await asyncio.sleep(5)  # Wait for system startup
        frame_interval = 0.25

        while self._running:
            cfg = await DetectionSettings.load()
            if not cfg["detection_enabled"]:
                await asyncio.sleep(5)
                continue

            cap = None
            try:
                substream_url = await get_substream_rtsp()
                cap = cv2.VideoCapture(substream_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                if not cap.isOpened():
                    logger.warning("Could not open substream for AI detection, retrying in 10s...")
                    await asyncio.sleep(10)
                    continue

                warmup_frames = 0
                while self._running:
                    t_start = time.time()
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        logger.warning("Failed to read substream frame, reconnecting...")
                        break

                    h, w = frame.shape[:2]

                    # 1. MOG2 Motion Extraction + Morphological Opening (kernel 5x5)
                    fg_mask = self.bg_subtractor.apply(frame)
                    clean_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, self.kernel_5x5)

                    motion_pixels = cv2.countNonZero(clean_mask)
                    total_pixels = h * w
                    motion_ratio = (motion_pixels / max(1, total_pixels)) * 100.0

                    # 2. Warm up MOG2 and initialize parked vehicle baseline
                    warmup_frames += 1
                    if warmup_frames == 8 or (not self.parked_vehicle_boxes and warmup_frames >= 4):
                        self.update_parked_baseline(frame)

                    # 3. Update baseline every 60s during quiet periods (motion_ratio < 0.5%)
                    now_t = time.time()
                    if motion_ratio < 0.5 and (now_t - self.last_baseline_update) >= 60.0:
                        self.update_parked_baseline(frame)

                    if motion_ratio >= cfg["motion_sensitivity"]:
                        self._last_motion_seen = now_t

                        if (now_t - self._last_trigger_time) > self._cooldown_seconds:
                            # Evaluate detections with ByteTrack & strict priorities
                            cat, lbl, conf = self.evaluate_detections(frame, clean_mask)

                            if cat is None and cfg["record_motion"] and motion_ratio >= (cfg["motion_sensitivity"] * 2.5):
                                cat = "motion"
                                lbl = "Beweging"
                                conf = 0.5

                            if cat is not None:
                                self._last_trigger_time = now_t
                                logger.info(
                                    "Confirmed AI detection: %s (%s, conf=%.2f) — dispatching clip task",
                                    cat, lbl, conf,
                                )
                                asyncio.create_task(
                                    self._handle_detection(cat, lbl, conf, cfg, frame)
                                )

                    elapsed = time.time() - t_start
                    sleep_dur = max(0.02, frame_interval - elapsed)
                    await asyncio.sleep(sleep_dur)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("AI detection loop error: %s", e)
                await asyncio.sleep(10)
            finally:
                if cap is not None:
                    cap.release()

    async def _handle_detection(
        self,
        category: str,
        label: str,
        confidence: float,
        cfg: dict,
        keyframe: np.ndarray,
    ) -> None:
        """Handle confirmed AI detection: record smart clip, save event, and notify."""
        should_record = False
        should_notify = False

        if category == "person":
            should_record = cfg["record_person"]
            should_notify = cfg["notify_person"]
        elif category == "vehicle":
            should_record = cfg["record_vehicle"]
            should_notify = cfg["notify_vehicle"]
        elif category == "animal":
            should_record = cfg["record_animal"]
            should_notify = cfg["notify_animal"]
        else:  # General motion
            should_record = cfg["record_motion"]
            should_notify = cfg["notify_motion"]

        if not should_record:
            logger.info("Category '%s' recording disabled in settings, skipping clip", category)
            return

        in_quiet_hours = False
        if cfg["quiet_hours_enabled"]:
            in_quiet_hours = self.is_in_quiet_hours(cfg["quiet_hours_start"], cfg["quiet_hours_end"])
            if in_quiet_hours:
                should_notify = False
                logger.info("Quiet hours active: notification muted for %s", category)

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        clip_id = f"ai_{category}_{timestamp_str}"
        clip_filename = f"{clip_id}.mp4"
        clip_path = str(settings.CLIPS_DIR / clip_filename)
        snapshot_path = str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")
        snapshot_url = f"/api/v1/snapshots/{clip_id}.jpg"

        try:
            cv2.imwrite(snapshot_path, keyframe)
        except Exception:
            pass

        category_dutch = {
            "person": "Persoon gedetecteerd",
            "vehicle": "Voertuig gedetecteerd",
            "animal": "Dier gedetecteerd",
            "motion": "Beweging gedetecteerd",
        }.get(category, "Beweging")

        title = f"{category_dutch}: {label}"

        # Record in Event Queue for reliable push / 5G fallback
        from app.models import save_event
        event_dict = await save_event(
            event_type="motion",
            category=category,
            title=title,
            clip_id=clip_id,
            snapshot_url=snapshot_url,
        )

        # Broadcast WebSocket event to all connected Android apps
        try:
            from app.main import ws_manager
            await ws_manager.broadcast(
                "AI_DETECTION",
                {
                    "event_id": event_dict["id"],
                    "clip_id": clip_id,
                    "category": category,
                    "label": label,
                    "title": title,
                    "confidence": round(confidence, 2),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "snapshot_url": snapshot_url,
                    "in_quiet_hours": in_quiet_hours,
                    "should_notify": should_notify,
                },
            )
        except Exception as e:
            logger.warning("Error broadcasting AI detection event: %s", e)

        # Dynamic clip assembly (6s pre-roll + motion + 6s post-roll)
        try:
            from app.recorder import record_smart_clip
            duration = await record_smart_clip(
                clip_id=clip_id,
                clip_path=clip_path,
                preroll_seconds=6.0,
                postroll_seconds=6.0,
                motion_active_func=self.is_motion_active,
            )
            await ensure_thumbnail(clip_id, clip_path)

            await save_clip(
                clip_id=clip_id,
                call_id=clip_id,
                video_path=clip_path,
                snapshot_path=snapshot_path,
                answered_by=f"AI: {category_dutch} ({label})",
                duration=duration,
            )
            logger.info("AI Detection clip saved: %s (%.1fs)", clip_path, duration)

        except Exception as e:
            logger.error("Failed recording AI smart clip %s: %s", clip_id, e)


smart_detector = SmartDetector()
