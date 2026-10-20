"""
Test 2: Onboarding bootstrap, QR payload, optional usernames, and unique username enforcement.
"""

import pytest
import uuid

@pytest.mark.asyncio
async def test_onboarding_bootstrap(async_client):
    resp = await async_client.get("/api/v1/onboarding/bootstrap")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "lan_url" in data
    assert "wan_url" in data
    assert "camera_ip" in data
    assert "camera_credentials_configured" in data

@pytest.mark.asyncio
async def test_onboarding_qr(async_client):
    resp = await async_client.get("/api/v1/onboarding/qr")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "payload" in data
    assert "token" in data["payload"]
    assert "qr_image_url" in data
    assert data["qr_image_url"].startswith("data:image/png;base64,")

@pytest.mark.asyncio
async def test_pairing_optional_username(async_client):
    device_id = f"test_dev_{uuid.uuid4().hex[:8]}"
    # Empty username -> should auto-generate "Onbekend #X"
    payload = {
        "user_name": "",
        "device_name": "Test Phone",
        "fcm_token": "fcm_test_token",
        "device_id": device_id,
        "android_version": "15",
        "connection_type": "LAN",
    }
    resp = await async_client.post("/api/v1/pair", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_name"].startswith("Onbekend #")
    assert "jwt_token" in data

@pytest.mark.asyncio
async def test_pairing_unique_username_enforcement(async_client):
    unique_name = f"UniqueUser_{uuid.uuid4().hex[:6]}"
    dev1 = f"dev1_{uuid.uuid4().hex[:6]}"
    dev2 = f"dev2_{uuid.uuid4().hex[:6]}"

    # Device 1 pairs with unique_name
    p1 = {
        "user_name": unique_name,
        "device_name": "Pixel 9",
        "fcm_token": "fcm1",
        "device_id": dev1,
        "android_version": "15",
    }
    resp1 = await async_client.post("/api/v1/pair", json=p1)
    assert resp1.status_code == 200

    # Device 2 tries to take the same name -> MUST return 409 Conflict
    p2 = {
        "user_name": unique_name,
        "device_name": "Galaxy S25",
        "fcm_token": "fcm2",
        "device_id": dev2,
        "android_version": "15",
    }
    resp2 = await async_client.post("/api/v1/pair", json=p2)
    assert resp2.status_code == 409
    assert "al gekoppeld" in resp2.json()["detail"]

    # Device 1 re-pairs with same name -> MUST succeed (same device)
    resp3 = await async_client.post("/api/v1/pair", json=p1)
    assert resp3.status_code == 200
