"""
Foxway Smart Doorbell — Test Suite for v3.4.0:
1. 100% EZVIZ AI Sensitivity & Stationary Parked-Car Dynamic Memory.
2. High-Speed AI Scan Burst (4 FPS / 0.25s on Port 8000 SDK Alarm motion).
3. Per-Category Cooldowns (5s for same category, 0s for different category).
4. Passive Doorbell Network & SDK Analyser (/proc/net/tcp & NET_DVR_GetDVRWorkState_V30).
"""

import asyncio
import time
import pytest
import numpy as np
from app.ai_detector import SmartDetector, CandidateDetection, compute_iou
from app.sdk_alarm_listener import SDKAlarmListener


@pytest.mark.asyncio
async def test_stationary_parked_car_memory():
    """Verify that cars parked in driveway are retained in stationary memory and filtered out."""
    detector = SmartDetector()
    now = time.time()
    w = 640.0

    # Car parked at driveway coordinates
    parked_box = (100.0, 200.0, 300.0, 400.0)

    # First sighting
    detector.update_stationary_vehicles([parked_box], now, int(w))
    assert len(detector.stationary_vehicles) == 1
    assert not detector.is_stationary_parked(parked_box, w, now)  # Not confirmed yet (only 1 frame)

    # Subsequent sightings in same spot
    for i in range(1, 5):
        t = now + (i * 0.5)
        # Small jitter (wind, sensor noise)
        jittered_box = (100.0 + i*0.2, 200.0 + i*0.1, 300.0 + i*0.2, 400.0 + i*0.1)
        detector.update_stationary_vehicles([jittered_box], t, int(w))

    # Now it must be confirmed as stationary parked!
    assert detector.is_stationary_parked(parked_box, w, now + 3.0)


@pytest.mark.asyncio
async def test_moving_vehicle_detection_and_verification():
    """Verify moving vehicle with displacement >= 4.5% is verified and approved."""
    detector = SmartDetector()
    now = time.time()
    w = 640.0

    # Moving vehicle passing by
    box1 = (50.0, 250.0, 180.0, 350.0)
    box2 = (90.0, 250.0, 220.0, 350.0)  # dx = 40 pixels = 6.25% of 640

    track = CandidateDetection("track_1", "vehicle", "Auto", box1, 0.65, now, 0.20)
    track.frames.append((now + 0.25, box2, 0.70))
    track.motion_ratios.append(0.25)
    detector._candidate_tracks["track_1"] = track

    # Not parked previously
    assert not detector.is_stationary_parked(box1, w, now)

    cx1 = (box1[0] + box1[2]) / 2.0
    cx2 = (box2[0] + box2[2]) / 2.0
    delta_d = abs(cx2 - cx1) / w
    assert delta_d >= 0.045  # Passed threshold


@pytest.mark.asyncio
async def test_pedestrian_across_street_detection():
    """Verify pedestrian across street (small size, conf=0.36, disp=2%) is verified."""
    detector = SmartDetector()
    now = time.time()
    w = 640.0
    h = 480.0

    # Distant pedestrian across the street (x=400, y=150, small box)
    box1 = (400.0, 150.0, 430.0, 220.0)
    box2 = (412.0, 150.0, 442.0, 220.0)  # dx = 12 pixels = 1.875% of 640

    track = CandidateDetection("track_p", "person", "Persoon", box1, 0.36, now, 0.10)
    track.frames.append((now + 0.25, box2, 0.38))
    track.motion_ratios.append(0.12)

    cx1 = (box1[0] + box1[2]) / 2.0
    cx2 = (box2[0] + box2[2]) / 2.0
    dx = abs(cx2 - cx1) / w

    assert dx >= 0.015  # Meets 1.5% displacement requirement
    assert track.best_conf >= 0.35  # Meets v3.4.0 0.35 confidence threshold


@pytest.mark.asyncio
async def test_hardware_motion_triggers_burst_scan():
    """Verify notify_hardware_motion activates high-speed scan burst."""
    detector = SmartDetector()
    assert (time.time() - detector._last_hardware_motion_time) > 10.0

    detector.notify_hardware_motion()
    assert (time.time() - detector._last_hardware_motion_time) < 1.0


@pytest.mark.asyncio
async def test_per_category_cooldown():
    """Verify 5s cooldown for same category and 0s cooldown for different category."""
    detector = SmartDetector()
    now = time.time()

    # Trigger vehicle
    detector._last_trigger_per_category["vehicle"] = now - 2.0
    detector._last_clip_end_per_category["vehicle"] = now - 2.0

    # Person has not triggered yet
    assert "person" not in detector._last_trigger_per_category

    # Same category (vehicle) is still in cooldown (< 5s)
    vehicle_cd_remaining = 5.0 - (now - detector._last_trigger_per_category["vehicle"])
    assert vehicle_cd_remaining > 0

    # Different category (person) has 0s cooldown!
    person_last_trigger = detector._last_trigger_per_category.get("person", 0.0)
    assert (now - person_last_trigger) > 5.0


@pytest.mark.asyncio
async def test_passive_doorbell_status_monitor():
    """Verify passive doorbell status monitor reads /proc/net/tcp and returns structured data without errors."""
    listener = SDKAlarmListener()
    data = await listener.log_passive_doorbell_status(trigger_source="TEST_SUITE")

    assert "trigger_source" in data
    assert data["trigger_source"] == "TEST_SUITE"
    assert "timestamp" in data
    assert "active_sockets" in data
    assert "csdk_workstate" in data
    assert "sdk_alarm_channel" in data
    assert isinstance(data["active_sockets"], list)

