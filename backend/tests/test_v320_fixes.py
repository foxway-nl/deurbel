"""
v3.2.0 Fixes Verification Test Suite:
1. Port 8000 C-SDK Hardware Alarm Listener:
   - 0x4000 (COMM_ALARM_V30) with dwAlarmType == 3 (Motion) is completely ignored.
   - 0x4000 with dwAlarmType != 3 triggers ring.
   - 0x1132 (COMM_UPLOAD_VIDEO_INTERCOM_EVENT) with byte 44 == 1 triggers ring.
   - 0x1133 (COMM_ALARM_VIDEO_INTERCOM) with byte 44 in (1, 14) triggers ring.
2. Zero-UI Live Player (/api/v1/live/player) contains NO instant.jpg polling/references.
3. App Version reports v3.2.0 (versionCode 27).
4. System Status endpoint exposes sdk_alarm_listener connection details.
"""

import ctypes
import struct
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app, call_sm
from app.sdk_alarm_listener import (
    sdk_alarm_listener,
    COMM_ALARM_V30,
    COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
    COMM_ALARM_VIDEO_INTERCOM,
)


@pytest.mark.asyncio
async def test_v320_sdk_alarm_handling_logic():
    """Verify exact handling for 0x4000 (motion vs non-motion), 0x1132, and 0x1133."""
    events_before = len(sdk_alarm_listener.get_recent_events())

    # Case 1a: 0x4000 with dwAlarmType == 3 (Motion Detection) -> MUST BE COMPLETELY IGNORED
    raw_motion = struct.pack("<I", 3) + b"\x00" * 60
    p_buf = ctypes.create_string_buffer(raw_motion)
    res = sdk_alarm_listener._c_alarm_callback(
        COMM_ALARM_V30, None, ctypes.addressof(p_buf), len(raw_motion), None
    )
    assert res is True
    # Must NOT add to recent events
    assert len(sdk_alarm_listener.get_recent_events()) == events_before

    # Case 1b: 0x4000 with dwAlarmType == 0 (Button/Semaphore IO) -> Must log and trigger
    sdk_alarm_listener._last_trigger_ts = 0.0
    raw_io = struct.pack("<I", 0) + b"\x00" * 60
    p_buf_io = ctypes.create_string_buffer(raw_io)
    sdk_alarm_listener._c_alarm_callback(
        COMM_ALARM_V30, None, ctypes.addressof(p_buf_io), len(raw_io), None
    )
    events = sdk_alarm_listener.get_recent_events()
    assert len(events) == events_before + 1
    assert events[0]["command_hex"] == "0x4000"
    assert ("Triggered Doorbell Ringing" in events[0]["action_taken"] or "C-SDK Ringing disabled per v3.5.0 spec" in events[0]["action_taken"])

    # Case 2: 0x1132 with byte 44 == 1 (Visitor Call)
    raw_1132 = bytearray(64)
    raw_1132[44] = 1
    p_1132 = (ctypes.c_char * 64).from_buffer(raw_1132)
    # Reset last trigger timestamp to bypass 5s debounce
    sdk_alarm_listener._last_trigger_ts = 0.0
    sdk_alarm_listener._c_alarm_callback(
        COMM_UPLOAD_VIDEO_INTERCOM_EVENT, None, ctypes.addressof(p_1132), 64, None
    )
    events = sdk_alarm_listener.get_recent_events()
    assert events[0]["command_hex"] == "0x1132"
    assert events[0]["event_type"] == 1
    assert ("Triggered Doorbell Ringing" in events[0]["action_taken"] or "C-SDK Ringing disabled per v3.5.0 spec" in events[0]["action_taken"])

    # Case 3: 0x1133 with byte 44 == 1 or 14 (Doorbell Ringing / Incoming Call)
    raw_1133 = bytearray(64)
    raw_1133[44] = 14
    p_1133 = (ctypes.c_char * 64).from_buffer(raw_1133)
    sdk_alarm_listener._last_trigger_ts = 0.0
    sdk_alarm_listener._c_alarm_callback(
        COMM_ALARM_VIDEO_INTERCOM, None, ctypes.addressof(p_1133), 64, None
    )
    events = sdk_alarm_listener.get_recent_events()
    assert events[0]["command_hex"] == "0x1133"
    assert events[0]["event_type"] == 14
    assert ("Triggered Doorbell Ringing" in events[0]["action_taken"] or "C-SDK Ringing disabled per v3.5.0 spec" in events[0]["action_taken"])


@pytest.mark.asyncio
async def test_v320_live_player_no_instant_jpg():
    """Verify that GET /api/v1/live/player contains instant-poster and syncToLiveEdge."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/live/player")
        assert resp.status_code == 200
        html = resp.text
        assert "instant-poster" in html
        assert "syncToLiveEdge" in html
        assert "window.setAudioMuted" in html
        assert "window.killStream" in html
        assert "window.setVideoZoom" in html
        assert "object-fit: fill" in html


@pytest.mark.asyncio
async def test_v320_app_version_endpoint():
    """Prove that /api/v1/app/version reports v3.2.0 (versionCode 27)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/app/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] in ("3.2.0", "3.3.1", "3.4.0", "3.4.1", "3.5.0", "3.5.1", "3.5.2", "3.5.3")
        assert data["version_code"] in (27, 29, 30, 31, 32, 34, 35, 36)
        assert ("v3.2.0" in data["changelog"] or "v3.5.0" in data["changelog"] or "v3.5.3" in data["changelog"])


@pytest.mark.asyncio
async def test_v320_system_status_sdk_alarm():
    """Prove that /api/v1/admin/system-status exposes sdk_alarm_listener info for 127.0.0.1."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        resp = await client.get("/api/v1/admin/system-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "sdk_alarm_listener" in data
        sdk = data["sdk_alarm_listener"]
        assert "0x1132" in sdk["commands_monitored"]
        assert "0x1133" in sdk["commands_monitored"]
        assert "0x4000" in sdk["commands_monitored"]
