"""
Foxway Smart Doorbell — Test Suite for v3.5.1:
1. Complete detachment of Network Sniffer from Doorbell Ring Trigger (Passive Differential Byte Learner).
2. ONLY SHAZAM_CHIME & manual test trigger calls; NETWORK_SNIFF never triggers ringing.
3. Differential byte learner isolates unique candidates into sniffer_learning.jsonl and exposes status.
4. Stuck-Recording Sweeper finalizes clips (>10KB) to READY or deletes invalid records.
5. Thumbnail endpoint always delivers HTTP 200 image/jpeg.
6. Live player HTML specifies mode="mse,webrtc,mp4" for native AAC audio.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.network_sniffer import NetworkSniffer, network_sniffer
from app.ring_coordinator import ring_coordinator
from app.recorder import (
    sweep_stuck_recordings,
    get_day_clip_dir,
    resolve_clip_path,
)
from app.models import save_clip, get_clip, delete_clip
from app.config import settings


@pytest.mark.asyncio
async def test_v351_sniffer_detached_from_ring():
    """Verify that NETWORK_SNIFF never initiates a ring or call (v3.5.1)."""
    ring_coordinator.set_loop(asyncio.get_running_loop())
    ring_coordinator.reset()
    called_events = []

    def mock_handler(source, details):
        called_events.append((source, details))

    ring_coordinator.set_on_ring_callback(mock_handler)

    # 1. NETWORK_SNIFF signal registered
    ring_coordinator.register_signal("NETWORK_SNIFF", {"pattern": "A", "bytes": 312})
    await asyncio.sleep(0.05)
    # MUST NOT ring!
    assert len(called_events) == 0

    # 2. SHAZAM_CHIME MUST ring at ms 0
    ring_coordinator.register_signal("SHAZAM_CHIME", {"ncc": 0.88})
    await asyncio.sleep(0.05)
    assert len(called_events) == 1
    assert called_events[0][0] == "SHAZAM_CHIME"


@pytest.mark.asyncio
async def test_v351_passive_differential_byte_learner(tmp_path):
    """Verify differential byte learner isolates candidate bytes during chime and updates status."""
    sniffer = NetworkSniffer()
    now_t = time.time()

    # Baseline background noise packet
    bg_pkt = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "ts_float": now_t - 10.0,
        "src_ip": "192.168.2.31",
        "src_port": 55432,
        "dst_ip": "81.5.191.227",
        "dst_port": 8820,
        "protocol": "TCP",
        "payload_len": 191,
        "total_len": 231,
        "is_syn": False,
        "payload_hex": "aabbccddeeff00112233",
    }
    sniffer._payload_buffer.append(bg_pkt)
    sniffer.background_noise_hashes.add(bg_pkt["payload_hex"])

    # Unique chime burst packet occurring right at chime time
    chime_pkt = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "ts_float": now_t,
        "src_ip": "192.168.2.31",
        "src_port": 55433,
        "dst_ip": "81.5.191.227",
        "dst_port": 8820,
        "protocol": "TCP",
        "payload_len": 240,
        "total_len": 280,
        "is_syn": False,
        "payload_hex": "11223344556677889900aabbccddeeff",
    }
    sniffer._payload_buffer.append(chime_pkt)

    # Trigger Shazam Chime correlation
    sniffer.on_shazam_chime_triggered(now_t)

    # Verify status
    status = sniffer.get_learner_status()
    assert status["enabled"] is True
    assert "Shadow Mode" in status["mode"]
    assert status["background_noise_count"] >= 1
    assert status["buffered_packets_count"] >= 2
    assert status["learned_candidates_count"] >= 1
    assert len(status["recent_candidates"]) >= 1
    assert status["recent_candidates"][-1]["hex_payload"] == chime_pkt["payload_hex"]


@pytest.mark.asyncio
async def test_v351_stuck_recording_sweeper(tmp_path):
    """Verify Stuck-Recording Sweeper fixes valid recordings (>10KB) to READY and purges empty ones."""
    day_dir = get_day_clip_dir()

    # 1. Stuck clip with existing valid MP4 file (> 10KB)
    clip_valid_id = "test-sweeper-valid-12345"
    clip_valid_mp4 = day_dir / f"{clip_valid_id}.mp4"
    # Write 15 KB dummy data
    with open(clip_valid_mp4, "wb") as f:
        f.write(b"\x00" * 15000)

    # 50 seconds old
    old_ts = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    await save_clip(
        clip_id=clip_valid_id,
        call_id=clip_valid_id,
        video_path=str(clip_valid_mp4),
        snapshot_path="",
        status="RECORDING",
        category="motion",
        answered_by="AI: Test",
        duration=0.0,
        file_size=0,
        created_at=old_ts,
    )

    # 2. Stuck clip with missing / 0-byte file
    clip_empty_id = "test-sweeper-empty-67890"
    await save_clip(
        clip_id=clip_empty_id,
        call_id=clip_empty_id,
        video_path="",
        snapshot_path="",
        status="RECORDING",
        category="motion",
        answered_by="AI: Test Empty",
        duration=0.0,
        file_size=0,
        created_at=old_ts,
    )

    # Run sweeper with max_age_seconds=40.0
    swept_count = await sweep_stuck_recordings(max_age_seconds=40.0)
    assert swept_count == 2

    # Verify valid clip updated to READY
    valid_record = await get_clip(clip_valid_id)
    assert valid_record is not None
    assert valid_record["status"] == "READY"
    assert valid_record["file_size"] == 15000

    # Verify empty clip removed
    empty_record = await get_clip(clip_empty_id)
    assert empty_record is None

    # Cleanup
    await delete_clip(clip_valid_id)
    if clip_valid_mp4.exists():
        clip_valid_mp4.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_v351_thumbnail_endpoint_always_200():
    """Verify GET /api/v1/clips/{clip_id}/thumbnail always returns 200 image/jpeg."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/clips/non-existent-clip-uuid-99999/thumbnail")
        assert resp.status_code == 200
        assert "image/jpeg" in resp.headers["content-type"]
        assert len(resp.content) > 0


@pytest.mark.asyncio
async def test_v351_live_player_mse_mode():
    """Verify live player HTML template sets mode='mse,webrtc,mp4' for audio compatibility."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/live/player")
        assert resp.status_code == 200
        assert 'mode="mse,webrtc,mp4"' in resp.text

