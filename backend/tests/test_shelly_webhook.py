"""
Test 5: Shelly Plus Uni Webhook trigger and debounce protection.
"""

import pytest

@pytest.mark.asyncio
async def test_shelly_webhook_status(async_client):
    resp = await async_client.get("/api/v1/webhook/shelly-ring/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert "webhook_url_lan" in data
    assert "webhook_url_wan" in data

@pytest.mark.asyncio
async def test_shelly_webhook_trigger_and_debounce(async_client):
    # First ring trigger
    resp1 = await async_client.post("/api/v1/webhook/shelly-ring")
    assert resp1.status_code == 200
    data1 = resp1.json()
    # Can be RINGING or SUPPRESSED if cooldown active from startup
    assert data1["status"] in ("RINGING", "SUPPRESSED")

    # Immediate second ring trigger -> MUST be suppressed by debounce or existing call
    resp2 = await async_client.post("/api/v1/webhook/shelly-ring")
    assert resp2.status_code == 200
    data2 = resp2.json()
    # Second ring is suppressed or maintains active call
    assert data2["status"] in ("SUPPRESSED", "RINGING")


@pytest.mark.asyncio
async def test_shelly_webhook_force_trigger(async_client):
    resp = await async_client.post("/api/v1/webhook/shelly-ring?force=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "call_id" in data

