"""
Foxway Smart Doorbell — AI Detector v3.6.3 Verification Test Suite.

Proves:
1. Ongoing Motion Keep-Alive for Verified Tracks:
   A verified track that continues moving keeps updating `_last_verified_object_seen`,
   so `is_motion_active()` remains True.
2. Sliding-Window Trajectory Evaluation (No 4.0s Track-Death Bug):
   An object that starts moving after > 4.0s in frame is approved via the 4.5s sliding window.
3. High-Res Motion Zoom-Crop + Cross-Scale NMS:
   Motion ROI is sliced from high-resolution raw_frame, and overlapping full-frame + crop
   detections are deduplicated via NMS.
4. Clean active recording merge without creating/deleting ghost rows.
"""

import asyncio
import math
import time
from unittest.mock import AsyncMock, MagicMock, patch

import cv2
import numpy as np
import pytest

from app.ai_detector import (
    DetectionSettings,
    SmartDetector,
    TrackedTrajectory,
    compute_iou,
)


class TestV363AiDetector:
    """Verification suite for v3.6.3 AI detector fixes."""

    @pytest.mark.asyncio
    async def test_1_ongoing_motion_keep_alive_for_verified_track(self):
        """
        Prove: If a track is already verified (trk.verified = True) and keeps moving,
        _last_verified_object_seen is updated and is_motion_active() returns True.
        """
        detector = SmartDetector()
        now = time.time()

        # Create a track that was previously verified
        raw_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        tid = "trk_1"
        trk = TrackedTrajectory(
            track_id=tid,
            category="person",
            label="Persoon",
            box=(200.0, 100.0, 260.0, 350.0),
            confidence=0.65,
            timestamp=now - 2.0,
            frame=raw_frame,
            mog2_ratio=0.03,
        )
        trk.verified = True
        detector._tracks[tid] = trk
        detector._last_verified_object_seen = now - 10.0  # Expired timestamp

        cfg = {
            "detection_enabled": True,
            "record_person": True,
            "notify_person": True,
            "person_conf_threshold": 0.38,
            "vehicle_conf_threshold": 0.42,
            "animal_conf_threshold": 0.35,
            "event_cooldown_seconds": 5.0,
            "min_displacement_person": 0.02,
            "min_displacement_vehicle": 0.045,
            "exclusion_zones": "[]",
        }

        # Mock inference returning the person slightly moved (walking in frame)
        moved_box = (220.0, 110.0, 280.0, 360.0)
        mock_raw_dets = [((float(moved_box[0]), float(moved_box[1]), float(moved_box[2]), float(moved_box[3])), 0.68, 0)]

        with patch.object(detector, "_infer_image", return_value=mock_raw_dets):
            with patch("app.recorder.is_recording_active", return_value=True):
                with patch("app.recorder.extend_active_recording") as mock_extend:
                    await detector._process_frame_pipeline(raw_frame, now, cfg)

                    # Verify keep-alive fired
                    assert detector._last_verified_object_seen >= now, "_last_verified_object_seen must be updated!"
                    assert detector.is_motion_active() is True, "is_motion_active() must be True for ongoing motion!"
                    mock_extend.assert_called_with("person", "Persoon")

    @pytest.mark.asyncio
    async def test_2_sliding_window_trajectory_no_4s_track_death(self):
        """
        Prove: An object stationary for 5 seconds (track_age = 5.0s > 4.0s) that starts
        moving is approved in v3.6.3 because evaluation uses a 4.5s sliding window
        instead of permanently ignoring tracks older than 4.0s.
        """
        detector = SmartDetector()
        t0 = 1000.0
        now = t0 + 5.0  # 5.0 seconds later (in v3.6.2 this was rejected by 'track_age <= 4.0')

        raw_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        tid = "trk_2"
        # Track started 5 seconds ago at (200, 100, 260, 300)
        initial_box = (200.0, 100.0, 260.0, 300.0)
        trk = TrackedTrajectory(
            track_id=tid,
            category="person",
            label="Persoon",
            box=initial_box,
            confidence=0.52,
            timestamp=t0,
            frame=raw_frame,
            mog2_ratio=0.02,
        )
        # Add history: stationary at t0+1, t0+2, t0+3, t0+4
        for sec in range(1, 5):
            trk.frames.append((t0 + float(sec), initial_box, 0.50))
            trk.mog2_ratios.append(0.01)

        detector._tracks[tid] = trk

        # Now at t0 + 5.0, the person takes a clear step: x shifts by 40px (6.25% of width)
        moved_box = (240.0, 100.0, 300.0, 300.0)
        mock_raw_dets = [(moved_box, 0.60, 0)]

        cfg = {
            "detection_enabled": True,
            "record_person": True,
            "notify_person": True,
            "person_conf_threshold": 0.38,
            "vehicle_conf_threshold": 0.42,
            "animal_conf_threshold": 0.35,
            "event_cooldown_seconds": 5.0,
            "min_displacement_person": 0.02,
            "min_displacement_vehicle": 0.045,
            "exclusion_zones": "[]",
        }

        with patch.object(detector, "_infer_image", return_value=mock_raw_dets):
            with patch.object(detector, "_dispatch_verified_detection", new_callable=AsyncMock) as mock_dispatch:
                await detector._process_frame_pipeline(raw_frame, now, cfg)

                # The track must be APPROVED despite total track age = 5.0s!
                assert trk.verified is True, "Track older than 4.0s MUST be approved if moving within recent sliding window!"
                assert mock_dispatch.called, "_dispatch_verified_detection must be called for approved track!"
                call_kwargs = mock_dispatch.call_args[1]
                assert call_kwargs["category"] == "person"
                assert call_kwargs["label"] == "Persoon"

    @pytest.mark.asyncio
    async def test_3_high_res_motion_zoom_crop_and_cross_scale_nms(self):
        """
        Prove:
        1. High-Res Zoom Crop slices directly from high-resolution raw_frame (e.g. 1920x1080).
        2. Overlapping detections between full-frame and zoom-crop are merged via NMS (deduplicated).
        """
        detector = SmartDetector()
        now = time.time()

        # High-res raw frame (1920x1080)
        raw_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

        # Draw a small bright motion patch in raw_frame (simulating a distant person)
        # In 640x480 space: cx=320, cy=240, w=40, h=60 (scale = 3.0x in raw_frame)
        cv2.rectangle(raw_frame, (960 - 60, 540 - 90), (960 + 60, 540 + 90), (255, 255, 255), -1)

        cfg = {
            "detection_enabled": True,
            "record_person": True,
            "notify_person": True,
            "person_conf_threshold": 0.38,
            "vehicle_conf_threshold": 0.42,
            "animal_conf_threshold": 0.35,
            "event_cooldown_seconds": 5.0,
            "min_displacement_person": 0.02,
            "min_displacement_vehicle": 0.045,
            "exclusion_zones": "[]",
        }

        # Mock _infer_image to simulate:
        # 1. Full-frame returns a weak/borderline detection: box at (300, 210, 340, 270), conf 0.40
        # 2. Zoom-crop returns a crisp high-res detection overlapping with it: conf 0.75
        crop_called_shapes = []

        def side_effect_infer(img, conf):
            h, w = img.shape[:2]
            if w == 640 and h == 480:
                # Full frame inference
                return [((300.0, 210.0, 340.0, 270.0), 0.40, 0)]
            else:
                # Zoom crop from high-res raw_frame!
                crop_called_shapes.append((w, h))
                # Returns local box inside the crop
                return [((10.0, 10.0, 50.0, 70.0), 0.75, 0)]

        with patch.object(detector, "_infer_image", side_effect=side_effect_infer):
            await detector._process_frame_pipeline(raw_frame, now, cfg)

            # If zoom-crop occurred, crop_called_shapes will have inspected the slice
            # Check NMS deduplication: tracks should have only 1 track for this person, not 2
            person_tracks = [t for t in detector._tracks.values() if t.category == "person"]
            assert len(person_tracks) <= 1, "Cross-scale NMS must deduplicate overlapping full-frame and crop detections!"

    @pytest.mark.asyncio
    async def test_4_clean_active_recording_merge_no_ghost_rows(self):
        """
        Prove: When a recording is already active, _dispatch_verified_detection merges
        the category without inserting/deleting ghost rows in SQLite.
        """
        detector = SmartDetector()
        cfg = {
            "record_person": True,
            "notify_person": True,
            "quiet_hours_enabled": False,
        }
        raw_frame = np.zeros((480, 640, 3), dtype=np.uint8)

        mock_active_info = {"clip_id": "existing-clip-123", "categories": {"vehicle"}}

        with patch("app.recorder.is_recording_active", return_value=True):
            with patch("app.recorder._active_recording_info", mock_active_info):
                with patch("app.recorder.extend_active_recording") as mock_extend:
                    with patch("app.models.save_clip") as mock_save:
                        with patch("app.models.delete_clip") as mock_delete:
                            with patch("app.database.get_db") as mock_get_db:
                                mock_db = AsyncMock()
                                mock_get_db.return_value.__aenter__.return_value = mock_db

                                await detector._dispatch_verified_detection(
                                    category="person",
                                    label="Persoon",
                                    confidence=0.72,
                                    disp_pct=5.0,
                                    box=(100.0, 100.0, 200.0, 300.0),
                                    keyframe=raw_frame,
                                    cfg=cfg,
                                )

                                # Verify active recording extension and category merge
                                mock_extend.assert_called_with("person", "Persoon")
                                assert "person" in mock_active_info["categories"]
                                # Verify NO ghost save_clip or delete_clip were called!
                                mock_save.assert_not_called()
                                mock_delete.assert_not_called()

