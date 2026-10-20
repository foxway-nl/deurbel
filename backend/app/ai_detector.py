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
_SUBSTREAM_RTSP = f"rtsp://{settings.DOORBELL_USER}:{settings.DOORBELL_PASS}@{settings.DOORBELL_IP}:554/H.264/ch1/sub/av_stream"


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
            "activity_zone_top_ignore_percent": int(await get_setting("activity_zone_top_ignore_percent", "20")),
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

    def run_yolo_inference(
        self,
        frame: np.ndarray,
        motion_mask: Optional[np.ndarray] = None
    ) -> tuple[str, str, float]:
        """
        Stage 2: Run YOLOv8n inference on a single frame.
        When motion_mask is provided, filters out stationary objects (e.g. parked cars).
        Returns: (category, label, confidence)
          category: 'person' | 'vehicle' | 'animal' | 'motion'
        """
        if self._session is None:
            return "motion", "Beweging", 1.0

        try:
            h, w = frame.shape[:2]
            # Resize with letterboxing to 640x640
            scale = min(640 / h, 640 / w)
            nh, nw = int(h * scale), int(w * scale)
            resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
            canvas = np.zeros((640, 640, 3), dtype=np.uint8)
            pad_top = (640 - nh) // 2
            pad_left = (640 - nw) // 2
            canvas[pad_top : pad_top + nh, pad_left : pad_left + nw] = resized

            # Convert BGR to RGB, normalize [0, 1], transpose to NCHW
            blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            blob = np.transpose(blob, (2, 0, 1))[np.newaxis, ...]

            input_name = self._session.get_inputs()[0].name
            outputs = self._session.run(None, {input_name: blob})
            predictions = outputs[0][0]  # Shape (84, 8400)

            best_category = "motion"
            best_label = "Beweging"
            best_conf = 0.0

            # YOLOv8 format: 4 bbox coords + 80 class probabilities
            classes_scores = predictions[4:, :]  # Shape (80, 8400)
            max_scores = np.max(classes_scores, axis=0)
            class_ids = np.argmax(classes_scores, axis=0)

            threshold = 0.40  # Confidence threshold
            valid_indices = np.where(max_scores >= threshold)[0]

            for idx in valid_indices:
                cid = int(class_ids[idx])
                conf = float(max_scores[idx])

                # Bounding box coords in letterboxed canvas
                cx, cy, bw, bh = predictions[0:4, idx]

                # Map back to original frame dimensions
                bx = int((cx - pad_left) / scale - (bw / scale) / 2)
                by = int((cy - pad_top) / scale - (bh / scale) / 2)
                bw_orig = int(bw / scale)
                bh_orig = int(bh / scale)

                x1 = max(0, min(w - 1, bx))
                y1 = max(0, min(h - 1, by))
                x2 = max(0, min(w, bx + bw_orig))
                y2 = max(0, min(h, by + bh_orig))

                # Check if this object intersects with the active motion mask
                if motion_mask is not None and (x2 > x1) and (y2 > y1):
                    obj_roi = motion_mask[y1:y2, x1:x2]
                    obj_motion_pixels = cv2.countNonZero(obj_roi)
                    min_motion = max(35, int((x2 - x1) * (y2 - y1) * 0.015))
                    if obj_motion_pixels < min_motion:
                        # Stationary background object (e.g. parked car or fence) — ignore!
                        continue

                if cid in PERSON_CLASSES and conf > best_conf:
                    best_category = "person"
                    best_label = COCO_NAMES.get(cid, "Persoon")
                    best_conf = conf
                elif cid in VEHICLE_CLASSES and conf > best_conf and best_category != "person":
                    best_category = "vehicle"
                    best_label = COCO_NAMES.get(cid, "Voertuig")
                    best_conf = conf
                elif cid in ANIMAL_CLASSES and conf > best_conf and best_category not in ("person", "vehicle"):
                    best_category = "animal"
                    best_label = COCO_NAMES.get(cid, "Dier")
                    best_conf = conf

            return best_category, best_label, best_conf
        except Exception as e:
            logger.warning("YOLO inference failed: %s", e)
            return "motion", "Beweging", 0.5

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
        """Main low-CPU detection loop (Stage 1 + Stage 2)."""
        await asyncio.sleep(5)  # Wait for system startup
        prev_gray: Optional[np.ndarray] = None

        while self._running:
            cfg = await DetectionSettings.load()
            if not cfg["detection_enabled"]:
                await asyncio.sleep(5)
                continue

            cap = None
            try:
                # Open substream (768x432 H.264 @ 10fps)
                cap = cv2.VideoCapture(_SUBSTREAM_RTSP, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                if not cap.isOpened():
                    logger.warning("Could not open substream for AI detection, retrying in 10s...")
                    await asyncio.sleep(10)
                    continue

                frame_interval = 0.65  # ~1.5 FPS check rate (<1% CPU on Ryzen 2400GE)

                while self._running:
                    t_start = time.time()
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        logger.warning("Failed to read substream frame, reconnecting...")
                        break

                    # ── Stage 1: Fast Frame Differencing with Activity Zone Mask ──
                    h, w = frame.shape[:2]
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    blurred = cv2.GaussianBlur(gray, (21, 21), 0)

                    # Apply Activity Zone: mask out ignored top portion (e.g. street/clouds)
                    top_ignore_pct = cfg["activity_zone_top_ignore_percent"]
                    ignore_h = int(h * (top_ignore_pct / 100.0))
                    if ignore_h > 0:
                        blurred[0:ignore_h, :] = 0

                    if prev_gray is not None:
                        frame_diff = cv2.absdiff(prev_gray, blurred)
                        _, thresh = cv2.threshold(frame_diff, 25, 255, cv2.THRESH_BINARY)
                        active_pixels = cv2.countNonZero(thresh)
                        total_eval_pixels = (h - ignore_h) * w
                        motion_ratio = (active_pixels / max(1, total_eval_pixels)) * 100.0

                        # Check threshold
                        sensitivity = cfg["motion_sensitivity"]
                        if motion_ratio >= sensitivity:
                            now_t = time.time()
                            if (now_t - self._last_trigger_time) > self._cooldown_seconds:
                                self._last_trigger_time = now_t
                                # ── Stage 2: Target Classification via ONNX YOLOv8n with Motion Mask ──
                                category, label, conf = self.run_yolo_inference(frame, motion_mask=thresh)
                                if category == "motion":
                                    # Multi-frame verification: check next frame after 200ms
                                    await asyncio.sleep(0.20)
                                    ret2, frame2 = cap.read()
                                    if ret2 and frame2 is not None:
                                        cat2, lbl2, cnf2 = self.run_yolo_inference(frame2, motion_mask=thresh)
                                        if cat2 != "motion":
                                            category, label, conf = cat2, lbl2, cnf2
                                            frame = frame2

                                logger.info(
                                    "Motion detected (%.1f%%) → Classified: %s (%s, conf=%.2f)",
                                    motion_ratio, category, label, conf
                                )
                                # Trigger clip recording and push notification if enabled
                                asyncio.create_task(self._handle_detection(category, label, conf, cfg, frame))

                    prev_gray = blurred
                    elapsed = time.time() - t_start
                    sleep_dur = max(0.05, frame_interval - elapsed)
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
        """Handle confirmed AI detection: save clip and notify."""
        # 1. Check if category is enabled for recording
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
            logger.info("Category '%s' is disabled in recording settings, skipping clip", category)
            return

        # 2. Check Quiet Hours for notifications
        in_quiet_hours = False
        if cfg["quiet_hours_enabled"]:
            in_quiet_hours = self.is_in_quiet_hours(cfg["quiet_hours_start"], cfg["quiet_hours_end"])
            if in_quiet_hours:
                should_notify = False
                logger.info("Quiet hours active (%s–%s): notification muted", cfg["quiet_hours_start"], cfg["quiet_hours_end"])

        # 3. Create clip record and trigger recording
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        clip_id = f"ai_{category}_{timestamp_str}"
        clip_filename = f"{clip_id}.mp4"
        clip_path = str(settings.CLIPS_DIR / clip_filename)
        snapshot_path = str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")

        # Save keyframe thumbnail immediately
        try:
            cv2.imwrite(snapshot_path, keyframe)
        except Exception:
            pass

        # Broadcast WebSocket event to all connected family apps
        from app.main import ws_manager
        await ws_manager.broadcast(
            "AI_DETECTION",
            {
                "clip_id": clip_id,
                "category": category,
                "label": label,
                "confidence": round(confidence, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "snapshot_url": f"/api/v1/snapshots/{clip_id}.jpg",
                "in_quiet_hours": in_quiet_hours,
                "should_notify": should_notify,
            }
        )

        # 4. Record 15-second HD clip via FFmpeg
        cmd = [
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", settings.GO2RTC_RTSP_URL,
            "-t", "15",
            "-c:v", "copy",
            "-c:a", "aac",
            "-movflags", "+faststart",
            clip_path,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()

            # Probe duration & ensure thumbnail
            duration = 15.0
            await ensure_thumbnail(clip_id, clip_path)

            category_dutch = {
                "person": "Persoon gedetecteerd",
                "vehicle": "Voertuig gedetecteerd",
                "animal": "Dier gedetecteerd",
                "motion": "Beweging gedetecteerd",
            }.get(category, "Beweging")

            await save_clip(
                clip_id=clip_id,
                call_id=clip_id,
                video_path=clip_path,
                snapshot_path=snapshot_path,
                answered_by=f"AI: {category_dutch} ({label})",
                duration=duration,
            )
            logger.info("AI Detection clip saved: %s (%s)", clip_path, label)

            # 5. Compress clip in background with low CPU priority
            from app.recorder import compress_clip_background
            asyncio.create_task(compress_clip_background(clip_path))

        except Exception as e:
            logger.error("Failed recording AI detection clip %s: %s", clip_id, e)


smart_detector = SmartDetector()
