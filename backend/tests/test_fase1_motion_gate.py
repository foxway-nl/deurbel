"""
Unit tests for FASE 1: Motion-Gated Inference (app.motion_gate).
"""

import numpy as np
import pytest
from app.motion_gate import MotionGater, MotionGateResult


def test_motion_gate_default_disabled():
    """Verify that MotionGater is disabled by default and passes frames through."""
    gater = MotionGater(enabled=False)
    assert not gater.enabled

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    res = gater.process_frame(frame)
    assert res.is_gate_open is True
    assert "DISABLED" in res.reason


def test_motion_gate_static_scene_closes_gate():
    """Verify that a static scene causes the gate to close when enabled."""
    gater = MotionGater(enabled=True, min_area_ratio=0.003, cooldown_sec=0.1)

    # Feed identical static frames to establish background
    frame = np.full((480, 640, 3), 128, dtype=np.uint8)
    for _ in range(15):
        res = gater.process_frame(frame, now=100.0)

    # After background settles, gate should close
    res = gater.process_frame(frame, now=105.0)
    assert res.is_gate_open is False
    assert res.has_active_motion is False
    assert len(res.rois) == 0


def test_motion_gate_moving_object_opens_gate():
    """Verify that a genuine moving object opens the gate and generates ROIs."""
    gater = MotionGater(enabled=True, min_area_ratio=0.001, cooldown_sec=1.0)

    # Establish static background
    bg = np.full((480, 640, 3), 50, dtype=np.uint8)
    for _ in range(15):
        gater.process_frame(bg, now=100.0)

    # Introduce a moving bright rectangle (car or person moving)
    frame_with_motion = bg.copy()
    frame_with_motion[150:250, 200:350] = 220  # 100x150 = 15,000 pixels

    res = gater.process_frame(frame_with_motion, now=101.0)
    assert res.is_gate_open is True
    assert res.has_active_motion is True
    assert len(res.rois) >= 1
    roi = res.rois[0]
    # Check that ROI encompasses the motion region with padding
    assert roi.x1 <= 200 and roi.x2 >= 350
    assert roi.y1 <= 150 and roi.y2 >= 250


def test_motion_gate_parked_car_suppression():
    """Verify that motion entirely inside a stationary parked car box is suppressed."""
    gater = MotionGater(enabled=True, min_area_ratio=0.001, cooldown_sec=0.1)

    bg = np.full((480, 640, 3), 60, dtype=np.uint8)
    for _ in range(15):
        gater.process_frame(bg, now=100.0)

    # Parked car box: x1=100, y1=100, x2=300, y2=300
    parked_box = (100.0, 100.0, 300.0, 300.0)

    # Motion strictly inside the parked car (e.g. shadow / glint: 120..180, 120..200)
    frame_glint = bg.copy()
    frame_glint[120:180, 120:200] = 200

    res = gater.process_frame(frame_glint, now=105.0, stationary_boxes=[parked_box])
    # The motion was inside the parked car and should be suppressed
    assert res.is_gate_open is False
    assert len(res.rois) == 0


def test_motion_gate_global_shift_warning():
    """Verify that >65% global frame shift flags a camera shift warning."""
    gater = MotionGater(enabled=True, global_motion_threshold=0.65)

    bg = np.full((480, 640, 3), 40, dtype=np.uint8)
    for _ in range(15):
        gater.process_frame(bg, now=100.0)

    # Whole frame completely changes color (e.g. IR cut filter or camera bump)
    shifted = np.full((480, 640, 3), 220, dtype=np.uint8)
    res = gater.process_frame(shifted, now=101.0)
    assert res.camera_shift_warning is True
    assert res.is_gate_open is False
    assert "GLOBAL_CAMERA_SHIFT" in res.reason


def test_motion_gate_cooldown_hold():
    """Verify that gate remains open for cooldown_sec after motion ends."""
    gater = MotionGater(enabled=True, min_area_ratio=0.001, cooldown_sec=1.5)

    bg = np.full((480, 640, 3), 50, dtype=np.uint8)
    for _ in range(15):
        gater.process_frame(bg, now=100.0)

    # Motion at t=101.0
    motion_frame = bg.copy()
    motion_frame[100:200, 100:200] = 240
    gater.process_frame(motion_frame, now=101.0)

    # Scene static again at t=101.8 (cooldown 1.5s active)
    res_cd = gater.process_frame(bg, now=101.8)
    assert res_cd.is_gate_open is True
    assert "COOLDOWN_ACTIVE" in res_cd.reason

    # Scene static at t=103.0 (cooldown expired)
    res_done = gater.process_frame(bg, now=103.0)
    assert res_done.is_gate_open is False
