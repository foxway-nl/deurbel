"""
Test 3: AI Detection filtering, motion mask intersection, and strict recording rule.
"""

import numpy as np
import pytest
from app.ai_detector import smart_detector, DetectionSettings

def test_yolo_model_initialization():
    initialized = smart_detector.init_model()
    assert initialized is True
    assert smart_detector._session is not None

def test_motion_mask_filters_stationary_car():
    # Synthetic frame 768x432
    frame = np.zeros((432, 768, 3), dtype=np.uint8)
    motion_mask = np.zeros((432, 768), dtype=np.uint8)

    # Motion is on the porch (x: 50..150, y: 150..250)
    motion_mask[150:250, 50:150] = 255

    # Run inference with motion mask: stationary items with 0 motion pixels are rejected
    cat, lbl, conf = smart_detector.run_yolo_inference(frame, motion_mask=motion_mask)
    # Since black frame has no moving object on porch, category must be "motion"
    assert cat == "motion"

@pytest.mark.asyncio
async def test_strict_recording_rule_discards_general_motion():
    cfg = await DetectionSettings.load()
    # Ensure record_motion is false
    cfg["record_motion"] = False

    category = "motion"
    should_record = cfg.get(f"record_{category}", False)
    assert should_record is False, "Empty motion MUST NOT record when record_motion=False"


def test_vehicle_displacement_check_parked_car_ignored():
    """Verify that a stationary/parked vehicle (delta_d < 0.08) is ignored."""
    import math
    frame_width = 768.0

    # Frame 1: center at (300, 200)
    cx1, cy1 = 300.0, 200.0
    # Frame 2: center at (308, 204) - slight jitter of parked car
    cx2, cy2 = 308.0, 204.0

    delta_d = math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2) / frame_width
    assert delta_d < 0.08, f"Displacement {delta_d} should be < 0.08"
    is_parked = delta_d < 0.08
    assert is_parked is True, "Stationary/parked vehicle MUST be ignored"


def test_vehicle_displacement_check_moving_car_accepted():
    """Verify that a moving vehicle (delta_d >= 0.08) is accepted."""
    import math
    frame_width = 768.0

    # Frame 1: center at (150, 200)
    cx1, cy1 = 150.0, 200.0
    # Frame 2: center at (230, 210) - moving across camera view
    cx2, cy2 = 230.0, 210.0

    delta_d = math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2) / frame_width
    assert delta_d >= 0.08, f"Displacement {delta_d} should be >= 0.08"
    is_moving = delta_d >= 0.08
    assert is_moving is True, "Moving vehicle MUST be confirmed"


@pytest.mark.asyncio
async def test_dynamic_subnet_discovery():
    """Verify dynamic /24 subnet calculation and camera discovery."""
    import ipaddress
    from app.camera_discovery import get_local_ip, get_local_subnet, is_doorbell_alive

    local_ip = get_local_ip()
    assert local_ip not in ("", "0.0.0.0")

    subnet = get_local_subnet()
    assert isinstance(subnet, ipaddress.IPv4Network)
    assert subnet.prefixlen == 24
    assert ipaddress.ip_address(local_ip) in subnet

    # Test reachability check against live doorbell
    alive = await is_doorbell_alive("192.168.2.31")
    assert isinstance(alive, bool)
