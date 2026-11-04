"""
Foxway Smart Doorbell — Consistent Object Tracker & Motion Classifier (v3.5.5 - FASE 2).

Features:
1. ByteTrack Association:
   - Uses supervision.ByteTrack for multi-object tracking across frames.
   - Assigns persistent integer track IDs (tracker_id).
2. Temporal Consistency & Anti-Glitch:
   - A track is INITIALIZING until observed in >= min_track_frames (default 3 frames).
   - Spurious single-frame false positives are filtered out automatically.
3. Motion Classification per Track:
   - "DRIVING": Track moved >= driving_displacement_px (default 25.0px) over last N frames.
   - "PARKED": Track moved < driving_displacement_px over last N frames (stationary).
   - "WALKING": Pedestrian displacement >= min_person_disp.
   - "ANIMAL": Animal presence verified over consistent frames.
4. Configurable Parameters & Feature Flag:
   - 'enabled': Default False (feature flag).
   - 'min_track_frames': Minimum consecutive frames to confirm track (default 3).
   - 'driving_displacement_px': Minimum pixel displacement for driving state (default 25.0px).
   - 'window_n_frames': Rolling window of frames for motion calculation (default 5).
"""

from __future__ import annotations

import collections
import logging
import math
import time
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import supervision as sv

logger = logging.getLogger("foxway.object_tracker")


def compute_box_iou(box1: Tuple[float, float, float, float], box2: Tuple[float, float, float, float]) -> float:
    """Compute Intersection over Union (IoU) between two boxes."""
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    a1 = max(0.0, box1[2] - box1[0]) * max(0.0, box1[3] - box1[1])
    a2 = max(0.0, box2[2] - box2[0]) * max(0.0, box2[3] - box2[1])
    union = a1 + a2 - inter
    return (inter / union) if union > 0.0 else 0.0


class TrackState(str, Enum):
    INITIALIZING = "INITIALIZING"
    PARKED = "PARKED"
    DRIVING = "DRIVING"
    WALKING = "WALKING"
    ANIMAL = "ANIMAL"
    UNKNOWN = "UNKNOWN"


