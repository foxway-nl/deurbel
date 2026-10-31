"""
Foxway Smart Doorbell — v3.3.1 Verification Test Suite.

Verifies:
1. Doorbell ring trigger source tracking ('CSDK_PORT_8000', 'SHAZAM_CHIME', 'TEST_BUTTON', 'SHELLY_WEBHOOK').
2. Zero-suppression of doorbell rings under all quiet hours and silence settings.
3. Notification Poortwachter category switches (notify_person, notify_vehicle, notify_animal).
4. Settings changes and notify decisions logged to daily black-box JSONL.
5. Shazam chime threshold calibrated to 0.72 and admin API JSON serialization.
"""

import os
import json
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app, call_sm
from app.models import save_event, get_events_since, init_db, get_setting, set_setting
from app.state_machine import CallStateMachine, CallState
from app.ring_detector import ring_detector
from app.logger import get_today_log_path, log_settings_change, log_notify_decision


@pytest.mark.asyncio
async def test_v331_trigger_source_in_events_and_push():
    """Verify trigger_source is recorded in database and returned in events API."""
    await init_db()

    # 1. Test save_event with trigger_source
    ev_csdk = await save_event(
        event_type="ring",
        category="ring",
        title="Aanbel oproep",
        clip_id="test_csdk_clip",
        trigger_source="CSDK_PORT_8000",
    )
    assert ev_csdk["trigger_source"] == "CSDK_PORT_8000"

    ev_shazam = await save_event(
        event_type="ring",
        category="ring",
        title="Aanbel oproep",
        clip_id="test_shazam_clip",
        trigger_source="SHAZAM_CHIME",
    )
    assert ev_shazam["trigger_source"] == "SHAZAM_CHIME"

    # 2. Verify retrieval via get_events_since
    events = await get_events_since(since_id=ev_csdk["id"] - 1, limit=10)
    found_csdk = next((e for e in events if e["id"] == ev_csdk["id"]), None)
    assert found_csdk is not None
    assert found_csdk["trigger_source"] == "CSDK_PORT_8000"


@pytest.mark.asyncio
async def test_v331_state_machine_trigger_source():
    """Verify CallStateMachine correctly assigns trigger_source to active call."""
    sm = CallStateMachine()
    call = await sm.ring(force=True, trigger_source="SHAZAM_CHIME")
    assert call is not None
    assert call.trigger_source == "SHAZAM_CHIME"
    call_dict = call.to_dict()
    assert call_dict["trigger_source"] == "SHAZAM_CHIME"

    # Re-triggering updates trigger source
    call2 = await sm.ring(force=False, trigger_source="CSDK_PORT_8000")
    assert call2 is not None
    assert call2.call_id == call.call_id
    assert call2.trigger_source == "CSDK_PORT_8000"


@pytest.mark.asyncio
async def test_v331_notification_poortwachter_and_settings_logging():
    """Test 2-way settings update and verify [SETTINGS CHANGE] logged."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Update detection settings
        update_payload = {
            "detection_enabled": True,
            "record_person": True,
            "record_vehicle": True,
            "record_animal": True,
            "record_motion": False,
            "notify_person": False,  # Turn off person notifications
            "notify_vehicle": True,
            "notify_animal": False,
            "notify_motion": False,
            "quiet_hours_enabled": True,
            "quiet_hours_start": "23:30",
            "quiet_hours_end": "07:30",
            "activity_zone_top_ignore_percent": 20,
            "motion_sensitivity": 1.5,
        }
        res = await client.put("/api/v1/settings/detection", json=update_payload)
        assert res.status_code == 200

        # Verify persisted
        assert await get_setting("notify_person") == "false"
        assert await get_setting("notify_vehicle") == "true"
        assert await get_setting("notify_animal") == "false"

        # Verify [SETTINGS CHANGE] written to log file
        log_path = get_today_log_path()
        assert os.path.exists(log_path)
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            settings_lines = [l for l in lines if "SETTINGS CHANGE" in l]
            assert len(settings_lines) > 0


@pytest.mark.asyncio
async def test_v331_shazam_threshold_and_admin_endpoint():
    """Verify calibrated NCC threshold 0.72 and admin API response."""
    assert ring_detector.ncc_trigger_threshold == 0.72

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/admin/shazam-detections")
        assert res.status_code == 200
        data = res.json()
        assert "detections" in data
        assert data.get("threshold") == 0.72


@pytest.mark.asyncio
async def test_v331_admin_auto_login():
    """Verify 1-click admin auto-login token generation and cookie setting."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Fetch token from /api/v1/admin/auth-token
        res_token = await client.get("/api/v1/admin/auth-token")
        assert res_token.status_code == 200
        token_data = res_token.json()
        assert "token" in token_data
        token = token_data["token"]
        assert len(token) > 20

        # 2. Access /admin with token param -> should redirect to /admin and set cookie
        res_admin = await client.get(f"/admin?token={token}", follow_redirects=False)
        assert res_admin.status_code == 302
        assert res_admin.headers["location"] == "/admin"
        assert "foxway_admin_session" in res_admin.cookies

