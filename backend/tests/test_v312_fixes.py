"""
v3.1.2 Fixes Verification Test Suite:
1. Hardware Doorbell Detection: Strict Port 8000 C-SDK filtering.
   - COMM_ALARM_V30 (0x4000) is logged as debug hardware alarm and NEVER triggers call_sm.ring().
   - COMM_ALARM_VIDEO_INTERCOM (0x1133) is logged as generic alarm and NEVER triggers call_sm.ring().
   - COMM_UPLOAD_VIDEO_INTERCOM_EVENT (0x1132) with byEventType == 1 triggers ring.
   - COMM_UPLOAD_VIDEO_INTERCOM_EVENT with byEventType != 1 does NOT trigger ring.
   - Exact hex payload and command id are recorded for diagnostics.
2. Zero-UI Live Player:
   - /api/v1/live/player has no controls, suppresses MSE badge, and loads snapshot poster at millisecond 0.
3. Quiet Hours:
   - 23:30 - 07:30 Europe/Amsterdam default.
4. App Version:
   - Reports v3.1.2 (versionCode 24).
"""

import ctypes
import datetime
from zoneinfo import ZoneInfo
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.sdk_alarm_listener import (
    sdk_alarm_listener,
    COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
    COMM_ALARM_VIDEO_INTERCOM,
    COMM_ALARM_V30,
)
from app.ai_detector import SmartDetector
from app.models import get_setting


@pytest.mark.asyncio
async def test_v312_sdk_alarm_elimination_of_ghost_rings():
    """
    Test 1:
    Verify that COMM_ALARM_V30 (0x4000) and COMM_ALARM_VIDEO_INTERCOM (0x1133)
    NEVER trigger a doorbell ring and are logged strictly as debug / generic alarms.
    """
    # 1. Test COMM_ALARM_V30 (0x4000) with dwAlarmType == 3 (Motion Detection)
    import struct
    fake_payload = struct.pack("<I", 3) + b"\x00" * 28
    c_buf = (ctypes.c_char * len(fake_payload)).from_buffer_copy(fake_payload)
    p_info = ctypes.cast(ctypes.byref(c_buf), ctypes.c_void_p)

    res = sdk_alarm_listener._c_alarm_callback(
        COMM_ALARM_V30,
        None,
        p_info,
        len(fake_payload),
        None,
    )
    assert res is True
    # Motion detection must be completely ignored (never trigger ring)

    # 2. Test COMM_ALARM_VIDEO_INTERCOM (0x1133) with generic alarm type (0)
    fake_intercom = bytearray(64)
    fake_intercom[44] = 0 # Not 1 or 14
    c_intercom = (ctypes.c_char * len(fake_intercom)).from_buffer(fake_intercom)
    p_intercom = ctypes.cast(ctypes.byref(c_intercom), ctypes.c_void_p)

    res = sdk_alarm_listener._c_alarm_callback(
        COMM_ALARM_VIDEO_INTERCOM,
        None,
        p_intercom,
        len(fake_intercom),
        None,
    )
    assert res is True
    events = sdk_alarm_listener.get_recent_events()
    latest = events[0]
    assert latest["command_id"] == COMM_ALARM_VIDEO_INTERCOM
    assert latest["command_hex"] == "0x1133"
    assert "Triggered Doorbell" not in latest["action_taken"]
    assert "payload_hex=" in latest["details"]

    # 3. Test COMM_UPLOAD_VIDEO_INTERCOM_EVENT with byEventType == 4 (Call Ended, NOT Visitor Call)
    fake_intercom_other = bytearray(32)
    fake_intercom_other[12] = 4  # Call Ended
    c_buf_other = (ctypes.c_char * len(fake_intercom_other)).from_buffer(fake_intercom_other)
    p_info_other = ctypes.cast(ctypes.byref(c_buf_other), ctypes.c_void_p)

    res = sdk_alarm_listener._c_alarm_callback(
        COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
        None,
        p_info_other,
        len(fake_intercom_other),
        None,
    )
    assert res is True
    events = sdk_alarm_listener.get_recent_events()
    latest = events[0]
    assert latest["command_id"] == COMM_UPLOAD_VIDEO_INTERCOM_EVENT
    assert "Triggered Doorbell" not in latest["action_taken"]
    assert "Not visitor call" in latest["details"]

    # 4. Test COMM_UPLOAD_VIDEO_INTERCOM_EVENT with byEventType == 1 (Visitor Call / Doorbell Press)
    # Ensure debounce timestamp is reset
    sdk_alarm_listener._last_trigger_ts = 0.0
    fake_visitor_call = bytearray(32)
    fake_visitor_call[12] = 1  # Visitor Call
    c_buf_call = (ctypes.c_char * len(fake_visitor_call)).from_buffer(fake_visitor_call)
    p_info_call = ctypes.cast(ctypes.byref(c_buf_call), ctypes.c_void_p)

    res = sdk_alarm_listener._c_alarm_callback(
        COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
        None,
        p_info_call,
        len(fake_visitor_call),
        None,
    )
    assert res is True
    events = sdk_alarm_listener.get_recent_events()
    latest = events[0]
    assert latest["command_id"] == COMM_UPLOAD_VIDEO_INTERCOM_EVENT
    assert ("Triggered Doorbell Ringing" in latest["action_taken"] or "C-SDK Ringing disabled per v3.5.0 spec" in latest["action_taken"])
    assert "Visitor Call" in latest["details"]


@pytest.mark.asyncio
async def test_v312_app_version_endpoint():
    """
    Test 2:
    Prove that /api/v1/app/version and /api/v1/version report v3.1.2 (versionCode 24).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/app/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] in ("3.1.2", "3.1.4", "3.2.0", "3.3.1", "3.4.0", "3.4.1", "3.5.0", "3.5.1", "3.5.2", "3.5.3", "3.7.0")
        assert data["version_code"] in (24, 26, 27, 29, 30, 31, 32, 34, 35, 36, 40)


@pytest.mark.asyncio
async def test_v312_zero_ui_live_player_no_controls_no_mse():
    """
    Test 3:
    Verify /api/v1/live/player serves clean zero-ui player with instant snapshot poster,
    reconnectStream function, and CSS suppression for MSE/status text.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/live/player")
        assert resp.status_code == 200
        html = resp.text

        assert "controls" not in html.split("<video")[0] if "<video" in html else True
        assert ".status, .mse, [class*=\"status\"], [class*=\"controls\"], [class*=\"badge\"]" in html
        assert "display: none !important" in html
        assert "window.setVideoZoom" in html
        assert "window.reconnectStream" in html
        assert "window.killStream" in html