class TrackedObject:
    """Represents a single persistent object track across frames."""

    def __init__(
        self,
        tracker_id: int,
        category: str,
        label: str,
        initial_box: Tuple[float, float, float, float],
        initial_conf: float,
        timestamp: float,
        frame: Optional[np.ndarray] = None,
        max_history: int = 30,
    ):
        self.tracker_id = tracker_id
        self.category = category
        self.label = label
        self.first_seen = timestamp
        self.last_seen = timestamp
        self.state = TrackState.INITIALIZING
        self.is_confirmed = False
        self.has_been_dispatched = False

        self.best_conf = initial_conf
        self.best_box = initial_box
        self.best_frame: Optional[np.ndarray] = frame.copy() if frame is not None else None

        # Rolling history of (timestamp, box, conf, centroid)
        cx = (initial_box[0] + initial_box[2]) / 2.0
        cy = (initial_box[1] + initial_box[3]) / 2.0
        self.history: Deque[Tuple[float, Tuple[float, float, float, float], float, Tuple[float, float]]] = collections.deque(
            maxlen=max_history
        )
        self.history.append((timestamp, initial_box, initial_conf, (cx, cy)))

        self.total_displacement_px: float = 0.0
        self.window_displacement_px: float = 0.0
        self.speed_px_per_sec: float = 0.0
        self.is_anchored: bool = False
        self.anchor_box: Optional[Tuple[float, float, float, float]] = None
        self.anchor_centroid: Optional[Tuple[float, float]] = None

    def update(
        self,
        box: Tuple[float, float, float, float],
        conf: float,
        timestamp: float,
        frame: Optional[np.ndarray] = None,
        window_n: int = 5,
        driving_threshold_px: float = 25.0,
        min_track_frames: int = 3,
        frame_width: float = 640.0,
    ) -> None:
        """Update track with a new observation frame."""
        self.last_seen = timestamp
        cx = (box[0] + box[2]) / 2.0
        cy = (box[1] + box[3]) / 2.0
        self.history.append((timestamp, box, conf, (cx, cy)))

        if conf > self.best_conf:
            self.best_conf = conf
            self.best_box = box
            if frame is not None:
                self.best_frame = frame.copy()

        # Check track confirmation (>= min_track_frames)
        if len(self.history) >= min_track_frames:
            self.is_confirmed = True

        # Calculate total displacement from origin
        first_c = self.history[0][3]
        last_c = (cx, cy)
        self.total_displacement_px = math.hypot(last_c[0] - first_c[0], last_c[1] - first_c[1])

        # Calculate window displacement over last N frames
        n = min(len(self.history), window_n)
        window_start_c = self.history[-n][3]
        window_start_t = self.history[-n][0]
        self.window_displacement_px = math.hypot(last_c[0] - window_start_c[0], last_c[1] - window_start_c[1])

        dt = timestamp - window_start_t
        self.speed_px_per_sec = (self.window_displacement_px / dt) if dt > 0.05 else 0.0

        # Classify state
        if self.is_anchored:
            self.is_confirmed = True

        if not self.is_confirmed and not self.is_anchored:
            self.state = TrackState.INITIALIZING
        elif self.category == "vehicle":
            # Persistent Parking Anchor Memory (Arlo / Reolink standard)
            if not self.is_anchored:
                # If vehicle was already parked, or observed for >= 6 frames with small movement:
                if self.state == TrackState.PARKED or (len(self.history) >= 6 and self.total_displacement_px < driving_threshold_px * 1.5):
                    self.is_anchored = True
                    self.anchor_box = box
                    self.anchor_centroid = (cx, cy)
                    self.state = TrackState.PARKED

            if self.is_anchored and self.anchor_centroid is not None:
                anchor_disp = math.hypot(cx - self.anchor_centroid[0], cy - self.anchor_centroid[1])
                # An anchored car cannot become DRIVING from minor shadow/lighting shifts (< 70px)
                # It requires true physical displacement >= 70px (or >= 11% frame width) and speed >= 45px/s
                min_departure_disp = max(70.0, 0.11 * frame_width)
                if anchor_disp >= min_departure_disp and self.speed_px_per_sec >= 45.0:
                    self.state = TrackState.DRIVING
                else:
                    self.state = TrackState.PARKED
            else:
                # Freshly arriving or passing vehicle:
                min_moving_disp = max(40.0, 0.06 * frame_width)
                if self.window_displacement_px >= min_moving_disp and self.speed_px_per_sec >= 40.0:
                    self.state = TrackState.DRIVING
                elif len(self.history) >= min_track_frames and self.total_displacement_px >= (driving_threshold_px * 2.0):
                    self.state = TrackState.DRIVING
                else:
                    self.state = TrackState.PARKED
        elif self.category == "person":
            min_person_px = 0.010 * frame_width  # 1.0% width = ~6.4px
            if self.total_displacement_px >= min_person_px or self.best_conf >= 0.50:
                self.state = TrackState.WALKING
            else:
                self.state = TrackState.INITIALIZING
        elif self.category == "animal":
            self.state = TrackState.ANIMAL


