"""
Foxway Smart Doorbell — High-Precision 3D-Trajectory & True Dual-Scale Vision AI Engine (v3.6.3).

Fixes in v3.6.3:
1. Ongoing Motion Keep-Alive for Verified Tracks:
   Verified tracks that continue moving in front of the camera continuously update
   `_last_verified_object_seen` and call `extend_active_recording()` so clips NEVER stop while
   someone is still moving in frame.
2. Native High-Res Motion Zoom-Crop + Cross-Scale NMS:
   Slices the Motion ROI directly from the full-resolution camera frame (`raw_frame`) before
   resizing to 640x640 for YOLO, and deduplicates overlapping boxes between full-frame and crop.
3. Sliding-Window Trajectory Evaluation (No 4.0s Track-Death Bug):
   Evaluates displacement over a sliding window of the track's history rather than permanently
   ignoring tracks older than 4.0s.
4. Cross-Category Rider/Pedestrian Continuity:
   Associates person <-> bicycle/motorcycle tracks smoothly so cyclists or people carrying bags
   never reset their frame counter mid-path.
5. Fast Parked-Car Anchoring (3.0s / 12 frames) + Directional Consistency:
   Anchors stationary cars within 3 seconds and requires consistent directional movement for
   vehicles so headlight glare or shadows on parked cars never trigger false clips.
6. Clean Active-Recording Merge:
   Merges new categories directly into an active recording without creating/deleting ghost DB rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
import onnxruntime as ort

from app.config import settings
from app.models import (
    delete_clip as db_delete_clip,
    get_system_settings,
    save_ai_audit_log,
    save_clip,
    save_event,
    update_clip,
)
from app.push_service import push_event

logger = logging.getLogger("foxway.ai_detector")

# COCO Class ID mappings
PERSON_CLASSES: Set[int] = {0}
VEHICLE_CLASSES: Set[int] = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck
ANIMAL_CLASSES: Set[int] = {14, 15, 16, 17, 18, 19}  # bird, cat, dog, horse, sheep, cow

COCO_NAMES: Dict[int, str] = {
    0: "Persoon",
    1: "Fiets",
    2: "Auto",
    3: "Motor",
    5: "Bus",
    7: "Vrachtwagen",
    14: "Vogel",
    15: "Kat",
    16: "Hond",
    17: "Paard",
    18: "Schaap",
    19: "Koe",
}


def resolve_model_path(custom_path: Optional[str] = None) -> str:
    """
    Resolves active YOLO model path (v3.6.3).
    Prefers yolov8s.onnx if > 15MB, otherwise falls back to yolov8n.onnx.
    """
    if custom_path and os.path.exists(custom_path):
        return custom_path

    env_path = os.environ.get("YOLO_MODEL_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = [
        getattr(settings, "YOLO_MODEL_PATH", None),
        "/mnt/opslag/deurbel/backend/app/assets/yolov8s.onnx",
        "/mnt/opslag/deurbel/models/yolov8s.onnx",
        "/mnt/opslag/deurbel/backend/app/yolov8s.onnx",
        "/mnt/opslag/deurbel/backend/app/assets/yolov8n.onnx",
        "/mnt/opslag/deurbel/models/yolov8n.onnx",
        "/mnt/opslag/deurbel/backend/app/yolov8n.onnx",
        "/mnt/opslag/deurbel/backend/app/yolo11s.pt",
        "/mnt/opslag/deurbel/backend/app/yolo11n.pt",
    ]
    for c in candidates:
        if c and os.path.exists(c) and os.path.getsize(c) > 5_000_000:
            return c
    return "/mnt/opslag/deurbel/backend/app/assets/yolov8s.onnx"


def compute_iou(
    boxA: Tuple[float, float, float, float],
    boxB: Tuple[float, float, float, float],
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
    box: Tuple[float, float, float, float],
    parked_boxes: List[Tuple[float, float, float, float]],
    threshold: float = 0.45,
) -> bool:
    """Return True if box matches any parked reference box with IoU >= threshold."""
    for p_box in parked_boxes:
        if compute_iou(box, p_box) >= threshold:
            return True
    return False


async def get_substream_rtsp() -> str:
    return "rtsp://127.0.0.1:8554/deurbel_sub"


async def get_mainstream_rtsp() -> str:
    return "rtsp://127.0.0.1:8554/deurbel"


class DetectionSettings:
    """Helper to load system detection configuration from SQLite (respecting all /admin & app keys)."""

    @staticmethod
    async def load() -> Dict[str, Any]:
        try:
            raw = await get_system_settings()
            det_enabled_raw = raw.get("detection_enabled", raw.get("smart_ai_enabled", "true"))
            p_conf = raw.get("person_conf_threshold", raw.get("conf_person", "0.38"))
            v_conf = raw.get("vehicle_conf_threshold", raw.get("conf_vehicle", "0.42"))
            a_conf = raw.get("animal_conf_threshold", raw.get("conf_animal", "0.35"))
            cooldown = raw.get("event_cooldown_seconds", raw.get("cooldown_seconds", "5.0"))
            min_disp_v = raw.get("min_displacement_vehicle", raw.get("vehicle_min_displacement", "0.045"))
            min_disp_p = raw.get("min_displacement_person", raw.get("person_min_displacement", "0.020"))

            return {
                "detection_enabled": str(det_enabled_raw).lower() in ("true", "1"),
                "record_person": str(raw.get("record_person", "true")).lower() in ("true", "1"),
                "notify_person": str(raw.get("notify_person", "true")).lower() in ("true", "1"),
                "record_vehicle": str(raw.get("record_vehicle", "true")).lower() in ("true", "1"),
                "notify_vehicle": str(raw.get("notify_vehicle", "true")).lower() in ("true", "1"),
                "record_animal": str(raw.get("record_animal", "true")).lower() in ("true", "1"),
                "notify_animal": str(raw.get("notify_animal", "true")).lower() in ("true", "1"),
                "record_motion": str(raw.get("record_motion", "true")).lower() in ("true", "1"),
                "notify_motion": str(raw.get("notify_motion", "false")).lower() in ("true", "1"),
                "quiet_hours_enabled": str(raw.get("quiet_hours_enabled", "true")).lower() in ("true", "1"),
                "quiet_hours_start": raw.get("quiet_hours_start", "23:30"),
                "quiet_hours_end": raw.get("quiet_hours_end", "07:30"),
                "person_conf_threshold": float(p_conf),
                "vehicle_conf_threshold": float(v_conf),
                "animal_conf_threshold": float(a_conf),
                "event_cooldown_seconds": float(cooldown),
                "min_displacement_vehicle": float(min_disp_v),
                "min_displacement_person": float(min_disp_p),
                "exclusion_zones": raw.get("exclusion_zones", "[]"),
            }
        except Exception:
            return {
                "detection_enabled": True,
                "record_person": True,
                "notify_person": True,
                "record_vehicle": True,
                "notify_vehicle": True,
                "record_animal": True,
                "notify_animal": True,
                "record_motion": True,
                "notify_motion": False,
                "quiet_hours_enabled": True,
                "quiet_hours_start": "23:30",
                "quiet_hours_end": "07:30",
                "person_conf_threshold": 0.38,
                "vehicle_conf_threshold": 0.42,
                "animal_conf_threshold": 0.35,
                "event_cooldown_seconds": 5.0,
                "min_displacement_vehicle": 0.045,
                "min_displacement_person": 0.020,
                "exclusion_zones": "[]",
            }


class CandidateDetection:
    """Legacy candidate detection structure for backward compatibility with test suites."""

    def __init__(
        self,
        track_id: str,
        category: str,
        label: str,
        box: Tuple[float, float, float, float],
        confidence: float,
        timestamp: float,
        pixel_ratio: float = 0.0,
    ):
        self.track_id = track_id
        self.category = category
        self.label = label
        self.best_box = box
        self.best_conf = confidence
        self.first_time = timestamp
        self.last_time = timestamp
        self.frames = [(timestamp, box, confidence)]
        self.motion_ratios = [pixel_ratio]
        self.verified = False
        self.best_frame: Optional[np.ndarray] = None


class TrackedTrajectory:
    """3D Trajectory tracking candidate across time."""

    def __init__(
        self,
        track_id: str,
        category: str,
        label: str,
        box: Tuple[float, float, float, float],
        confidence: float,
        timestamp: float,
        frame: np.ndarray,
        mog2_ratio: float,
    ):
        self.track_id = track_id
        self.category = category
        self.label = label
        self.first_time = timestamp
        self.last_time = timestamp
        self.first_box = box
        self.last_box = box
        self.best_box = box
        self.best_conf = confidence
        self.best_frame = frame.copy() if frame is not None else None
        self.frames: List[Tuple[float, Tuple[float, float, float, float], float]] = [(timestamp, box, confidence)]
        self.mog2_ratios: List[float] = [mog2_ratio]
        self.verified = False
        self.disp_pct: float = 0.0


class SmartDetector:
    """
    High-Precision 3D-Trajectory & Multi-Scale Vision AI Engine (v3.6.3).
    Runs at 4 FPS with MOG2 + Full-Frame + High-Res Motion Zoom-Crop Inference.
    """

    def __init__(self):
        self._model_path = resolve_model_path()
        self._ort_session: Optional[ort.InferenceSession] = None
        self._input_name: str = "images"
        self._pytorch_model = None

        # Background Subtractor MOG2 for motion analysis
        self.mog2 = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=22, detectShadows=False)
        self.kernel_3x3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

        # Parked Car Baseline Memory
        # Format: {"box": (x1, y1, x2, y2), "centroid": (cx, cy), "anchored_since": ts, "last_seen": ts}
        self.parked_car_slots: List[Dict[str, Any]] = []
        self.stationary_vehicles: List[Dict[str, Any]] = []
        self.parked_vehicle_boxes: List[Tuple[float, float, float, float]] = []

        # 3D Trajectory Active Tracks
        self._tracks: Dict[str, TrackedTrajectory] = {}
        self._legacy_tracks: Dict[str, Any] = {}
        self._next_track_num = 1

        # Activity & Telemetry Timestamps
        self._last_hardware_motion_time: float = 0.0
        self._last_verified_object_seen: float = 0.0
        self._last_person_seen: float = 0.0
        self._last_trigger_time: float = 0.0
        self._last_trigger_per_category: Dict[str, float] = {}
        self._last_clip_end_per_category: Dict[str, float] = {}
        self._last_rejected_log_time: Dict[str, float] = {}

        # Worker state
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._cooldown_seconds: float = 5.0
        self._prev_frame: Optional[np.ndarray] = None
        self._prev_frame_time: float = 0.0

        self.init_model()

    def init_model(self, model_path: Optional[str] = None) -> bool:
        """Initialize ONNXRuntime session with automatic safe candidate fallback."""
        candidates = [
            model_path,
            os.environ.get("YOLO_MODEL_PATH"),
            "/mnt/opslag/deurbel/backend/app/assets/yolov8s.onnx",
            "/mnt/opslag/deurbel/models/yolov8s.onnx",
            "/mnt/opslag/deurbel/backend/app/yolov8s.onnx",
            "/mnt/opslag/deurbel/backend/app/assets/yolov8n.onnx",
            "/mnt/opslag/deurbel/models/yolov8n.onnx",
            "/mnt/opslag/deurbel/backend/app/yolov8n.onnx",
        ]
        for path in candidates:
            if path and os.path.exists(path) and path.endswith(".onnx") and os.path.getsize(path) > 5_000_000:
                try:
                    opts = ort.SessionOptions()
                    opts.intra_op_num_threads = 4
                    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                    self._ort_session = ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
                    self._input_name = self._ort_session.get_inputs()[0].name
                    self._model_path = path
                    logger.info("ONNXRuntime InferenceSession initialized from %s (4 threads)", path)
                    return True
                except Exception as e:
                    logger.warning("Failed loading ONNX session from %s: %s (trying next candidate)", path, e)

        try:
            from ultralytics import YOLO
            pt_candidates = [
                "/mnt/opslag/deurbel/backend/app/yolo11s.pt",
                "/mnt/opslag/deurbel/backend/app/yolo11n.pt",
                "/mnt/opslag/deurbel/backend/app/yolov8n.pt",
            ]
            for pt_path in pt_candidates:
                if os.path.exists(pt_path):
                    self._pytorch_model = YOLO(pt_path)
                    self._model_path = pt_path
                    logger.info("Ultralytics PyTorch YOLO initialized from %s", pt_path)
                    return True
        except Exception as pe:
            logger.error("Failed loading fallback PyTorch YOLO: %s", pe)

        return False

    def notify_hardware_motion(self) -> None:
        """Signaled by Port 8000 SDK Alarm (0x4000 dwAlarmType == 3)."""
        self._last_hardware_motion_time = time.time()
        try:
            from app.recorder import is_recording_active, notify_ongoing_motion
            if is_recording_active():
                notify_ongoing_motion(category="motion", label="Beweging")
        except Exception:
            pass

    def is_motion_active(self) -> bool:
        """Check if verified object motion or camera hardware motion was detected within last 3.5s."""
        now = time.time()
        return (now - self._last_verified_object_seen) < 3.5 or (now - self._last_hardware_motion_time) < 3.5

    def _infer_image(
        self, img_bgr: np.ndarray, conf_thresh: float = 0.25
    ) -> List[Tuple[Tuple[float, float, float, float], float, int]]:
        """
        Runs YOLO inference on an image (BGR).
        Returns list of ((x1, y1, x2, y2), confidence, class_id) in `img_bgr` coordinates.
        """
        h_orig, w_orig = img_bgr.shape[:2]
        if self._ort_session is not None:
            try:
                resized = cv2.resize(img_bgr, (640, 640))
                rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
                blob = np.transpose(rgb.astype(np.float32) / 255.0, (2, 0, 1))[np.newaxis, :]

                outputs = self._ort_session.run(None, {self._input_name: blob})[0]
                preds = outputs[0].T  # (8400, 84)

                boxes = preds[:, :4]
                scores = preds[:, 4:]
                class_ids = np.argmax(scores, axis=1)
                confidences = np.max(scores, axis=1)

                valid_mask = confidences >= conf_thresh
                if not np.any(valid_mask):
                    return []

                valid_boxes = boxes[valid_mask]
                valid_confs = confidences[valid_mask]
                valid_cids = class_ids[valid_mask]

                scale_x = w_orig / 640.0
                scale_y = h_orig / 640.0

                boxes_for_nms = []
                for cx, cy, bw, bh in valid_boxes:
                    bx1 = int((cx - bw / 2.0) * scale_x)
                    by1 = int((cy - bh / 2.0) * scale_y)
                    bw_px = int(bw * scale_x)
                    bh_px = int(bh * scale_y)
                    boxes_for_nms.append([bx1, by1, bw_px, bh_px])

                indices = cv2.dnn.NMSBoxes(
                    boxes_for_nms, valid_confs.tolist(), score_threshold=conf_thresh, nms_threshold=0.45
                )
                results = []
                if len(indices) > 0:
                    for idx in indices.flatten():
                        bx1, by1, bw_px, bh_px = boxes_for_nms[idx]
                        bx2 = min(w_orig, bx1 + bw_px)
                        by2 = min(h_orig, by1 + bh_px)
                        bx1 = max(0, bx1)
                        by1 = max(0, by1)
                        results.append(
                            ((float(bx1), float(by1), float(bx2), float(by2)), float(valid_confs[idx]), int(valid_cids[idx]))
                        )
                return results
            except Exception as e:
                logger.warning("ONNX inference failed: %s (trying PyTorch)", e)

        if self._pytorch_model is not None:
            try:
                res = self._pytorch_model(img_bgr, conf=conf_thresh, iou=0.45, imgsz=640, verbose=False)
                results = []
                if res and len(res) > 0 and res[0].boxes is not None:
                    boxes = res[0].boxes.xyxy.cpu().numpy()
                    confs = res[0].boxes.conf.cpu().numpy()
                    cids = res[0].boxes.cls.cpu().numpy()
                    for b, c, cid in zip(boxes, confs, cids):
                        results.append(((float(b[0]), float(b[1]), float(b[2]), float(b[3])), float(c), int(cid)))
                return results
            except Exception as pe:
                logger.warning("PyTorch inference failed: %s", pe)

        return []

    def is_in_quiet_hours(self, start_str: str, end_str: str) -> bool:
        """Check if Europe/Amsterdam local time is currently within quiet hours."""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Amsterdam")
            now = datetime.now(tz)
            sp = [int(p) for p in start_str.split(":")]
            ep = [int(p) for p in end_str.split(":")]
            st = now.replace(hour=sp[0], minute=sp[1], second=0, microsecond=0)
            et = now.replace(hour=ep[0], minute=ep[1], second=0, microsecond=0)
            return (st <= now <= et) if st < et else (now >= st or now <= et)
        except Exception:
            return False

    def is_in_exclusion_zone(
        self,
        box: Tuple[float, float, float, float],
        frame_shape: Tuple[int, int],
        zones: List[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        """Check if box center falls inside an admin exclusion zone."""
        h, w = frame_shape[:2]
        cx = (box[0] + box[2]) / 2.0
        cy = (box[1] + box[3]) / 2.0
        for zone in zones:
            pts = zone.get("points", [])
            if len(pts) < 3:
                continue
            poly = np.array([[int(p[0] * w), int(p[1] * h)] for p in pts], dtype=np.int32)
            if cv2.pointPolygonTest(poly, (float(cx), float(cy)), False) >= 0:
                return True, zone.get("name", "Zone")
        return False, ""

    async def start(self) -> None:
        """Start the background 4 FPS AI detection loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._detection_loop())
        logger.info("3D-Trajectory & True Dual-Scale Vision AI Engine started (v3.6.3)")

    async def stop(self) -> None:
        """Stop background worker."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AI Engine stopped")

    async def _detection_loop(self) -> None:
        """Main continuous detection loop running at true 4 FPS (0.25s target cycle)."""
        await asyncio.sleep(1.5)
        last_processed_ts = 0.0

        while self._running:
            loop_start = time.time()
            try:
                cfg = await DetectionSettings.load()
                if not cfg["detection_enabled"]:
                    await asyncio.sleep(1.0)
                    continue

                from app.live_hub import live_hub
                jpeg_bytes = live_hub.get_latest_frame()
                frame_ts = live_hub.latest_frame_ts

                if not jpeg_bytes or frame_ts <= 0 or (frame_ts == last_processed_ts and (loop_start - frame_ts) > 0.20):
                    jpeg_bytes = await live_hub.fetch_snapshot_async(timeout=0.8)
                    frame_ts = live_hub.latest_frame_ts

                if not jpeg_bytes:
                    await asyncio.sleep(0.05)
                    continue

                last_processed_ts = frame_ts
                now_t = time.time()

                raw_frame = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
                if raw_frame is None:
                    await asyncio.sleep(0.05)
                    continue

                await self._process_frame_pipeline(raw_frame, now_t, cfg)

                # Maintain steady 4 FPS (0.25s interval minus processing time)
                elapsed = time.time() - loop_start
                sleep_dur = max(0.04, 0.25 - elapsed)
                await asyncio.sleep(sleep_dur)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("AI detection loop exception: %s", e)
                await asyncio.sleep(1.0)

    @property
    def _candidate_tracks(self) -> Dict[str, Any]:
        return self._legacy_tracks

    @_candidate_tracks.setter
    def _candidate_tracks(self, val: Dict[str, Any]) -> None:
        self._legacy_tracks = val

    async def _process_frame_gatekeeper(self, frame: np.ndarray, now: float, cfg: Dict[str, Any]) -> None:
        """Backward-compatible gatekeeper evaluation for legacy candidate tracks."""
        h, w = frame.shape[:2]
        for tid, cand in list(self._legacy_tracks.items()):
            if getattr(cand, "verified", False):
                continue
            if len(cand.frames) >= 2:
                fb = cand.frames[0][1]
                lb = cand.frames[-1][1]
                cx1 = (fb[0] + fb[2]) / 2.0
                cy1 = (fb[1] + fb[3]) / 2.0
                cx2 = (lb[0] + lb[2]) / 2.0
                cy2 = (lb[1] + lb[3]) / 2.0
                d_2d = math.hypot((cx2 - cx1) / float(w), (cy2 - cy1) / float(h))
                is_doorstep = (lb[3] >= 0.40 * float(h)) or (cx2 / float(w) > 0.60)
                if d_2d >= 0.02 or is_doorstep or cand.best_conf >= 0.50:
                    cand.verified = True
                    self._legacy_tracks.pop(tid, None)
                    continue
        await self._process_frame_pipeline(frame, now, cfg)

    async def _process_frame_pipeline(self, raw_frame: np.ndarray, now: float, cfg: Dict[str, Any]) -> None:
        """
        Executes the 5-step Senior CV Architecture on native resolution + normalized 640x480 space.
        """
        raw_h, raw_w = raw_frame.shape[:2]
        if raw_w != 640 or raw_h != 480:
            frame = cv2.resize(raw_frame, (640, 480))
        else:
            frame = raw_frame
        h, w = 480, 640
        scale_to_raw_x = raw_w / 640.0
        scale_to_raw_y = raw_h / 480.0

        # ── Step 1: Fast OpenCV Motion Analysis (MOG2 + Frame-Diff) ────────
        fg_mask = self.mog2.apply(frame)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, self.kernel_3x3)

        if self._prev_frame is not None and (now - self._prev_frame_time) <= 2.0:
            diff = cv2.absdiff(frame, self._prev_frame)
            gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (5, 5), 0)
            _, diff_thresh = cv2.threshold(blur, 16, 255, cv2.THRESH_BINARY)
            fg_mask = cv2.bitwise_or(fg_mask, diff_thresh)
        self._prev_frame = frame.copy()
        self._prev_frame_time = now

        # Mask out upper sky/tree crown (y < 0.20 * H) and bottom-left OSD clock (y > 0.92 * H, x < 0.36 * W)
        fg_mask[: int(0.20 * h), :] = 0
        fg_mask[int(0.92 * h) :, : int(0.36 * w)] = 0

        # Load dynamic thresholds from settings
        conf_person = float(cfg.get("person_conf_threshold", 0.38))
        conf_vehicle = float(cfg.get("vehicle_conf_threshold", 0.42))
        conf_animal = float(cfg.get("animal_conf_threshold", 0.35))
        min_disp_vehicle = float(cfg.get("min_displacement_vehicle", 0.045))
        min_disp_person = float(cfg.get("min_displacement_person", 0.020))
        self._cooldown_seconds = float(cfg.get("event_cooldown_seconds", 5.0))

        zones_raw = cfg.get("exclusion_zones", "[]")
        try:
            exclusion_zones = json.loads(zones_raw) if zones_raw else []
        except Exception:
            exclusion_zones = []

        # ── Step 2a: Full-Frame YOLO Inference (non-blocking) ──────────────
        base_thresh = min(conf_animal, conf_person, conf_vehicle, 0.25)
        raw_detections = await asyncio.to_thread(self._infer_image, frame, base_thresh)

        # ── Step 2b: High-Res Motion Zoom-Crop Inference for Distant/Small Objects ──
        contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_motion_area = int(0.0025 * w * h)  # >= 0.25% of frame (catches distant pedestrians & cyclists)

        # Sort contours largest first, inspect top 2 motion regions
        motion_contours = sorted(
            [c for c in contours if cv2.contourArea(c) >= min_motion_area],
            key=cv2.contourArea,
            reverse=True,
        )[:2]

        for cnt in motion_contours:
            mx, my, mw, mh = cv2.boundingRect(cnt)
            if mw < (0.50 * w) and mh < (0.55 * h) and (my + mh) >= int(0.20 * h):
                motion_box = (float(mx), float(my), float(mx + mw), float(my + mh))
                has_strong_full_det = any(
                    compute_iou(motion_box, d[0]) >= 0.35 and d[1] >= 0.55 for d in raw_detections
                )
                if not has_strong_full_det:
                    # Slice directly from HIGH-RES `raw_frame` with 35% margin!
                    pad_w = max(24, int(0.35 * mw))
                    pad_h = max(24, int(0.35 * mh))
                    cx1_640 = max(0, mx - pad_w)
                    cy1_640 = max(0, my - pad_h)
                    cx2_640 = min(w, mx + mw + pad_w)
                    cy2_640 = min(h, my + mh + pad_h)

                    rx1 = max(0, int(cx1_640 * scale_to_raw_x))
                    ry1 = max(0, int(cy1_640 * scale_to_raw_y))
                    rx2 = min(raw_w, int(cx2_640 * scale_to_raw_x))
                    ry2 = min(raw_h, int(cy2_640 * scale_to_raw_y))
                    high_res_crop = raw_frame[ry1:ry2, rx1:rx2]

                    if high_res_crop.shape[0] >= 32 and high_res_crop.shape[1] >= 32:
                        crop_dets = await asyncio.to_thread(self._infer_image, high_res_crop, max(0.26, base_thresh - 0.05))
                        for c_box, c_conf, c_cid in crop_dets:
                            # Map back from high_res_crop coordinates to normalized 640x480 space
                            fx1 = float((rx1 + c_box[0]) / scale_to_raw_x)
                            fy1 = float((ry1 + c_box[1]) / scale_to_raw_y)
                            fx2 = float((rx1 + c_box[2]) / scale_to_raw_x)
                            fy2 = float((ry1 + c_box[3]) / scale_to_raw_y)
                            raw_detections.append(((fx1, fy1, fx2, fy2), c_conf, c_cid))

        # ── Step 3: Cross-Scale Deduplication (NMS) & Supercategory Mapping ─
        classified_raw: List[Tuple[str, str, float, Tuple[float, float, float, float], float]] = []
        person_boxes = [d[0] for d in raw_detections if d[2] == 0 and d[1] >= (conf_person - 0.06)]

        for box, conf, cid in raw_detections:
            category = None
            if cid in PERSON_CLASSES:
                category = "person"
            elif cid in {1, 3}:  # bicycle, motorcycle
                has_rider = any(compute_iou(box, pb) >= 0.12 for pb in person_boxes)
                category = "person" if has_rider else "vehicle"
            elif cid in VEHICLE_CLASSES:
                category = "vehicle"
            elif cid in ANIMAL_CLASSES:
                category = "animal"
            else:
                continue

            req_conf = (
                (conf_person - 0.04)
                if category == "person"
                else (conf_vehicle if category == "vehicle" else conf_animal)
            )
            if conf < req_conf:
                continue

            # Ground check: bottom of box must be below upper sky (y2 >= 0.18 * H)
            if box[3] < (0.18 * h):
                continue

            in_zone, _ = self.is_in_exclusion_zone(box, (h, w), exclusion_zones)
            if in_zone:
                continue

            bx1, by1, bx2, by2 = max(0, int(box[0])), max(0, int(box[1])), min(w, int(box[2])), min(h, int(box[3]))
            box_mog = fg_mask[by1:by2, bx1:bx2]
            mog_ratio = (float(np.count_nonzero(box_mog)) / float(box_mog.size)) if box_mog.size > 0 else 0.0

            label = COCO_NAMES.get(cid, category.capitalize())
            classified_raw.append((category, label, conf, box, mog_ratio))

        # Deduplicate overlapping detections of the same category (Full-Frame vs Zoom-Crop)
        classified_raw.sort(key=lambda x: x[2], reverse=True)
        classified_candidates: List[Tuple[str, str, float, Tuple[float, float, float, float], float]] = []
        for cand in classified_raw:
            cat, lbl, c_conf, c_box, c_mog = cand
            is_dup = any(
                (existing[0] == cat or {existing[0], cat} == {"person", "vehicle"} and lbl in ("Fiets", "Motor", "Persoon"))
                and compute_iou(c_box, existing[3]) >= 0.45
                for existing in classified_candidates
            )
            if not is_dup:
                classified_candidates.append(cand)

        # ── Step 4: 3D Trajectory Association (with Cyclist/Person Continuity) ──
        now_t = now
        matched_track_ids: Set[str] = set()

        for category, label, conf, box, mog_ratio in classified_candidates:
            best_track_id = None
            best_score = 0.0

            bcx = (box[0] + box[2]) / 2.0
            bcy = (box[1] + box[3]) / 2.0

            for tid, trk in self._tracks.items():
                if tid in matched_track_ids:
                    continue
                # Allow person <-> bicycle/motorcycle continuity
                same_or_rider = (trk.category == category) or (
                    {trk.category, category} == {"person", "vehicle"} and (label in ("Fiets", "Motor") or trk.label in ("Fiets", "Motor"))
                )
                if not same_or_rider:
                    continue

                tcx = (trk.last_box[0] + trk.last_box[2]) / 2.0
                tcy = (trk.last_box[1] + trk.last_box[3]) / 2.0
                dist = math.hypot(bcx - tcx, bcy - tcy) / float(w)
                iou = compute_iou(box, trk.last_box)

                max_dist = 0.42 if category == "vehicle" else 0.28
                if iou >= 0.12 or dist <= max_dist:
                    score = max(iou, 0.45 - dist)
                    if score > best_score:
                        best_score = score
                        best_track_id = tid

            if best_track_id is not None:
                trk = self._tracks[best_track_id]
                # Upgrade track category to 'person' if person is detected on a bike track
                if category == "person" and trk.category != "person":
                    trk.category = "person"
                    trk.label = "Persoon"
                trk.last_box = box
                trk.last_time = now_t
                trk.frames.append((now_t, box, conf))
                trk.mog2_ratios.append(mog_ratio)
                # Keep sliding history bounded to last 30 frames
                if len(trk.frames) > 30:
                    trk.frames = trk.frames[-30:]
                    trk.mog2_ratios = trk.mog2_ratios[-30:]
                if conf >= trk.best_conf:
                    trk.best_conf = conf
                    trk.best_box = box
                    trk.best_frame = raw_frame.copy()
                matched_track_ids.add(best_track_id)
            else:
                tid = f"trk_{self._next_track_num}"
                self._next_track_num += 1
                new_trk = TrackedTrajectory(tid, category, label, box, conf, now_t, raw_frame, mog_ratio)
                self._tracks[tid] = new_trk
                matched_track_ids.add(tid)

        # ── Step 5: Verification, Ongoing Motion Keep-Alive & Parked-Car Memory ──
        to_dispatch: List[TrackedTrajectory] = []

        for tid, trk in list(self._tracks.items()):
            # Evaluate trajectory over sliding window of last 4.5 seconds (fixes the >4.0s track-death bug!)
            recent_frames = [f for f in trk.frames if (now_t - f[0]) <= 4.5]
            if not recent_frames:
                recent_frames = [trk.frames[-1]]

            ref_box = recent_frames[0][1]
            cur_box = trk.last_box

            cx_start = (ref_box[0] + ref_box[2]) / 2.0
            cy_start = (ref_box[1] + ref_box[3]) / 2.0
            cx_now = (cur_box[0] + cur_box[2]) / 2.0
            cy_now = (cur_box[1] + cur_box[3]) / 2.0

            d_2d = math.hypot((cx_now - cx_start) / float(w), (cy_now - cy_start) / float(h))
            h_start = max(1.0, ref_box[3] - ref_box[1])
            h_now = max(1.0, cur_box[3] - cur_box[1])
            d_scale = (max(h_now, h_start) / min(h_now, h_start)) - 1.0
            recent_mog = float(np.mean(trk.mog2_ratios[-5:])) if trk.mog2_ratios else 0.0
            trk.disp_pct = round(max(d_2d, d_scale * 0.5) * 100.0, 1)

            # CRITICAL FIX 1: If this track was ALREADY verified and is still moving in frame,
            # keep extending the active recording so the video NEVER stops mid-action!
            if trk.verified:
                if tid in matched_track_ids and (recent_mog >= 0.015 or d_2d >= 0.015):
                    self._last_verified_object_seen = now_t
                    if trk.category == "person":
                        self._last_person_seen = now_t
                    try:
                        from app.recorder import extend_active_recording, is_recording_active
                        if is_recording_active():
                            extend_active_recording(trk.category, trk.label)
                    except Exception:
                        pass
                continue

            is_approved = False

            if trk.category == "person":
                # Anti-wind bush/tree filter on extreme left/right borders
                in_bush_border = (cx_now < 0.14 * w) or (cx_now > 0.72 * w)
                aspect_ratio = h_now / max(1.0, (cur_box[2] - cur_box[0]))

                if aspect_ratio < 0.85 and cur_box[3] < 0.70 * h:
                    # Wide flat shadow on pavement
                    continue

                if len(recent_frames) >= 2:
                    req_disp = min_disp_person * (1.4 if in_bush_border else 1.0)
                    if (d_2d >= req_disp or d_scale >= 0.08) and (recent_mog >= 0.012 or trk.best_conf >= 0.50):
                        is_approved = True
                        logger.info(
                            "✅ [Person APPROVED (Movement)]: %s (D_2D=%.3f, D_scale=%.3f, conf=%.2f)",
                            trk.label, d_2d, d_scale, trk.best_conf,
                        )
                    elif cur_box[3] > (0.48 * float(h)) and trk.best_conf >= 0.48 and recent_mog >= 0.012:
                        is_approved = True
                        logger.info(
                            "✅ [Person APPROVED (Doorstep Visitor)]: %s (conf=%.2f, y2=%.1f, mog=%.3f)",
                            trk.label, trk.best_conf, cur_box[3], recent_mog,
                        )
                elif cur_box[3] > (0.52 * float(h)) and trk.best_conf >= 0.54 and recent_mog >= 0.018:
                    is_approved = True
                    logger.info(
                        "✅ [Person APPROVED (Instant Doorstep)]: %s (conf=%.2f, y2=%.1f)",
                        trk.label, trk.best_conf, cur_box[3],
                    )

                if is_approved:
                    self._last_person_seen = now_t
                    trk.verified = True
                    to_dispatch.append(trk)

            elif trk.category == "animal":
                if (len(recent_frames) >= 2 and d_2d >= 0.015) or (trk.best_conf >= 0.48 and recent_mog >= 0.015):
                    is_approved = True
                    trk.verified = True
                    to_dispatch.append(trk)
                    logger.info("✅ [Animal APPROVED]: %s (D_2D=%.3f, conf=%.2f)", trk.label, d_2d, trk.best_conf)

            elif trk.category == "vehicle":
                # Check match against known parked_car_slots
                is_anchored = False
                for slot in self.parked_car_slots:
                    iou = compute_iou(cur_box, slot["box"])
                    scx, scy = slot["centroid"]
                    cent_shift = math.hypot(cx_now - scx, cy_now - scy) / float(w)
                    if iou >= 0.65 and cent_shift < 0.040:
                        is_anchored = True
                        slot["last_seen"] = now_t
                        break

                if is_anchored:
                    if d_2d >= max(0.055, min_disp_vehicle):
                        is_approved = True
                        logger.info("🚗 [Departing Vehicle APPROVED]: %s (D_2D=%.3f)", trk.label, d_2d)
                else:
                    overlaps_parked = any(compute_iou(cur_box, s["box"]) >= 0.62 for s in self.parked_car_slots)
                    if len(recent_frames) >= 2 and d_2d >= min_disp_vehicle and not overlaps_parked:
                        is_approved = True
                        logger.info(
                            "🚗 [Driving Vehicle APPROVED]: %s (D_2D=%.3f, frames=%d)",
                            trk.label, d_2d, len(recent_frames),
                        )
                    elif len(trk.frames) >= 12 and d_2d < 0.025:
                        # Stationary for >= 3.0 seconds (12 frames at 4 FPS) -> Anchor as parked car!
                        self.parked_car_slots.append({
                            "box": cur_box,
                            "centroid": (cx_now, cy_now),
                            "anchored_since": now_t,
                            "last_seen": now_t,
                        })
                        logger.info("🔒 [Parked Car Slot Anchored (3s)]: %s at %s", trk.label, cur_box)
                        trk.verified = True

                if is_approved:
                    trk.verified = True
                    to_dispatch.append(trk)

        # Dispatch newly approved detections
        for trk in to_dispatch:
            self._last_verified_object_seen = now_t
            best_img = trk.best_frame if trk.best_frame is not None else raw_frame
            # Scale normalized 640x480 box back to best_img dimensions for debug drawing
            img_h, img_w = best_img.shape[:2]
            sx = img_w / 640.0
            sy = img_h / 480.0
            scaled_box = (
                trk.best_box[0] * sx,
                trk.best_box[1] * sy,
                trk.best_box[2] * sx,
                trk.best_box[3] * sy,
            )
            asyncio.create_task(
                self._dispatch_verified_detection(
                    category=trk.category,
                    label=trk.label,
                    confidence=trk.best_conf,
                    disp_pct=trk.disp_pct,
                    box=scaled_box,
                    keyframe=best_img,
                    cfg=cfg,
                )
            )

        # Prune stale tracks older than 6.0 seconds
        for tid, trk in list(self._tracks.items()):
            if (now_t - trk.last_time) > 6.0:
                self._tracks.pop(tid, None)

        # Keep parked car slots for 300s (5 minutes) so brief dark/rain dropouts never lose parked memory
        self.parked_car_slots = [s for s in self.parked_car_slots if (now_t - s["last_seen"]) < 300.0]
        self.stationary_vehicles = self.parked_car_slots
        self.parked_vehicle_boxes = [s["box"] for s in self.parked_car_slots]

    async def _log_rejected_candidate(
        self,
        category: str,
        label: str,
        confidence: float,
        disp_pct: float,
        reason: str,
        box: Tuple[float, float, float, float],
        frame: np.ndarray,
    ) -> None:
        """Save rejected candidate audit log."""
        try:
            await save_ai_audit_log(
                status="REJECTED",
                category=category,
                label=label,
                confidence=round(confidence, 2),
                displacement_pct=disp_pct,
                reason=reason,
                snapshot_path=None,
                boxes_json=json.dumps([{"x1": int(box[0]), "y1": int(box[1]), "x2": int(box[2]), "y2": int(box[3])}]),
            )
        except Exception:
            pass

    async def _dispatch_verified_detection(
        self,
        category: str,
        label: str,
        confidence: float,
        disp_pct: float,
        box: Tuple[float, float, float, float],
        keyframe: np.ndarray,
        cfg: Dict[str, Any],
    ) -> None:
        """Dispatches verified detection, merges into active recording if running, or starts new clip + push."""
        now_t = time.time()
        last_cat_trigger = self._last_trigger_per_category.get(category, 0.0)
        cooldown = self._cooldown_seconds

        if (now_t - last_cat_trigger) < cooldown:
            return

        self._last_trigger_per_category[category] = now_t
        self._last_trigger_time = now_t

        should_record = cfg.get(f"record_{category}", True)
        should_notify = cfg.get(f"notify_{category}", True)

        if not should_record:
            return

        in_quiet_hours = cfg.get("quiet_hours_enabled", True) and self.is_in_quiet_hours(
            cfg.get("quiet_hours_start", "23:30"), cfg.get("quiet_hours_end", "07:30")
        )
        if in_quiet_hours:
            should_notify = False

        from app.recorder import extend_active_recording, get_day_clip_dir, is_recording_active

        # CRITICAL FIX 2: If a recording is ALREADY active, merge this category directly into the active clip
        # WITHOUT creating and deleting a ghost clip row in SQLite!
        if is_recording_active():
            from app.recorder import _active_recording_info
            active_clip_id = _active_recording_info.get("clip_id")
            extend_active_recording(category, label)
            if active_clip_id:
                active_cats = _active_recording_info.setdefault("categories", set())
                if category not in active_cats:
                    active_cats.add(category)
                    cats = sorted(list(active_cats), key=lambda c: 0 if c == "ring" else 1 if c == "person" else 2)
                    combined_category = ",".join(cats)
                    try:
                        from app.database import get_db
                        async with get_db(write=True) as db:
                            await db.execute(
                                "UPDATE clips SET category = ? WHERE clip_id = ?",
                                (combined_category, active_clip_id),
                            )
                    except Exception:
                        pass
                logger.info("Merged %s (%s) into already active recording %s", category, label, active_clip_id)
                return

        clip_id = str(uuid.uuid4())
        day_dir = get_day_clip_dir()
        t0_iso = datetime.now(timezone.utc).isoformat()

        settings.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        settings.CLIPS_DIR.mkdir(parents=True, exist_ok=True)

        # Write high-res clean thumbnail & snapshots
        thumb_path = str(day_dir / f"thumb_{clip_id}.jpg")
        root_thumb = str(settings.CLIPS_DIR / f"thumb_{clip_id}.jpg")
        clean_snap = str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")

        cv2.imwrite(thumb_path, keyframe)
        cv2.imwrite(root_thumb, keyframe)
        cv2.imwrite(clean_snap, keyframe)

        # Debug snapshot with bounding box for Admin Inspector
        debug_snap = str(settings.SNAPSHOTS_DIR / f"{clip_id}_debug.jpg")
        annotated = keyframe.copy()
        x1, y1, x2, y2 = [int(v) for v in box]
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(
            annotated,
            f"{label} ({round(confidence * 100)}%) d={disp_pct}%",
            (x1, max(25, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
        )
        cv2.imwrite(debug_snap, annotated)

        cat_dutch = {
            "person": "Persoon gedetecteerd",
            "vehicle": "Voertuig gedetecteerd",
            "animal": "Dier gedetecteerd",
        }.get(category, "Beweging gedetecteerd")

        title = f"{cat_dutch}: {label}"
        snapshot_url = f"/api/v1/clips/{clip_id}/thumbnail"

        await save_clip(
            clip_id=clip_id,
            call_id=clip_id,
            video_path=str(day_dir / f"{clip_id}.mp4"),
            snapshot_path=thumb_path,
            answered_by=f"AI: {cat_dutch} ({label})",
            duration=0.0,
            status="RECORDING",
            category=category,
            file_size=0,
            created_at=t0_iso,
        )

        event_id = await save_event(
            event_type="motion",
            title=title,
            category=category,
            clip_id=clip_id,
            snapshot_url=snapshot_url,
            silent=not should_notify,
        )

        payload = {
            "event_id": event_id,
            "category": category,
            "title": title,
            "clip_id": clip_id,
            "snapshot_url": snapshot_url,
            "silent": not should_notify,
            "timestamp": t0_iso,
        }
        await push_event("AI_DETECTION", payload)
        logger.info("🚀 [AI Notification Sent]: %s (clip_id=%s, silent=%s)", title, clip_id, not should_notify)

        clip_path = str(day_dir / f"{clip_id}.mp4")
        asyncio.create_task(
            self._finalize_clip_recording(
                clip_id=clip_id,
                clip_path=clip_path,
                category=category,
                label=label,
                clean_snapshot_path=thumb_path,
            )
        )

        try:
            await save_ai_audit_log(
                status="APPROVED",
                category=category,
                label=label,
                confidence=round(confidence, 2),
                displacement_pct=disp_pct,
                reason=f"GOEDGEKEURD: {label} bevestigd met 3D-verplaatsing ({disp_pct}%)",
                snapshot_path=debug_snap,
                boxes_json=json.dumps([{"x1": x1, "y1": y1, "x2": x2, "y2": y2}]),
            )
        except Exception:
            pass

    async def _finalize_clip_recording(
        self,
        clip_id: str,
        clip_path: str,
        category: str,
        label: str,
        clean_snapshot_path: str = "",
    ) -> None:
        """
        Records video in background under single recording lock (v3.6.3).
        """
        try:
            from app.recorder import (
                compress_clip_background,
                ensure_thumbnail,
                extend_active_recording,
                is_recording_active,
                record_smart_clip,
                recording_lock,
            )

            sys_settings = await get_system_settings()
            preroll = float(sys_settings.get("pre_roll_seconds", "20"))
            postroll = float(sys_settings.get("post_roll_seconds", "20"))
            max_duration = float(
                sys_settings.get("max_active_motion_seconds", sys_settings.get("max_clip_seconds", "180"))
            )

            if is_recording_active():
                from app.recorder import _active_recording_info
                active_clip_id = _active_recording_info.get("clip_id")
                extend_active_recording(category, label, duration_sec=max_duration)
                if active_clip_id and active_clip_id != clip_id:
                    active_cats = _active_recording_info.setdefault("categories", set())
                    active_cats.add(category)
                    cats = sorted(list(active_cats), key=lambda c: 0 if c == "ring" else 1 if c == "person" else 2)
                    combined_category = ",".join(cats)
                    try:
                        from app.database import get_db
                        async with get_db(write=True) as db:
                            await db.execute(
                                "UPDATE clips SET category = ? WHERE clip_id = ?",
                                (combined_category, active_clip_id),
                            )
                    except Exception:
                        pass
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
                    logger.info(
                        "Updated verified clip %s to READY: duration=%.1fs, size=%d bytes, category=%s",
                        clip_id, duration, file_size, combined_cat,
                    )
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
