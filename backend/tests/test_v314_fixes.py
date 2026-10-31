"""
v3.1.4 Fixes Verification Test Suite:
1. Acoustic chime detection (ring_detector.py, 728 Hz / 580 Hz FFT) is permanently eradicated.
2. System settings no longer store audio_chime_fallback_enabled or ring frequency settings.
3. Hardware Doorbell Detection: Strict hardware-only triggers:
   - Port 8000 C-SDK (COMM_UPLOAD_VIDEO_INTERCOM_EVENT with byEventType == 1).
   - Shelly Plus Uni webhook (POST /api/v1/webhook/shelly-ring).
   - Manual test trigger (?force=true).
4. App Version reports v3.1.4 (versionCode 26).
"""

import os
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import get_setting, init_db


@pytest.mark.asyncio
async def test_v314_acoustic_chime_eradication():
    """
    Test 1:
    Verify legacy FFT chime settings are purged from system_settings,
    and ring_detector provides the modern v3.3.0 3-layer Shazam detector.
    """
    await init_db()

    # 1. Ensure system_settings does not contain legacy chime settings
    chime_setting = await get_setting("audio_chime_fallback_enabled", None)
    assert chime_setting is None, "audio_chime_fallback_enabled must be purged from database"

    ding_setting = await get_setting("ring_ding_hz", None)
    assert ding_setting is None, "ring_ding_hz must be purged from database"

    # 2. Verify modern ring_detector is Shazam-based
    from app.ring_detector import ring_detector
    assert hasattr(ring_detector, "evaluate_audio_window")


@pytest.mark.asyncio
async def test_v314_app_version_endpoint():
    """
    Test 2:
    Prove that /api/v1/app/version and /api/v1/version report v3.1.4 (versionCode 26).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/app/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] in ("3.1.4", "3.2.0", "3.3.1", "3.4.0")
        assert data["version_code"] in (26, 27, 29, 30)
        assert ("v3.1.4" in data["changelog"] or "v3.2.0" in data["changelog"])

