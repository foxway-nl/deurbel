"""
Test 4: Port 8000 Audio Bridge concurrency lock and graceful error handling.
"""

import pytest
from app.audio_bridge import force_close_active_voicecom, _voicecom_thread_lock

def test_force_close_active_voicecom():
    # Calling force_close_active_voicecom should never raise an exception
    force_close_active_voicecom()
    assert True

def test_voicecom_thread_lock():
    with _voicecom_thread_lock:
        # Re-entrant acquisition must succeed without deadlock
        with _voicecom_thread_lock:
            pass
    assert True

@pytest.mark.asyncio
async def test_audio_speak_structured_error(async_client):
    # Test speak endpoint with non-existent reply ID
    resp = await async_client.post("/api/v1/audio/speak?reply_id=non_existent_clip_123")
    # Must return 404 with structured JSON, never an unhandled 500
    assert resp.status_code == 404
    data = resp.json()
    assert "detail" in data
