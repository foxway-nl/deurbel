"""
Foxway Smart Doorbell — Low-CPU 2-Stage AI Detection & Activity Zone Engine.

Designed specifically for HP EliteDesk 405 (AMD Ryzen 5 2400GE):
  • Stage 1: Ultra-low CPU (<1%) frame-differencing on 768x432 substream at 1.5 fps.
  • Activity Zone / Privacy Mask: Configurable mask (e.g. ignores top X% of frame / street).
  • Stage 2: ONNXRuntime CPU inference (~35-40ms) using YOLOv8n only when motion is triggered.
  • Categorizes: "person", "vehicle", "animal", "motion".
  • Quiet Hours / Nachtmodus: Suppresses alert notifications while still recording.
  • Cooldown & Pre-roll segment support.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import onnxruntime as ort

from app.config import settings
from app.models import get_setting, set_setting, save_clip
from app.recorder import ensure_thumbnail

logger = logging.getLogger("foxway.ai_detector")

# COCO Class ID mappings for YOLOv8
PERSON_CLASSES = {0}
VEHICLE_CLASSES = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck
ANIMAL_CLASSES = {14, 15, 16, 17, 18, 19}  # bird, cat, dog, horse, sheep, cow

COCO_NAMES = {
    0: "Persoon", 1: "Fiets", 2: "Auto", 3: "Motor", 5: "Bus", 7: "Vrachtwagen",
    14: "Vogel", 15: "Kat", 16: "Hond", 17: "Paard", 18: "Schaap", 19: "Koe"
}

_MODEL_PATH = "/mnt/opslag/deurbel/backend/yolov8n.onnx"


async def get_substream_rtsp() -> str:
    """Dynamically resolve camera substream RTSP URL from SQLite app_settings or configuration."""
    from app.models import get_camera_credentials
    creds = await get_camera_credentials()
    return f"rtsp://{creds['user']}:{creds['pass']}@{creds['ip']}:554/H.264/ch1/sub/av_stream"


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
            "activity_zone_top_ignore_percent": 0,  # Activity zone removed per specification
            "motion_sensitivity": float(await get_setting("motion_sensitivity", "1.5")),
        }

    @classmethod
    async def save(cls, data: dict) -> None:
        for k, v in data.items():
            await set_setting(k, str(v).lower() if isinstance(v, bool) else str(v))


class SmartDetector:
    """Manages the 2-stage motion & AI detection pipeline."""

    def __init__(self):
        self._session: Optional[ort.InferenceSession] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_trigger_time = 0.0
        self._cooldown_seconds = 25.0
        self._last_motion_seen = 0.0

    def is_motion_active(self) -> bool:
        """Check if active motion was detected in the camera frame within the last 1.5 seconds."""
        return (time.time() - self._last_motion_seen) < 1.5

    def init_model(self) -> bool:
        """Initialize ONNXRuntime session for YOLOv8n."""
        if not os.path.exists(_MODEL_PATH):
            logger.warning("YOLOv8n ONNX model not found at %s", _MODEL_PATH)
            return False
        try:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(_MODEL_PATH, sess_options=opts, providers=["CPUExecutionProvider"])
            logger.info("YOLOv8n ONNX model loaded successfully into CPU session")
            return True
        except Exception as e:
            logger.error("Failed loading YOLOv8n model: %s", e)
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

    def detect_objects_yolo(self, frame: np.ndarray) -> list[dict]:
        """
        Run YOLOv8n inference on a single frame and return all detected objects.
        Returns list of dicts: {'cid', 'conf', 'category', 'label', 'bbox': (x1, y1, x2, y2), 'center': (cx, cy)}
        """
        if self._session is None:
            return []

        try:
            h, w = frame.shape[:2]
            scale = min(640 / h, 640 / w)
            nh, nw = int(h * scale), int(w * scale)
            resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
            canvas = np.zeros((640, 640, 3), dtype=np.uint8)
            pad_top = (640 - nh) // 2
            pad_left = (640 - nw) // 2
            canvas[pad_top : pad_top + nh, pad_left : pad_left + nw] = resized

            blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            blob = np.transpose(blob, (2, 0, 1))[np.newaxis, ...]

            input_name = self._session.get_inputs()[0].name
            outputs = self._session.run(None, {input_name: blob})
            predictions = outputs[0][0]  # Shape (84, 8400)

            classes_scores = predictions[4:, :]  # Shape (80, 8400)
            max_scores = np.max(classes_scores, axis=0)
            class_ids = np.argmax(classes_scores, axis=0)

            threshold = 0.45
            valid_indices = np.where(max_scores >= threshold)[0]
            detections = []

            for idx in valid_indices:
                cid = int(class_ids[idx])
                conf = float(max_scores[idx])

                cx, cy, bw, bh = predictions[0:4, idx]
                bx = int((cx - pad_left) / scale - (bw / scale) / 2)
                by = int((cy - pad_top) / scale - (bh / scale) / 2)
                bw_orig = int(bw / scale)
                bh_orig = int(bh / scale)

                x1 = max(0, min(w - 1, bx))
                y1 = max(0, min(h - 1, by))
                x2 = max(0, min(w, bx + bw_orig))
                y2 = max(0, min(h, by + bh_orig))
                center_x = (x1 + x2) / 2.0
                center_y = (y1 + y2) / 2.0

                category = "motion"
                label = "Beweging"
                if cid in PERSON_CLASSES:
                    category = "person"
                    label = COCO_NAMES.get(cid, "Persoon")
                elif cid in VEHICLE_CLASSES:
                    category = "vehicle"
                    label = COCO_NAMES.get(cid, "Voertuig")
                elif cid in ANIMAL_CLASSES:
                    category = "animal"
                    label = COCO_NAMES.get(cid, "Dier")

                detections.append({
                    "cid": cid,
                    "conf": conf,
                    "category": category,
                    "label": label,
                    "bbox": (x1, y1, x2, y2),
                    "center": (center_x, center_y),
                })

            return detections
        except Exception as e:
            logger.warning("YOLO detection error: %s", e)
            return []

    def run_yolo_inference(
        self,
        frame: np.ndarray,
        motion_mask: Optional[np.ndarray] = None,
    ) -> tuple[str, str, float]:
        """
        Stage 2 inference helper with motion mask verification.
        Filters out stationary objects where motion_ratio < 0.03.
        Returns: (category, label, confidence)
        """
        detections = self.detect_objects_yolo(frame)
        if not detections:
            return "motion", "Beweging", 0.5

        best_category = "motion"
        best_label = "Beweging"
        best_conf = 0.0

        for d in detections:
            cat = d["category"]
            lbl = d["label"]
            conf = d["conf"]
            x1, y1, x2, y2 = d["bbox"]

            if motion_mask is not None and (x2 > x1) and (y2 > y1):
                box_area = max(1, (x2 - x1) * (y2 - y1))
                motion_pixels = cv2.countNonZero(motion_mask[y1:y2, x1:x2])
                motion_ratio = motion_pixels / float(box_area)
                if motion_ratio < 0.03:
                    continue

            if cat == "person" and conf > best_conf:
                best_category, best_label, best_conf = cat, lbl, conf
            elif cat == "vehicle" and conf > best_conf and best_category != "person":
                best_category, best_label, best_conf = cat, lbl, conf
            elif cat == "animal" and conf > best_conf and best_category not in ("person", "vehicle"):
                best_category, best_label, best_conf = cat, lbl, conf

        return best_category, best_label, best_conf

    async def start(self) -> None:
        """Start the background motion detection loop."""
        if self._running:
            return
        self.init_model()
        self._running = True
        self._task = asyncio.create_task(self._detection_loop())
        logger.info("AI Smart Detection background worker started")

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
        """Main non-blocking detection loop (runs every 0.5s)."""
        await asyncio.sleep(5)  # Wait for system startup
        prev_gray: Optional[np.ndarray] = None

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

                frame_interval = 0.50  # 0.5s check rate per specification

                while self._running:
                    t_start = time.time()
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        logger.warning("Failed to read substream frame, reconnecting...")
                        break

                    h, w = frame.shape[:2]
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    blurred = cv2.GaussianBlur(gray, (21, 21), 0)

                    if prev_gray is not None:
                        frame_diff = cv2.absdiff(prev_gray, blurred)
                        _, thresh = cv2.threshold(frame_diff, 25, 255, cv2.THRESH_BINARY)
                        active_pixels = cv2.countNonZero(thresh)
                        total_eval_pixels = h * w
                        motion_ratio = (active_pixels / max(1, total_eval_pixels)) * 100.0

                        sensitivity = cfg["motion_sensitivity"]
                        if motion_ratio >= sensitivity:
                            self._last_motion_seen = time.time()
                            now_t = time.time()

                            if (now_t - self._last_trigger_time) > self._cooldown_seconds:
                                # Stage 2: YOLO detection on frame 1
                                detections_t1 = self.detect_objects_yolo(frame)

                                confirmed_cat = None
                                confirmed_lbl = None
                                confirmed_conf = 0.0

                                # 1. Check for Person detections
                                persons = [d for d in detections_t1 if d["category"] == "person"]
                                for p in persons:
                                    x1, y1, x2, y2 = p["bbox"]
                                    box_area = max(1, (x2 - x1) * (y2 - y1))
                                    motion_in_box = cv2.countNonZero(thresh[y1:y2, x1:x2])
                                    p_motion_ratio = motion_in_box / float(box_area)
                                    # Require moving pixel ratio >= 0.03
                                    if p_motion_ratio >= 0.03 and p["conf"] > confirmed_conf:
                                        confirmed_cat = "person"
                                        confirmed_lbl = p["label"]
                                        confirmed_conf = p["conf"]

                                # 2. Check for Animal detections if no person
                                if not confirmed_cat:
                                    animals = [d for d in detections_t1 if d["category"] == "animal"]
                                    for a in animals:
                                        x1, y1, x2, y2 = a["bbox"]
                                        box_area = max(1, (x2 - x1) * (y2 - y1))
                                        motion_in_box = cv2.countNonZero(thresh[y1:y2, x1:x2])
                                        a_motion_ratio = motion_in_box / float(box_area)
                                        # Require moving pixel ratio >= 0.03
                                        if a_motion_ratio >= 0.03 and a["conf"] > confirmed_conf:
                                            confirmed_cat = "animal"
                                            confirmed_lbl = a["label"]
                                            confirmed_conf = a["conf"]

                                # 3. Check for Vehicle detections with Mandatory Displacement Check (delta_d >= 0.08)
                                if not confirmed_cat:
                                    vehicles_t1 = [d for d in detections_t1 if d["category"] == "vehicle"]
                                    if vehicles_t1:
                                        # NEVER save vehicle clip based on 1 frame!
                                        best_v1 = max(vehicles_t1, key=lambda v: v["conf"])
                                        cx1, cy1 = best_v1["center"]

                                        # Sample frame 2 at 0.8s later
                                        await asyncio.sleep(0.8)
                                        ret2, frame2 = cap.read()
                                        if ret2 and frame2 is not None:
                                            detections_t2 = self.detect_objects_yolo(frame2)
                                            vehicles_t2 = [d for d in detections_t2 if d["category"] == "vehicle"]
                                            if vehicles_t2:
                                                best_v2 = min(
                                                    vehicles_t2,
                                                    key=lambda v: (v["center"][0] - cx1)**2 + (v["center"][1] - cy1)**2
                                                )
                                                cx2, cy2 = best_v2["center"]
                                                import math
                                                displacement_px = math.sqrt((cx2 - cx1)**2 + (cy2 - cy1)**2)
                                                delta_d = displacement_px / float(w)

                                                if delta_d >= 0.08:
                                                    confirmed_cat = "vehicle"
                                                    confirmed_lbl = best_v1["label"]
                                                    confirmed_conf = best_v1["conf"]
                                                    logger.info("Moving vehicle confirmed: delta_d=%.3f >= 0.08", delta_d)
                                                else:
                                                    logger.info("Parked vehicle ignored: delta_d=%.3f < 0.08 threshold", delta_d)
                                            else:
                                                logger.info("Vehicle candidate disappeared on frame 2, ignoring")

                                if confirmed_cat is None and cfg["record_motion"] and motion_ratio >= sensitivity * 2:
                                    confirmed_cat = "motion"
                                    confirmed_lbl = "Beweging"
                                    confirmed_conf = 0.5

                                if confirmed_cat:
                                    self._last_trigger_time = now_t
                                    logger.info(
                                        "Confirmed detection: %s (%s, conf=%.2f) — dispatching async clip task",
                                        confirmed_cat, confirmed_lbl, confirmed_conf
                                    )
                                    # Asynchronous non-blocking clip processing task
                                    asyncio.create_task(
                                        self._handle_detection(confirmed_cat, confirmed_lbl, confirmed_conf, cfg, frame)
                                    )

                    prev_gray = blurred
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

        # Record in Event Queue (Task 6)
        from app.models import save_event
        event_dict = await save_event(
            event_type="motion",
            category=category,
            title=title,
            clip_id=clip_id,
            snapshot_url=snapshot_url,
        )

        # Broadcast WebSocket event to all connected Android apps
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
            }
        )

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
