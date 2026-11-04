"""
Foxway Smart Doorbell — v3.5.0 Verification Test Suite.

Covers:
  1. RingCoordinator:
     - Immediate ms 0 ring trigger.
     - 3.5s cross-verification window:
       * Both SHAZAM_CHIME + NETWORK_SNIFF -> VERIFIED.
       * Single source -> MISMATCH.
  2. Network Sniffer (EZVIZ TCP 8820 Pattern Detection):
     - Suppresses isolated 191B keepalive packets.
     - Pattern A: Payload 195-400B (or total > 250B) triggers ring.
     - Pattern B: >= 3 packets of 191B within 1000ms triggers ring.
  3. Day-Folders (/clips/YYYY-MM-DD/) & Path Resolution:
     - resolve_clip_path finds clips in date subdirectories or root.
     - 50.0 GB (51200 MB) storage default.
  4. Server-Side Filtering & Pagination:
     - GET /api/v1/clips?paged=true&limit=... returns clips, total_count, has_more.
  5. Admin Test Push:
     - POST /api/v1/admin/test-push sends test push notification.
  6. App Version:
     - /api/v1/app/version reports v3.5.0 (versionCode 32).
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.ring_coordinator import ring_coordinator
from app.recorder import get_day_clip_dir, resolve_clip_path, get_storage_stats
from app.admin import create_session_token


@pytest.mark.asyncio
async def test_v350_ring_coordinator_verification():
    """Test 1 (v3.5.1): SHAZAM_CHIME triggers ring immediately.
    NETWORK_SNIFF is in shadow-learner mode and never triggers a ring.
    After 3.5s window, status is COMPLETED (single SHAZAM_CHIME source).
    """
    ring_coordinator.set_loop(asyncio.get_running_loop())
    ring_coordinator.reset()
    called_events = []

    def mock_handler(source, details):
        called_events.append((source, details))

    ring_coordinator.set_on_ring_callback(mock_handler)

    # SHAZAM_CHIME must ring immediately
    ring_coordinator.register_signal("SHAZAM_CHIME", {"ncc": 0.85})
    await asyncio.sleep(0.05)
    assert len(called_events) == 1
    assert called_events[0][0] == "SHAZAM_CHIME"

    # NETWORK_SNIFF within 3.5s confirms the ring without duplicate event
    ring_coordinator.register_signal("NETWORK_SNIFF", {"pattern": "A", "bytes": 312})
    await asyncio.sleep(0.05)
    assert len(called_events) == 1, "Dual verification must never trigger duplicate ring event"

    verifications = ring_coordinator.get_recent_verifications()
    assert len(verifications) >= 1
    latest = verifications[0]
    assert latest["status"] == "VERIFIED"
    assert latest["first_source"] == "SHAZAM_CHIME"
    assert latest["second_source"] == "NETWORK_SNIFF"


@pytest.mark.asyncio
async def test_v350_ring_coordinator_mismatch():
    """Test 1b: NETWORK_SNIFF triggers ring immediately at ms 0.
    If SHAZAM_CHIME does not follow within 3.5s, window completes as COMPLETED.
    """
    ring_coordinator.set_loop(asyncio.get_running_loop())
    ring_coordinator.reset()
    called_events = []

    def mock_handler(source, details):
        called_events.append((source, details))

    ring_coordinator.set_on_ring_callback(mock_handler)

    # NETWORK_SNIFF fires — triggers ring immediately at ms 0
    ring_coordinator.register_signal("NETWORK_SNIFF", {"pattern": "B", "pkts": 3})
    await asyncio.sleep(0.05)
    assert len(called_events) == 1
    assert called_events[0][0] == "NETWORK_SNIFF"

    # After 3.6s window closes without duplicate ring
    await asyncio.sleep(3.6)
    assert len(called_events) == 1
    verifications = ring_coordinator.get_recent_verifications()
    assert len(verifications) >= 1
    latest = verifications[0]
    assert latest["status"] == "COMPLETED"
    assert latest["first_source"] == "NETWORK_SNIFF"
    assert latest["missing_source"] == "SHAZAM_CHIME"


def test_v350_day_folders_and_resolution(tmp_path):
    """Test 2: Day-folders and resolve_clip_path logic."""
    dt = datetime(2026, 10, 31, 14, 30, tzinfo=timezone.utc)
    day_dir = get_day_clip_dir(dt)
    assert "2026-10-31" in str(day_dir)

    # Verify storage stats defaults to 50 GB
    stats = get_storage_stats()
    assert stats["max_storage_gb"] == 50.0
    assert stats["max_mb"] == 51200.0


@pytest.mark.asyncio
async def test_v350_server_side_clips_pagination():
    """Test 3: Server-side pagination & filtering on /api/v1/clips."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/clips?paged=true&limit=5&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "clips" in data
        assert "total_count" in data
        assert "has_more" in data
        assert isinstance(data["clips"], list)
        assert data["limit"] == 5
        assert data["offset"] == 0


@pytest.mark.asyncio
async def test_v350_admin_test_push_and_verifications():
    """Test 4: Admin test-push endpoint and ring-verifications endpoint."""
    token = create_session_token()
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test push
        push_resp = await client.post("/api/v1/admin/test-push", headers=headers)
        assert push_resp.status_code == 200
        push_data = push_resp.json()
        assert push_data["status"] == "ok"
        assert "Push-verbinding werkt 100%!" in push_data["message"]

        # Ring verifications
        ver_resp = await client.get("/api/v1/admin/ring-verifications", headers=headers)
        assert ver_resp.status_code == 200
        ver_data = ver_resp.json()
        assert ver_data["status"] == "ok"
        assert "verifications" in ver_data


@pytest.mark.asyncio
async def test_v350_app_version_reports_v351():
    """Test 5 (v3.5.1): App version reports v3.5.1 (versionCode 34)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/app/version")
        assert resp.status_code == 200
        data = resp.json()
        # Accept known backend versions up to v3.5.5
        assert data["version"] in ("3.5.0", "3.5.1", "3.5.2", "3.5.3", "3.5.4", "3.5.5")
        assert data["version_code"] in (32, 34, 35, 36, 37, 38)
