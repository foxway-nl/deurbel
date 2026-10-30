"""
Test 1: Health, Doorbell Status, and Stream Proxies.
"""

import pytest

@pytest.mark.asyncio
async def test_status_endpoint(async_client):
    resp = await async_client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "state" in data
    assert "connected_ws_users" in data

@pytest.mark.asyncio
async def test_doorbell_health_endpoint(async_client):
    resp = await async_client.get("/api/v1/doorbell/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "online" in data
    assert "camera_ip" in data

@pytest.mark.asyncio
async def test_app_version_endpoint(async_client):
    resp = await async_client.get("/api/v1/app/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] in ("2.9.0", "3.0.0", "3.1.2", "3.1.4")
    assert data["version_code"] in (20, 21, 24, 26)
    assert "apk_url" in data
    assert "changelog" in data


@pytest.mark.asyncio
async def test_instant_ram_snapshot_endpoint(async_client):
    import time
    from app.live_hub import live_hub
    # Seed a dummy fresh JPEG frame
    live_hub.latest_jpeg = b"\xff\xd8\xff\xe0dummy_jpeg_bytes\xff\xd9"
    live_hub.last_fresh_frame_time = time.monotonic()
    resp = await async_client.get("/api/v1/live/instant.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"\xff\xd8\xff\xe0dummy_jpeg_bytes\xff\xd9"
