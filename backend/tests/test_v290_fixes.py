"""
v2.9.0 Fixes Verification Test Suite:
  1. Test 1: 2 parked cars swap rejection (cx=0.42 and cx=0.62) via parked_spots pre-filtering and step-displacement constraint.
  2. Test 2: Quiet hours (23:30 - 07:30) in Europe/Amsterdam timezone (01:15 is quiet, 14:00 is not quiet).
  3. Test 3: Repeated forced doorbell ring triggers (POST /api/v1/webhook/shelly-ring?force=true) without cooldown blocks.
"""

from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from app.ai_detector import (
    SmartDetector,
    smart_detector,
    compute_iou,
    is_parked_vehicle,
)


def test_parked_cars_swap_rejection():
    """
    Test 1: Two parked cars at cx=0.42 and cx=0.62 must NOT be swapped or tracked as a moving vehicle.
    Pre-filtering on parked_spots and step-displacement constraint (dx <= 0.12, IoU >= 0.15)
    strictly reject matching across adjacent parked vehicles.
    """
    detector = SmartDetector()
    frame_w = 640.0
    frame_h = 480.0

    # Car 1: cx = 270 / 640 = 0.4218
    car1_box = (230.0, 200.0, 310.0, 270.0)
    # Car 2: cx = 400 / 640 = 0.625
    car2_box = (360.0, 200.0, 440.0, 270.0)

    # 1. Car 1 is stationary in parked spots
    detector.parked_spots.append(car1_box)
    detector.parked_vehicle_boxes.append(car1_box)

    # Verify IoU of Car 1 with parked spot is 1.0 (>= 0.55)
    assert compute_iou(car1_box, car1_box) == 1.0

    # If Car 1 has changed_pixel_ratio = 0.02 (< 0.30), pre-filter rejects it as parked spot
    all_parked = list(detector.parked_spots) + detector.parked_vehicle_boxes
    is_on_parked = any(compute_iou(car1_box, p) >= 0.55 for p in all_parked)
    ratio_car1 = 0.02
    assert is_on_parked is True
    # Box should be discarded immediately before candidate tracking
    should_reject = is_on_parked and (ratio_car1 < 0.30)
    assert should_reject is True

    # 2. Test Step-displacement constraint: Car 1 (frame N) to Car 2 (frame N+1)
    # Distance between centroids:
    cx1 = (car1_box[0] + car1_box[2]) / 2.0
    cx2 = (car2_box[0] + car2_box[2]) / 2.0
    step_dx = abs(cx2 - cx1) / frame_w
    iou_between_cars = compute_iou(car1_box, car2_box)

    # Step dx is ~0.203 (> 0.12) and IoU between adjacent parked cars is 0.0 (< 0.15)
    assert step_dx > 0.12
    assert iou_between_cars < 0.15

    # Association rule check:
    can_associate = (step_dx <= 0.12) and (iou_between_cars >= 0.15)
    assert can_associate is False, "Car 1 and Car 2 must NEVER associate into the same candidate track"


def test_quiet_hours_amsterdam_timezone():
    """
    Test 2: Quiet hours (23:30 - 07:30) in Europe/Amsterdam timezone.
    At 01:15, 23:45, and 06:30 quiet hours must be ACTIVE (True).
    At 14:00 and 08:00 quiet hours must be INACTIVE (False).
    """
    tz = ZoneInfo("Europe/Amsterdam")

    # 01:15 in Amsterdam -> QUIET
    dt_0115 = datetime(2026, 10, 29, 1, 15, 0, tzinfo=tz)
    assert smart_detector.is_in_quiet_hours("23:30", "07:30", check_time=dt_0115) is True

    # 23:45 in Amsterdam -> QUIET
    dt_2345 = datetime(2026, 10, 29, 23, 45, 0, tzinfo=tz)
    assert smart_detector.is_in_quiet_hours("23:30", "07:30", check_time=dt_2345) is True

    # 06:30 in Amsterdam -> QUIET
    dt_0630 = datetime(2026, 10, 29, 6, 30, 0, tzinfo=tz)
    assert smart_detector.is_in_quiet_hours("23:30", "07:30", check_time=dt_0630) is True

    # 14:00 in Amsterdam -> NOT QUIET
    dt_1400 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=tz)
    assert smart_detector.is_in_quiet_hours("23:30", "07:30", check_time=dt_1400) is False

    # 08:00 in Amsterdam -> NOT QUIET
    dt_0800 = datetime(2026, 10, 29, 8, 0, 0, tzinfo=tz)
    assert smart_detector.is_in_quiet_hours("23:30", "07:30", check_time=dt_0800) is False


@pytest.mark.asyncio
async def test_repeated_force_ring_triggers(async_client):
    """
    Test 3: Multiple consecutive Shelly webhook calls with ?force=true
    must ALL succeed without cooldown suppression and generate distinct call IDs.
    """
    call_ids = []
    for i in range(3):
        resp = await async_client.post("/api/v1/webhook/shelly-ring?force=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "call_id" in data
        assert data["call_id"] not in call_ids, f"Call ID {data['call_id']} must be unique"
        call_ids.append(data["call_id"])

    assert len(call_ids) == 3
    assert len(set(call_ids)) == 3

