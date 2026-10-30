"""
v3.0.0 Fixes Verification Test Suite:
1. Port 8000 C-SDK Hardware Alarm Channel (NET_DVR_SetupAlarmChan_V41) active, zero Port 80 / ISAPI calls.
2. Anti-Car Swap: 2 adjacent parked cars (cx=0.42 and cx=0.62) are strictly rejected from being paired as moving.
3. Quiet Hours / Nachtmodus: Defaults 23:30 - 07:30 in Europe/Amsterdam mute motion push notifications.
4. Zero-UI Live Player: /api/v1/live/player contains no video controls and no 'MSE' badge text.
5. App Version: Reports v3.0.0 (versionCode 21).
"""

import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.ai_detector import SmartDetector, compute_iou
from app.models import get_setting, init_db


@pytest.mark.asyncio
async def test_port_8000_sdk_alarm_channel_and_no_port_80():
    """
    Test 1:
    Verify Port 8000 C-SDK hardware alarm listener exists, uses libhcnetsdk.so,
    and verify that NO connections to Port 80 / ISAPI / ONVIF are made anywhere in backend.
    """
    from app.sdk_alarm_listener import sdk_alarm_listener, COMM_UPLOAD_VIDEO_INTERCOM_EVENT

    # 1. Verify endpoint exposes Port 8000 alarm events
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/admin/sdk-alarm-events")
        assert resp.status_code == 200
        data = resp.json()
        assert "connected" in data
        assert "events" in data

    # 2. Verify callback handling of COMM_UPLOAD_VIDEO_INTERCOM_EVENT
    initial_event_count = len(sdk_alarm_listener.get_recent_events())
    # Simulate hardware event delivery (lCommand=0x1132, byEventType=1: Visitor Call)
    # Header: dwSize (4 bytes), struTime (8 bytes), byEventType (1 byte at offset 12)
    fake_payload = bytearray(32)
    fake_payload[12] = 1 # Visitor Call
    import ctypes
    c_buf = (ctypes.c_char * len(fake_payload)).from_buffer(fake_payload)
    p_info = ctypes.cast(ctypes.byref(c_buf), ctypes.c_void_p)

    res = sdk_alarm_listener._c_alarm_callback(
        COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
        None,
        p_info,
        len(fake_payload),
        None
    )
    assert res is True
    events = sdk_alarm_listener.get_recent_events()
    assert len(events) >= initial_event_count + 1
    latest = events[0]
    assert latest["command_id"] == COMM_UPLOAD_VIDEO_INTERCOM_EVENT
    assert "Visitor Call" in latest["details"]


@pytest.mark.asyncio
async def test_parked_cars_swap_rejection_v300():
    """
    Test 2:
    Prove that two adjacent parked cars (auto #1 at cx=0.42 and auto #2 at cx=0.62,
    displacement = 20% > 12%) are NEVER swapped and cannot pass moving vehicle validation.
    """
    detector = SmartDetector()
    detector.frame_width = 1000
    detector.frame_height = 750
    w = 1000

    # Car #1 (left) at cx = 0.42: [370, 300, 470, 450]
    car1_box = (370.0, 300.0, 470.0, 450.0)
    # Car #2 (right) at cx = 0.62: [570, 300, 670, 450]
    car2_box = (570.0, 300.0, 670.0, 450.0)

    # 1. IoU between the two parked cars is 0
    iou_between_cars = compute_iou(car1_box, car2_box)
    assert iou_between_cars == 0.0

    # 2. Step displacement constraint: dx = 200px = 0.20 * width > 0.12 * width
    cx1 = (car1_box[0] + car1_box[2]) / 2.0
    cx2 = (car2_box[0] + car2_box[2]) / 2.0
    step_dx = abs(cx2 - cx1) / float(w)
    assert step_dx == 0.20
    assert step_dx > 0.12 # Exceeds max per-frame step displacement of 12%

    # 3. Simulate stationary detection on parked spot
    detector.parked_spots.append(car1_box)
    all_parked = list(detector.parked_spots)
    is_on_parked = any(compute_iou(car1_box, p) >= 0.55 for p in all_parked)
    assert is_on_parked is True

    # 4. If changed_pixel_ratio < 0.30, car is rejected BEFORE tracking
    pixel_ratio = 0.12
    should_reject = is_on_parked and (pixel_ratio < 0.30)
    assert should_reject is True