class ConsistentObjectTracker:
    """
    Supervision ByteTrack wrapper with temporal consistency & motion classification.
    """

    def __init__(
        self,
        enabled: bool = False,
        min_track_frames: int = 3,
        driving_displacement_px: float = 25.0,
        window_n_frames: int = 5,
        lost_track_buffer: int = 30,
        track_thresh: float = 0.20,
    ):
        self.enabled = enabled
        self.min_track_frames = min_track_frames
        self.driving_displacement_px = driving_displacement_px
        self.window_n_frames = window_n_frames
        self.lost_track_buffer = lost_track_buffer
        self.track_thresh = track_thresh

        self._byte_tracker = sv.ByteTrack(
            lost_track_buffer=self.lost_track_buffer,
            minimum_matching_threshold=0.8,
            track_activation_threshold=self.track_thresh,
        )
        self.tracks: Dict[int, TrackedObject] = {}

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        logger.info("ConsistentObjectTracker enabled set to: %s", self.enabled)

    def reset(self) -> None:
        """Reset internal tracker state."""
        self._byte_tracker = sv.ByteTrack(
            lost_track_buffer=self.lost_track_buffer,
            minimum_matching_threshold=0.8,
            track_activation_threshold=self.track_thresh,
        )
        self.tracks.clear()
        logger.info("ConsistentObjectTracker reset")

    def update_config(
        self,
        min_track_frames: Optional[int] = None,
        driving_displacement_px: Optional[float] = None,
        window_n_frames: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        if enabled is not None:
            self.enabled = enabled
        if min_track_frames is not None:
            self.min_track_frames = max(1, min(10, min_track_frames))
        if driving_displacement_px is not None:
            self.driving_displacement_px = max(5.0, min(100.0, driving_displacement_px))
        if window_n_frames is not None:
            self.window_n_frames = max(2, min(20, window_n_frames))

    def update(
        self,
        boxes: np.ndarray,
        confs: np.ndarray,
        classes: np.ndarray,
        frame: np.ndarray,
        now: Optional[float] = None,
        coco_names: Optional[Dict[int, str]] = None,
        known_anchors: Optional[List[Tuple[float, float, float, float]]] = None,
    ) -> List[TrackedObject]:
        """
        Updates ByteTrack with current frame's raw detections.

        Returns:
            List of currently active TrackedObject instances.
        """
        now_ts = now if now is not None else time.time()
        coco_names = coco_names or {}
        frame_width = float(frame.shape[1]) if frame is not None else 640.0

        if not self.enabled:
            return []

        if len(boxes) == 0:
            # Empty frame - tick ByteTrack with empty detections
            empty_dets = sv.Detections.empty()
            self._byte_tracker.update_with_detections(empty_dets)
            self._prune_stale_tracks(now_ts)
            return list(self.tracks.values())

        # Construct supervision Detections object
        dets = sv.Detections(
            xyxy=boxes.astype(np.float32),
            confidence=confs.astype(np.float32),
            class_id=classes.astype(int),
        )

        tracked_dets = self._byte_tracker.update_with_detections(dets)

        # Update or create TrackedObject per tracked item
        if tracked_dets.tracker_id is not None:
            for i, tid in enumerate(tracked_dets.tracker_id):
                t_box = (
                    float(tracked_dets.xyxy[i][0]),
                    float(tracked_dets.xyxy[i][1]),
                    float(tracked_dets.xyxy[i][2]),
                    float(tracked_dets.xyxy[i][3]),
                )
                t_conf = float(tracked_dets.confidence[i]) if tracked_dets.confidence is not None else 0.50
                t_cls = int(tracked_dets.class_id[i]) if tracked_dets.class_id is not None else 0

                # Determine category
                if t_cls == 0:
                    cat = "person"
                elif t_cls in (1, 2, 3, 5, 7):
                    cat = "vehicle"
                elif t_cls in (14, 15, 16, 17, 18, 19):
                    cat = "animal"
                else:
                    cat = "unknown"

                label = coco_names.get(t_cls, cat.capitalize())

                if tid not in self.tracks:
                    new_obj = TrackedObject(
                        tracker_id=tid,
                        category=cat,
                        label=label,
                        initial_box=t_box,
                        initial_conf=t_conf,
                        timestamp=now_ts,
                        frame=frame,
                    )
                    # Check if this new vehicle overlaps with a known parked anchor
                    if cat == "vehicle" and known_anchors:
                        for anchor in known_anchors:
                            if compute_box_iou(t_box, anchor) >= 0.35:
                                new_obj.is_anchored = True
                                new_obj.anchor_box = t_box
                                new_obj.anchor_centroid = ((t_box[0] + t_box[2]) / 2.0, (t_box[1] + t_box[3]) / 2.0)
                                new_obj.state = TrackState.PARKED
                                logger.debug("Vehicle track %d initialized directly as ANCHORED (IoU>=0.35 with known parking spot)", tid)
                                break
                    self.tracks[tid] = new_obj
                else:
                    self.tracks[tid].update(
                        box=t_box,
                        conf=t_conf,
                        timestamp=now_ts,
                        frame=frame,
                        window_n=self.window_n_frames,
                        driving_threshold_px=self.driving_displacement_px,
                        min_track_frames=self.min_track_frames,
                        frame_width=frame_width,
                    )

        self._prune_stale_tracks(now_ts)
        return list(self.tracks.values())

    def _prune_stale_tracks(self, now: float) -> None:
        """Prunes tracks that haven't been seen for > 4.0 seconds."""
        stale_ids = [tid for tid, obj in self.tracks.items() if (now - obj.last_seen) > 4.0]
        for tid in stale_ids:
            self.tracks.pop(tid, None)


# Singleton instance
consistent_tracker = ConsistentObjectTracker(enabled=False)

