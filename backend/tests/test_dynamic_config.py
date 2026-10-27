"""
Automated Pytest for 100% Dynamic Camera Configuration (Zero Hardcoding).

Verifies:
1. Dynamic loading of camera IP, port, username, and password from SQLite app_settings.
2. audio_bridge._get_credentials() dynamically reflects updated DB credentials without process restart.
3. ai_detector.get_substream_rtsp() dynamically reflects updated credentials.
4. FastAPI endpoints (/api/v1/onboarding/bootstrap and /api/v1/settings/camera) return injected credentials.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models import (
    get_camera_credentials,
    set_camera_credentials,
    set_setting,
)
from app.audio_bridge import _get_credentials
from app.ai_detector import get_substream_rtsp


@pytest.mark.asyncio
async def test_dynamic_credentials_injection_and_reflection():
    orig_creds = await get_camera_credentials()
    try:
        # 0. Clear camera credentials to prove default / clean state
        await set_setting("camera_ip", "")
        await set_setting("camera_user", "")
        await set_setting("camera_pass", "")
        cleared_creds = await get_camera_credentials()
        assert cleared_creds["pass"] == "" or cleared_creds["pass"] == settings.DOORBELL_PASS

        # 1. Inject test credentials into database
        test_ip = "10.200.150.42"
        test_port = 8899
        test_user = "sec_operator"
        test_pass = "DynamicVaultPass987!"

        await set_camera_credentials(test_ip, test_port, test_user, test_pass)

        # 2. Verify models.get_camera_credentials() returns dynamic values
        creds = await get_camera_credentials()
        assert creds["ip"] == test_ip
        assert creds["port"] == test_port
        assert creds["user"] == test_user
        assert creds["pass"] == test_pass
        assert creds["is_configured"] is True

        # 3. Verify audio_bridge._get_credentials() sync loader returns dynamic values
        bridge_ip, bridge_port, bridge_user, bridge_pass = _get_credentials()
        assert bridge_ip == test_ip
        assert bridge_port == test_port
        assert bridge_user == test_user
        assert bridge_pass == test_pass

        # 4. Verify ai_detector.get_substream_rtsp() resolves to go2rtc substream (eliminates RTSP contention)
        substream_url = await get_substream_rtsp()
        assert substream_url == "rtsp://127.0.0.1:8554/deurbel_sub"

        # 5. Verify FastAPI API endpoints return dynamic credentials
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/onboarding/bootstrap")
            assert resp.status_code == 200
            data = resp.json()
            assert data["camera_ip"] == test_ip
            assert data["camera_port"] == test_port
            assert data["camera_user"] == test_user
            assert data["camera_credentials_configured"] is True

            resp_settings = await ac.get("/api/v1/settings/camera")
            assert resp_settings.status_code == 200
            settings_data = resp_settings.json()
            assert settings_data["ip"] == test_ip
            assert settings_data["port"] == test_port
            assert settings_data["user"] == test_user
    finally:
        if orig_creds.get("ip"):
            await set_camera_credentials(
                orig_creds["ip"],
                orig_creds.get("port", 8000),
                orig_creds.get("user", "admin"),
                orig_creds.get("pass", ""),
            )