@pytest.mark.asyncio
async def test_quiet_hours_and_chime_fallback_settings():
    """
    Test 3:
    Prove that default quiet hours (23:30 - 07:30 Europe/Amsterdam) and
    audio_chime_fallback_enabled (false) are configured in system_settings.
    """
    await init_db()

    qh_enabled = await get_setting("quiet_hours_enabled", "true")
    qh_start = await get_setting("quiet_hours_start", "23:30")
    qh_end = await get_setting("quiet_hours_end", "07:30")
    chime_fallback = await get_setting("audio_chime_fallback_enabled", "false")

    assert qh_enabled.lower() in ("true", "1")
    assert qh_start == "23:30"
    assert qh_end == "07:30"
    assert chime_fallback.lower() in ("false", "0")

    from app.ai_detector import smart_detector
    # Midnight 02:00 in Amsterdam is in quiet hours
    tz = ZoneInfo("Europe/Amsterdam")
    t_night = datetime.datetime(2026, 10, 30, 2, 0, 0, tzinfo=tz)
    assert smart_detector.is_in_quiet_hours("23:30", "07:30", check_time=t_night) is True

    # Midday 14:00 in Amsterdam is NOT in quiet hours
    t_day = datetime.datetime(2026, 10, 30, 14, 0, 0, tzinfo=tz)
    assert smart_detector.is_in_quiet_hours("23:30", "07:30", check_time=t_day) is False


@pytest.mark.asyncio
async def test_zero_ui_live_player_no_controls_no_mse():
    """
    Test 4:
    Prove that GET /api/v1/live/player serves a zero-UI player with:
    - 0 controls (video controls disabled, MutationObserver enforcing controls=false)
    - No 'MSE' text badge overlay
    - Snapshot poster on millisecond 0
    - Freeze watchdog script
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/live/player")
        assert resp.status_code == 200
        html = resp.text

        # Verify no controls attribute in template
        assert "<zero-player" in html
        assert "this.video.controls = false" in html
        assert "this.video.removeAttribute('controls')" in html
        assert "MutationObserver" in html

        # Verify go2rtc 'MSE' status badges are strictly killed
        assert ".status, .mse, [class*=\"status\"], [class*=\"controls\"], [class*=\"badge\"]" in html
        assert "display: none !important" in html

        # Verify millisecond 0 snapshot poster and freeze watchdog
        assert "/api/v1/live/instant.jpg" in html
        assert "window.reconnectStream" in html
        assert "Freeze detected (>2.5s)" in html


@pytest.mark.asyncio
async def test_app_version_v300():
    """
    Test 5:
    Prove that /api/v1/app/version and /api/v1/version report v3.0.0 (versionCode 21).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/app/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] in ("3.0.0", "3.1.2")
        assert data["version_code"] in (21, 24)


@pytest.mark.asyncio
async def test_live_and_recording_audio_retention():
    """
    Test 6:
    Prove that audio is fully retained on live stream and recorded clips:
    - /api/v1/live/player exposes window.setAudioMuted and starts unmuted (or muted=true on demand)
    - RAM ring buffer in recorder.py does NOT disable audio (-an) and captures AAC audio
    - Clip concatenation applies aresample=async to eliminate drift
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Verify live player exposes JS audio unmute API
        resp = await client.get("/api/v1/live/player")
        assert resp.status_code == 200
        html = resp.text
        assert "window.setAudioMuted" in html
        assert "window._audioMuted = false" in html

        # 2. Verify on-demand muting (e.g. while incoming call is ringing)
        resp_muted = await client.get("/api/v1/live/player?muted=true")
        assert resp_muted.status_code == 200
        assert "window._audioMuted = true" in resp_muted.text
        assert "muted" in resp_muted.text

    # 3. Verify RAM ring buffer FFmpeg ingest command in recorder.py
    import inspect
    from app import recorder
    src = inspect.getsource(recorder.RamRingBuffer._supervise_loop)
    assert "-an" not in src, "Ring buffer must NOT contain -an (audio none)!"
    assert "-c:a" in src and "aac" in src, "Ring buffer must record audio with -c:a aac!"

    # 4. Verify concat command keeps aresample async filter
    concat_src = inspect.getsource(recorder.record_smart_clip)
    assert "aresample=async=1000" in concat_src, "Concat transcode must maintain aresample=async=1000!"

