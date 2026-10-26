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
    assert data["version"] == "1.8.0"
    assert data["version_code"] == 9
    assert "apk_url" in data
    assert "changelog" in data
