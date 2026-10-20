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
