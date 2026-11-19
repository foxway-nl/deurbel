"""
Foxway Smart Doorbell — Smart AI Compatibility & Recording Finalizer (ai_detector.py).
Replaces obsolete heavy YOLO frame-loop with lightweight integration shim for door_service.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel

from app.config import settings
from app.models import (
    delete_clip as db_delete_clip,
    get_system_settings,
    save_clip,
    save_event,
    update_clip,
)

logger = logging.getLogger("foxway.ai_detector")


class DetectionSettings(BaseModel):
    enabled: bool = True
    conf_threshold: float = 0.35
    record_person: bool = True
    record_vehicle: bool = True
    record_animal: bool = True


class CandidateDetection:
    """Compatibility shim for candidate detection."""
    def __init__(
        self,
        box: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
        confidence: float = 0.5,
        class_id: int = 0,
        label: str = "person",
        category: str = "person",
    ):
        self.box = box
        self.confidence = confidence
        self.class_id = class_id
        self.label = label
        self.category = category


def resolve_model_path(custom_path: Optional[str] = None) -> str:
    """Resolves active YOLO model path for backwards compatibility."""
    if custom_path and os.path.exists(custom_path):
        return custom_path
    candidates = [
        "/mnt/opslag/deurbel/backend/app/assets/yolo11n.onnx",
        "/mnt/opslag/deurbel/backend/app/assets/yolov8n.onnx",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "/mnt/opslag/deurbel/backend/app/assets/yolo11n.onnx"


def compute_iou(
    boxA: Tuple[float, float, float, float],
    boxB: Tuple[float, float, float, float],
) -> float:
    """Compute Intersection over Union (IoU) of two bounding boxes (x1, y1, x2, y2)."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    interArea = max(0.0, xB - xA) * max(0.0, yB - yA)
    boxAArea = max(0.0, boxA[2] - boxA[0]) * max(0.0, boxA[3] - boxA[1])
    boxBArea = max(0.0, boxB[2] - boxB[0]) * max(0.0, boxB[3] - boxB[1])
    unionArea = boxAArea + boxBArea - interArea

    if unionArea <= 0.0:
        return 0.0
    return float(interArea / unionArea)


class SmartDetector:
    """
    Lightweight compatibility shim for AI detection subsystem.
    Delegates active motion tracking to door_service and manages clip finalization.
    """

    def __init__(self) -> None:
        self.stationary_vehicles: List[Dict[str, Any]] = []
        self._last_clip_end_per_category: Dict[str, float] = {}
        self._is_running = False

    def is_motion_active(self) -> bool:
        """Returns True if door_service has reported recent motion."""
        try:
            from app.door_service import door_service
            return door_service.is_motion_active()
        except Exception:
            return False

    def notify_hardware_motion(self) -> None:
        """Safe no-op for hardware motion notification."""
        pass

    async def start(self) -> None:
        """No-op startup (supervised by door_service in main.py)."""
        self._is_running = True
        logger.info("SmartDetector shim active (object detection handled by door_service/door_watch.py)")

    async def stop(self) -> None:
        """No-op shutdown."""
        self._is_running = False

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
        Links pre-roll/post-roll segments from RAM buffer into permanent storage.
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


async def get_substream_rtsp() -> str:
    """Resolve RTSP substream URL (defaults to go2rtc deurbel_sub)."""
    return os.environ.get("RTSP_SUB_URL", "rtsp://127.0.0.1:8554/deurbel_sub")


smart_detector = SmartDetector()
