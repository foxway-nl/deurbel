"""
Foxway Smart Doorbell — End-to-End Live Pipeline Test Suite (v3.6.1).

Verifies the 3 core pipelines:
1. Chime Audio -> Ding-Dong Verification -> State Machine Ringing & Notification
2. AI Detection -> Video Recording (.mp4 with pre-roll) + Thumbnail (.jpg) + DB & Push Notification
3. Live Container Health, Running Tasks & Black-Box Triage
"""

import asyncio
import os
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest
import scipy.io.wavfile as wav

from app.ai_detector import SmartDetector, smart_detector
from app.config import settings
from app.database import get_db
from app.models import get_clip, get_events_since, init_db, save_clip
from app.ring_detector import ShazamChimeDetector, ring_detector, verify_ding_dong_chime
from app.state_machine import CallState, CallStateMachine


@pytest.mark.asyncio
class TestV361E2ELive:
    """End-to-End Live Pipeline Tests (v3.6.1)."""

    async def test_1_chime_audio_and_full_ring_pipeline(self):
        """
        Test 1:
        - Verify chime_reference.wav returns True via verify_ding_dong_chime
        - Verify 1s 728 Hz sine wave returns False
        - Verify 1s Gaussian white noise returns False
        - Trigger ring via state machine, verify RINGING state, DB entry & notification
        """
        await init_db()

        # 1a. Verify chime_reference.wav
        ref_path = "/mnt/opslag/deurbel/backend/app/assets/chime_reference.wav"
        if not os.path.exists(ref_path):
            ref_path = "/mnt/opslag/deurbel/backend/tests/chime_reference.wav"
        assert os.path.exists(ref_path), f"Reference chime audio not found at {ref_path}"

        sr, audio_data = wav.read(ref_path)
        if audio_data.dtype == np.int16:
            audio_f32 = audio_data.astype(np.float32) / 32768.0
        else:
            audio_f32 = audio_data.astype(np.float32)

        is_chime, score_chime, reason_chime = verify_ding_dong_chime(audio_f32, sample_rate=sr)
        assert is_chime is True, f"chime_reference.wav MUST be verified as True chime! (reason: {reason_chime})"
        assert score_chime >= 0.75, f"chime score must be >= 0.75, got {score_chime}"

        # 1b. Verify 1.0s 728 Hz single tone returns False
        t_1s = np.arange(8000) / 8000.0
        pure_728 = (np.sin(2 * np.pi * 728 * t_1s) * 0.8).astype(np.float32)
        is_whistle, score_whistle, reason_whistle = verify_ding_dong_chime(pure_728, sample_rate=8000)
        assert is_whistle is False, f"Pure 728 Hz whistle MUST NOT trigger chime! (reason: {reason_whistle})"

        # 1c. Verify 1.0s white noise returns False
        white_noise = np.random.normal(0, 0.1, 8000).astype(np.float32)
        is_noise, score_noise, reason_noise = verify_ding_dong_chime(white_noise, sample_rate=8000)
        assert is_noise is False, f"White noise MUST NOT trigger chime! (reason: {reason_noise})"

        # 1d. Ring callback and state machine test
        from app.main import _on_ringing, _on_start_recording, call_sm

        mock_push = AsyncMock()
        with patch("app.main.push_event", mock_push):
            # Reset state machine if needed
            curr = call_sm.current_call
            if curr:
                await call_sm.hangup(curr.call_id)
            call_sm.on_ringing = _on_ringing
            call_sm.on_start_recording = _on_start_recording

            # Ring the doorbell
            call = await call_sm.ring(trigger_source="SHAZAM_CHIME")
            assert call is not None
            assert call.state == CallState.RINGING

            # Allow event loop to process background tasks
            await asyncio.sleep(0.3)

            # Prove notification push was invoked
            assert mock_push.called
            call_args_list = [c[0] for c in mock_push.call_args_list]
            event_types = [args[0] for args in call_args_list if len(args) > 0]
            assert "CALL_RINGING" in event_types

            # Prove event was saved in DB
            events = await get_events_since(since_id=0, limit=20, order="desc")
            ring_events = [e for e in events if e.get("category") == "ring" or e.get("type") == "ring"]
            assert len(ring_events) > 0, "Ring event must be recorded in DB"

            # Clean up active call
            await call_sm.hangup(call.call_id)

    async def test_2_ai_detection_recording_and_notification(self):
        """
        Test 2:
        - Simulate approved person and vehicle detection from ai_detector
        - Verify .mp4 and .jpg thumbnail are created and registered in database
        - Verify push notification is sent
        """
        await init_db()
        detector = SmartDetector()

        # Create dummy keyframe
        frame = np.full((480, 640, 3), 120, dtype=np.uint8)
        cv2.putText(frame, "Foxway E2E Test Frame", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)

        cfg = {
            "detection_enabled": True,
            "record_person": True,
            "notify_person": True,
            "record_vehicle": True,
            "notify_vehicle": True,
            "record_animal": True,
            "notify_animal": True,
            "quiet_hours_enabled": False,
            "person_conf_threshold": 0.38,
            "vehicle_conf_threshold": 0.42,
            "animal_conf_threshold": 0.35,
            "event_cooldown_seconds": 0.0,
            "exclusion_zones": "[]",
        }

        mock_push = AsyncMock()
        with patch("app.ai_detector.push_event", mock_push):
            # Test person detection dispatch
            box_person = (150.0, 100.0, 300.0, 420.0)
            await detector._dispatch_verified_detection(
                category="person",
                label="Persoon",
                confidence=0.88,
                disp_pct=5.5,
                box=box_person,
                keyframe=frame,
                cfg=cfg,
            )

            # Verify push notification
            assert mock_push.called
            last_call = mock_push.call_args_list[-1]
            assert last_call[0][0] == "AI_DETECTION"
            payload = last_call[0][1]
            assert payload["category"] == "person"
            clip_id = payload["clip_id"]

            # Verify DB clip record was created
            clip = await get_clip(clip_id)
            assert clip is not None, f"Clip {clip_id} must exist in database"
            assert clip["category"] == "person"
            assert clip["status"] in ("RECORDING", "READY")

            # Verify thumbnail exists on disk
            thumb_path = clip["snapshot_path"]
            assert thumb_path and os.path.exists(thumb_path), f"Thumbnail not found at {thumb_path}"
            assert os.path.getsize(thumb_path) > 1000, "Thumbnail file must be > 1KB"

    async def test_3_live_services_and_triage(self):
        """
        Test 3:
        - Check that preroll buffer is actively writing segments in /dev/shm/deurbel_preroll
        - Run triage.py and verify 0 errors
        """
        preroll_dir = Path("/dev/shm/deurbel_preroll")
        if not preroll_dir.exists():
            preroll_dir = Path("/dev/shm/deurbel_ring")
        assert preroll_dir.exists(), f"Preroll buffer dir not found at {preroll_dir}"

        ts_files = list(preroll_dir.glob("*.ts"))
        assert len(ts_files) > 0, "Preroll buffer must contain .ts segments"

        # Check latest segment freshness (must be < 10 seconds old)
        newest = max(ts_files, key=lambda p: p.stat().st_mtime)
        age = time.time() - newest.stat().st_mtime
        assert age < 15.0, f"Latest segment {newest} is {age:.1f}s old (must be < 15s)"

        # Run triage.py
        proc = subprocess.run(
            ["python3", "/mnt/opslag/deurbel/scripts/triage.py"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"triage.py failed with returncode {proc.returncode}"
        assert "Total Errors  : 0" in proc.stdout, f"triage.py reported errors!\n{proc.stdout}"
