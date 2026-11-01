import pytest
import os
import time
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_settings_twoway_sync():
    from app.models import get_setting, set_setting, get_system_settings, update_system_settings, set_system_setting

    # 1. Update via set_setting (e.g. from app detection endpoint)
    await set_setting("smart_ai_enabled", "false")
    val_app = await get_setting("smart_ai_enabled")
    val_det = await get_setting("detection_enabled")
    assert val_app == "false"
    assert val_det == "false"

    sys_settings = await get_system_settings()
    assert sys_settings.get("smart_ai_enabled") == "false"
    assert sys_settings.get("detection_enabled") == "false"

    # Reset
    await set_setting("smart_ai_enabled", "true")
    assert await get_setting("smart_ai_enabled") == "true"
    assert await get_setting("detection_enabled") == "true"

    # 2. Update via update_system_settings (e.g. from /admin)
    await update_system_settings({
        "record_person": "true",
        "notify_person": "true",
        "retention_days": "14",
        "pre_roll_seconds": "20",
        "post_roll_seconds": "20"
    })
    assert await get_setting("record_person") == "true"
    assert await get_setting("notify_person") == "true"
    assert await get_setting("retention_days") == "14"
    assert await get_setting("pre_roll_seconds") == "20"

    # 3. Test alias threshold syncing
    await set_setting("person_conf_threshold", "0.55")
    assert await get_setting("conf_person") == "0.55"
    assert await get_setting("person_conf_threshold") == "0.55"


@pytest.mark.asyncio
async def test_recorder_motion_notification():
    from app.recorder import notify_ongoing_motion, _active_recording_info, recording_lock

    # When no recording is active, notify_ongoing_motion returns False safely
    assert notify_ongoing_motion("vehicle", "Auto") is False

    # Simulate active recording
    _active_recording_info["clip_id"] = "test_clip_123"
    _active_recording_info["last_motion_timestamp"] = time.time() - 10.0

    notify_ongoing_motion("person", "Persoon")
    # Verify timestamp was updated to near-current
    assert (time.time() - _active_recording_info["last_motion_timestamp"]) < 1.0
    assert _active_recording_info.get("category") == "person"

    _active_recording_info["clip_id"] = None


@pytest.mark.asyncio
async def test_admin_endpoints_structure():
    from app.admin import get_shazam_detections

    res = await get_shazam_detections()
    assert "status" in res
    assert "detections" in res
    assert isinstance(res["detections"], list)

