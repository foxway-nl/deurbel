"""
Foxway Smart Doorbell — Open-Source Supervision + Frigate Motion-Crop AI Detection Engine.

Architecture:
  • Roboflow Supervision (sv.Detections, sv.ByteTrack, sv.DetectionsSmoother)
  • Ultralytics YOLOv8s (Small) on High-Resolution Motion Crops (1280x960, 4:3)
  • Frigate-Style Motion-Crop:
      - Gaussian blur + adaptive running background average (cv2.accumulateWeighted).
      - Isolates active motion contours (area >= 0.003 * frame_area) with context padding.
      - 0 YOLO calls in rest when scene is static (parked cars trigger 0 inferences).
  • Strict Parked Car Elimination:
      - Cars/trucks/buses with < 25% pixel change in their box OR displacement < 12% screen width
        are dropped immediately as stationary/parked.
  • Person & Animal Validation:
      - Confirmed by ByteTrack in >= 3 frames with active internal box motion, filtering rain streaks.
  • Strict 1-to-1 Coupling:
      - Clips recorded first and saved in clips table -> ONLY THEN event saved in events table.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import supervision as sv
from ultralytics import YOLO

from app.config import settings
from app.models import get_setting, set_setting, save_clip, save_event
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
    "/mnt/opslag/deurbel/backend/yolov8s.pt"
    if os.path.exists("/mnt/opslag/deurbel/backend/yolov8s.pt")
    else (
        "/app/yolov8s.pt"
        if os.path.exists("/app/yolov8s.pt")
        else "yolov8s.pt"
    )
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
    Frigate-style Motion-Crop + Supervision ByteTrack AI Pipeline.
    Runs on 1280x960 4:3 video stream at 5 fps.
    """

    def __init__(self):
        self.model: Optional[YOLO] = None
        self.tracker: Optional[sv.ByteTrack] = None
        self.smoother: Optional[sv.DetectionsSmoother] = None

        # Adaptive background model for Motion-Crop
        self.avg_bg: Optional[np.ndarray] = None
        self.kernel_5x5 = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

        # Spatial parked vehicle reference boxes: list of (x1, y1, x2, y2)
        self.parked_vehicle_boxes: list[tuple[float, float, float, float]] = []
        self.last_baseline_update: float = 0.0

        # ByteTrack object tracking metrics
        self.track_origins: dict[int, tuple[float, float]] = {}       # tid -> (orig_cx, orig_cy)
        self.track_frame_counts: dict[int, int] = {}                  # tid -> frame count
        self.track_history: dict[int, list[tuple[float, float]]] = {} # tid -> list of (cx, cy)

        # Worker state
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._last_trigger_time: float = 0.0
        self._cooldown_seconds: float = 25.0
        self._last_motion_seen: float = 0.0

    def is_motion_active(self) -> bool:
        """Check if active motion was detected in the camera frame within the last 2.0 seconds."""
        return (time.time() - self._last_motion_seen) < 2.0

    def init_model(self) -> bool:
        """Initialize official Ultralytics YOLOv8s model and Roboflow Supervision components."""
        if self.model is not None and self.tracker is not None:
            return True
        try:
            self.model = YOLO(_MODEL_PATH)
            self.tracker = sv.ByteTrack(lost_track_buffer=30, minimum_matching_threshold=0.8)
            self.smoother = sv.DetectionsSmoother(length=3)
            logger.info("Ultralytics YOLOv8s + Supervision ByteTrack initialized from %s", _MODEL_PATH)
            return True
        except Exception as e:
            logger.error("Failed loading YOLOv8s/Supervision: %s", e)
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
            logger.info("Updated parked vehicle baseline: %d vehicles registered", len(self.parked_vehicle_boxes))
            return self.parked_vehicle_boxes
        except Exception as e:
            logger.warning("Error updating parked baseline: %s", e)
            return self.parked_vehicle_boxes

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
        Frigate-Style Step A: Find moving regions using adaptive background subtraction.
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

        # Threshold difference and dilate to eliminate rain noise
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        dilated = cv2.dilate(thresh, self.kernel_5x5, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_area = w * h
        min_contour_area = 0.003 * frame_area  # e.g. 3686 px on 1280x960

        valid_contours = [c for c in contours if cv2.contourArea(c) >= min_contour_area]
        if not valid_contours:
            # Zero motion: DO NOT call YOLO! Parked cars in rest trigger 0 inferences.
            return None, (0, 0, 0, 0), thresh

        # Combine all moving contours
        all_pts = np.vstack([c.reshape(-1, 2) for c in valid_contours])
        min_x, min_y = all_pts.min(axis=0)
        max_x, max_y = all_pts.max(axis=0)

        # 20% context padding around moving area
        bw = max_x - min_x
        bh = max_y - min_y
        pad_x = int(bw * 0.20)
        pad_y = int(bh * 0.20)

        x1 = max(0, min_x - pad_x)
        y1 = max(0, min_y - pad_y)
        x2 = min(w, max_x + pad_x)
        y2 = min(h, max_y + pad_y)

        # Minimum crop box 320x320 and square-ish
        side = max(x2 - x1, y2 - y1, 320)
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        crop_x1 = max(0, min(w - side, cx - side // 2))
        crop_y1 = max(0, min(h - side, cy - side // 2))
        crop_x2 = min(w, crop_x1 + side)
        crop_y2 = min(h, crop_y1 + side)

        crop_img = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        return crop_img, (crop_x1, crop_y1, crop_x2, crop_y2), thresh

    def evaluate_detections(
        self,
        frame: np.ndarray,
        clean_mask: Optional[np.ndarray] = None,
    ) -> tuple[Optional[str], Optional[str], float]:
        """
        Step B & Step C: Runs YOLOv8s + supervision on motion crop, translating coordinates
        and evaluating per-object pixel change and ByteTrack displacement.
        """
        if self.model is None and not self.init_model():
            return None, None, 0.0

        h, w = frame.shape[:2]

        # Step A: Extract motion crop
        if clean_mask is not None:
            # If clean_mask is explicitly provided (e.g. in test fixture), use it directly
            thresh = clean_mask
            if cv2.countNonZero(clean_mask) < (0.003 * w * h):
                return None, None, 0.0
            crop_img = frame
            crop_x1, crop_y1 = 0, 0
        else:
            crop_img, (crop_x1, crop_y1, crop_x2, crop_y2), thresh = self.extract_motion_crop(frame)
            if crop_img is None:
                # In rest: 0 detections
                return None, None, 0.0

        # Step B: Run YOLOv8s on high-resolution motion crop
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

        # Convert to Roboflow supervision Detections
        detections = sv.Detections.from_ultralytics(results[0])
        if len(detections) == 0:
            return None, None, 0.0

        # Translate box coordinates back to full 1280x960 frame
        if crop_x1 > 0 or crop_y1 > 0:
            detections.xyxy[:, [0, 2]] += crop_x1
            detections.xyxy[:, [1, 3]] += crop_y1

        # Track with ByteTrack and smooth with DetectionsSmoother
        try:
            detections = self.tracker.update_with_detections(detections)
            if self.smoother is not None:
                detections = self.smoother.update_with_detections(detections)
        except Exception as e:
            logger.warning("ByteTrack/Smoother update failed: %s", e)

        if len(detections) == 0:
            return None, None, 0.0

        # Step C: Evaluate per-object pixel change and displacement
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

            # Update tracking history
            self.track_frame_counts[tid] = self.track_frame_counts.get(tid, 0) + 1
            if tid not in self.track_origins:
                self.track_origins[tid] = (cx, cy)
            orig_cx, orig_cy = self.track_origins[tid]

            if tid not in self.track_history:
                self.track_history[tid] = []
            self.track_history[tid].append((cx, cy))
            if len(self.track_history[tid]) > 25:
                self.track_history[tid].pop(0)

            # Compute percentage of pixels within this specific bounding box having motion (diff > 25)
            changed_pixels = cv2.countNonZero(thresh[by1:by2, bx1:bx2])
            pixel_change_ratio = changed_pixels / float(box_area)

            # Horizontal displacement and total displacement
            h_disp = abs(cx - orig_cx)
            tot_disp = math.hypot(cx - orig_cx, cy - orig_cy)
            disp_ratio = tot_disp / screen_w

            # 1. Person: priority #1
            if cid in PERSON_CLASSES:
                if conf < 0.45:
                    continue
                if self.track_frame_counts.get(tid, 0) < 3 and len(self.track_history.get(tid, [])) < 3:
                    continue
                if pixel_change_ratio < 0.04:
                    continue
                # Rain streak check: narrow vertical streak
                if bh > 0 and (bw / float(bh)) < 0.15:
                    continue
                if best_person is None or conf > best_person[2]:
                    best_person = ("person", "Persoon", conf)

            # 2. Animal: priority #2
            elif cid in ANIMAL_CLASSES:
                if conf < 0.45:
                    continue
                if self.track_frame_counts.get(tid, 0) < 3 and len(self.track_history.get(tid, [])) < 3:
                    continue
                if pixel_change_ratio < 0.04:
                    continue
                aspect_ratio = bw / max(1.0, float(bh))
                if aspect_ratio < 0.3 or aspect_ratio > 3.0:
                    continue
                if box_area < (0.003 * (w * h)):
                    continue
                if best_animal is None or conf > best_animal[2]:
                    best_animal = ("animal", COCO_NAMES.get(cid, "Dier"), conf)

            # 3. Vehicle: priority #3 (Enforces strict parked car exclusion)
            elif cid in VEHICLE_CLASSES:
                # Parked Car Exclusion:
                # If car/truck/bus has < 25% pixel change OR displacement < 12% screen width -> PARKED!
                if cid in {2, 5, 7}:  # car, bus, truck
                    if pixel_change_ratio < 0.25 or disp_ratio < 0.12:
                        logger.debug(
                            "Parked car ignored (tid=%d, px_change=%.2f < 0.25, disp=%.2f < 0.12)",
                            tid, pixel_change_ratio, disp_ratio,
                        )
                        continue
                else:  # bicycle, motorcycle
                    if disp_ratio < 0.08 or pixel_change_ratio < 0.15:
                        continue

                # Valid moving vehicle confirmed!
                if best_vehicle is None or conf > best_vehicle[2]:
                    best_vehicle = ("vehicle", COCO_NAMES.get(cid, "Voertuig"), conf)

        # Strict Priority Evaluation: Person > Animal > Vehicle
        if best_person:
            return best_person
        if best_animal:
            return best_animal
        if best_vehicle:
            return best_vehicle

        return None, None, 0.0

    async def start(self) -> None:
        """Start the background motion detection loop."""
        if self._running:
            return
        self.init_model()
        self._running = True
        self._task = asyncio.create_task(self._detection_loop())
        logger.info("AI Smart Detection background worker started with Supervision + Motion-Crop")

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
        """Main non-blocking detection loop (runs at 5 fps / 0.20s interval on 1280x960)."""
        await asyncio.sleep(5)  # Wait for system startup
        frame_interval = 0.20

        while self._running:
            cfg = await DetectionSettings.load()
            if not cfg["detection_enabled"]:
                await asyncio.sleep(5)
                continue

            cap = None
            try:
                # Prefer local go2rtc main stream for high 4:3 resolution (1280x960)
                stream_url = await get_mainstream_rtsp()
                cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                if not cap.isOpened():
                    logger.warning("Could not open stream for AI detection, falling back to substream in 5s...")
                    await asyncio.sleep(5)
                    substream_url = await get_substream_rtsp()
                    cap = cv2.VideoCapture(substream_url, cv2.CAP_FFMPEG)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if not cap.isOpened():
                        await asyncio.sleep(10)
                        continue

                warmup_frames = 0
                while self._running:
                    t_start = time.time()
                    ret, raw_frame = cap.read()
                    if not ret or raw_frame is None:
                        logger.warning("Failed to read stream frame, reconnecting...")
                        break

                    # Resize to crisp 4:3 high-res frame (1280x960)
                    frame = cv2.resize(raw_frame, (1280, 960))
                    now_t = time.time()

                    # Evaluate detections with Frigate Motion-Crop + Supervision
                    cat, lbl, conf = self.evaluate_detections(frame)

                    if cat is not None:
                        self._last_motion_seen = now_t
                        if (now_t - self._last_trigger_time) > self._cooldown_seconds:
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
        """
        Handle confirmed AI detection:
        Strict 1-to-1 Coupling: Record clip first and persist in clips table -> ONLY THEN save event.
        """
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
        else:
            should_record = cfg["record_motion"]
            should_notify = cfg["notify_motion"]

        if not should_record:
            logger.info("Category '%s' recording disabled in settings, skipping clip", category)
            return

        in_quiet_hours = False
        if cfg["quiet_hours_enabled"]:
            in_quiet_hours = self.is_in_quiet_hours(
                cfg["quiet_hours_start"], cfg["quiet_hours_end"]
            )

        clip_id = str(uuid.uuid4())
        clip_path = str(settings.CLIPS_DIR / f"{clip_id}.mp4")
        snapshot_path = str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")

        # Save snapshot image
        cv2.imwrite(snapshot_path, keyframe)
        snapshot_url = f"/api/v1/snapshots/{clip_id}.jpg"

        category_dutch = {
            "person": "Persoon gedetecteerd",
            "vehicle": "Voertuig gedetecteerd",
            "animal": "Dier gedetecteerd",
            "motion": "Beweging gedetecteerd",
        }.get(category, "Beweging")

        title = f"{category_dutch}: {label}"

        # 1. Record the smart clip first
        try:
            from app.recorder import record_smart_clip
            duration = await record_smart_clip(
                clip_id=clip_id,
                clip_path=clip_path,
                preroll_seconds=6.0,
                postroll_seconds=6.0,
                motion_active_func=self.is_motion_active,
            )
            if duration <= 0.0:
                logger.warning("Smart clip %s produced 0 duration, skipping event creation", clip_id)
                return

            await ensure_thumbnail(clip_id, clip_path)

            # 2. Save clip in clips table
            await save_clip(
                clip_id=clip_id,
                call_id=clip_id,
                video_path=clip_path,
                snapshot_path=snapshot_path,
                answered_by=f"AI: {category_dutch} ({label})",
                duration=duration,
            )
            logger.info("AI Detection clip saved: %s (%.1fs)", clip_path, duration)

            # 3. ONLY after clip is confirmed in clips table: Save event in events table
            event_dict = await save_event(
                event_type="motion",
                category=category,
                title=title,
                clip_id=clip_id,
                snapshot_url=snapshot_url,
            )

            # 4. Broadcast WebSocket event to all connected Android apps
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

            # 5. Send FCM push notification if enabled and not in quiet hours
            if should_notify and not in_quiet_hours:
                from app.main import push_fcm_notification
                await push_fcm_notification(
                    title=title,
                    body="Beweging waargenomen bij de voordeur",
                    data={
                        "type": "motion",
                        "event_id": str(event_dict["id"]),
                        "clip_id": clip_id,
                        "snapshot_url": snapshot_url,
                    },
                )

        except Exception as e:
            logger.error("Failed recording AI smart clip %s: %s", clip_id, e)


smart_detector = SmartDetector()
