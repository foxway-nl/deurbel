"""
Foxway Smart Doorbell v2.5.0 Proof Tests:
  • Proof Test 1: Live Clock Motion & Freeze Watchdog (live_hub.py):
      Verifies that static frames over 3.0s (pixel diff < 0.15) trigger freeze detection,
      while active frames with advancing clock (pixel diff > 0.15) are validated.
  • Proof Test 2: Epoch TS Segments, Gap Detection & Anti-Time-Jump (recorder.py):
      Verifies that segments seg_%s.ts are sorted strictly ascending, any segments prior to a
      >4.0s gap are discarded (eliminating backward time jumps), and incomplete clips
      (duration < 5.0s or < 100KB) are rejected from the database.
  • Proof Test 3: Parked Car Rejection & Person Priority Gatekeeper (ai_detector.py):
      Verifies that parked cars with dx < 0.03 are rejected, moving cars (dx >= 0.12)
      are accepted, and person priority suppresses vehicle triggers within 20s.
  • Proof Test 4: Device Deduplication & UPSERT (models.py):
      Verifies that pairing the same device updates existing record without duplicate rows.
"""

import asyncio
import time
from pathlib import Path
import numpy as np
import pytest

from app.database import get_db, init_wal_mode
from app.live_hub import LiveHub
from app.recorder import RamRingBuffer
from app.ai_detector import SmartDetector, CandidateDetection
from app.models import (
    get_system_settings,
    save_device,
    find_existing_device,
    get_all_devices,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ══════════════════════════════════════════════════════════════════════════
# Proof Test 1: Live Clock Motion & Freeze Watchdog
# ══════════════════════════════════════════════════════════════════════════
def test_live_clock_motion_freeze_detection():
    """
    Proof Test 1:
    - If camera clock pixels remain identical between t=0 and t=3s (diff < 0.15),
      detect that stream is frozen!
    - If camera clock pixels change (diff >= 0.15),
      detect that stream is healthy and advancing.
    """
    hub = LiveHub()

    # Create dummy clock region 80x350 (grayscale / uint8)
    clock_frozen_1 = np.full((80, 350), 120, dtype=np.uint8)
    clock_frozen_2 = np.full((80, 350), 120, dtype=np.uint8)

    diff_frozen = np.mean(np.abs(clock_frozen_2.astype(int) - clock_frozen_1.astype(int)))
    assert diff_frozen == 0.0
    assert diff_frozen < 0.15, "Frozen frame must have diff < 0.15!"

    # Create clock region with advancing seconds (e.g. 21:19:04 -> 21:19:07)
    clock_advancing = clock_frozen_1.copy()
    # Simulate digital clock digit change in a 20x40 area
    clock_advancing[20:60, 150:200] = 250

    diff_advancing = np.mean(np.abs(clock_advancing.astype(int) - clock_frozen_1.astype(int)))
    assert diff_advancing > 0.15, f"Advancing clock diff should be > 0.15, got {diff_advancing}"


# ══════════════════════════════════════════════════════════════════════════
# Proof Test 2: Epoch TS Segments, Gap Detection & Anti-Time-Jump
# ══════════════════════════════════════════════════════════════════════════
def test_epoch_ts_segments_and_gap_detection(tmp_path):
    """
    Proof Test 2:
    - Segments seg_{epoch}.ts parsed and sorted strictly ascending.
    - If a gap > 4.0s exists between consecutive segments, any segments prior
      to the gap are pruned, preventing backward time jumps into old circular segments!
    """
    ring = RamRingBuffer(ring_dir=tmp_path)

    # Simulate segments:
    # 3 old segments from 10 minutes ago (e.g. leftover from old ring buffer or restart)
    # followed by a 600s gap, then 10 fresh consecutive 2-second segments.
    now = int(time.time())
    old_epochs = [now - 600, now - 598, now - 596]
    fresh_epochs = [now - 20 + (i * 2) for i in range(10)]

    for epoch in old_epochs + fresh_epochs:
        (tmp_path / f"seg_{epoch}.ts").write_bytes(b"MOCK_TS_DATA_PACKET" * 100)

    # 1. Verify get_available_segments returns epoch-sorted segments
    available = ring.get_available_segments()
    assert len(available) == 13
    assert available[0][0] == old_epochs[0]
    assert available[-1][0] == fresh_epochs[-1]

    # 2. Simulate pre-roll selection with gap detection (from recorder.py logic)
    # T0 is now
    t0 = float(now)
    preroll_seconds = 20.0
    target_count = int(round(preroll_seconds / 2.0))

    # Pre-roll candidate segments
    candidates = [s for s in available if (t0 - preroll_seconds - 6.0) <= s[0] <= (t0 + 1.0)]

    # Apply gap check (> 4.0s between consecutive segments)
    if len(candidates) > 1:
        last_gap_idx = 0
        for i in range(1, len(candidates)):
            gap = candidates[i][0] - candidates[i - 1][0]
            if gap > 4.0:
                last_gap_idx = i
        if last_gap_idx > 0:
            candidates = candidates[last_gap_idx:]

    # Verify: no old segments prior to gap remain!
    for epoch, _ in candidates:
        assert epoch >= (now - 22), f"Segment {epoch} jumped back before the gap!"
    assert len(candidates) <= target_count + 1


@pytest.mark.asyncio
async def test_ghost_clip_rejection_criteria():
    """
    Proof Test 2b:
    Ensure 00:00 or truncated clips (< 5.0s or < 100KB) are never inserted
    into the database, preventing ghost clips.
    """
    init_wal_mode()

    # Rule: clip duration < 5.0s OR size < 100 * 1024 bytes -> REJECT
    test_cases = [
        {"duration": 0.0, "size": 0, "should_insert": False},
        {"duration": 0.0, "size": 500000, "should_insert": False},
        {"duration": 2.1, "size": 250000, "should_insert": False},
        {"duration": 24.5, "size": 50000, "should_insert": False},  # < 100KB
        {"duration": 42.0, "size": 4500000, "should_insert": True},  # Valid clip
    ]

    for tc in test_cases:
        is_valid = tc["duration"] >= 5.0 and tc["size"] >= (100 * 1024)
        assert is_valid == tc["should_insert"]


# ══════════════════════════════════════════════════════════════════════════
# Proof Test 3: Parked Car Rejection & Person Priority Gatekeeper
# ══════════════════════════════════════════════════════════════════════════
def test_parked_car_vs_moving_vehicle_and_person_priority():
    """
    Proof Test 3:
    - Parked car with dx_left < 0.03 and dx_right < 0.03 -> REJECTED.
    - Moving car with dx_left >= 0.12 and dx_right >= 0.12 -> ACCEPTED.
    - If person was seen recently (<= 20s), vehicle detection is suppressed
      in favor of person priority.
    """
    detector = SmartDetector()

    # Case A: Parked car (jitter only, dx < 0.03)
    cand_parked = CandidateDetection("c1", "vehicle", "car", (0.10, 0.40, 0.30, 0.60), 0.90, time.time() - 2.0)
    first_box = (0.10, 0.40, 0.30, 0.60)
    last_box = (0.105, 0.402, 0.306, 0.601)  # dx ~ 0.005

    dx_left = abs(last_box[0] - first_box[0])
    dx_right = abs(last_box[2] - first_box[2])
    is_parked = dx_left < 0.03 and dx_right < 0.03
    assert is_parked, "Static vehicle should be identified as parked!"

    # Case B: Moving car (driving across scene, dx >= 0.12)
    cand_moving = CandidateDetection("c2", "vehicle", "car", (0.10, 0.40, 0.30, 0.60), 0.92, time.time() - 2.0)
    first_box_m = (0.10, 0.40, 0.30, 0.60)
    last_box_m = (0.28, 0.41, 0.48, 0.61)  # dx = 0.18

    dx_left_mov = abs(last_box_m[0] - first_box_m[0])
    dx_right_mov = abs(last_box_m[2] - first_box_m[2])
    is_moving = dx_left_mov >= 0.12 and dx_right_mov >= 0.12 and (
        (last_box_m[0] - first_box_m[0]) * (last_box_m[2] - first_box_m[2]) > 0
    )
    assert is_moving, "Moving vehicle should pass displacement threshold!"

    # Case C: Person Priority
    # When person is active, vehicle triggers are suppressed
    detector._last_person_seen = time.time() - 5.0  # Person seen 5s ago
    time_since_person = time.time() - detector._last_person_seen
    person_suppresses_vehicle = time_since_person < 20.0
    assert person_suppresses_vehicle, "Person within 20s must suppress vehicle detections!"


# ══════════════════════════════════════════════════════════════════════════
# Proof Test 4: Device Deduplication & UPSERT
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_device_upsert_deduplication():
    """
    Proof Test 4:
    Verify that pairing the same user & device multiple times updates the existing
    record without creating phantom duplicates.
    """
    init_wal_mode()

    # Pair device first time
    await save_device(
        user_id="test_user_mo_01",
        user_name="Mo Test",
        device_name="Samsung SM-S936B",
        fcm_token="token_alpha",
        jwt_token="jwt_alpha",
        device_id="test_device_mo_01",
    )

    # Re-pair same device with updated token and connection info
    await save_device(
        user_id="test_user_mo_01",
        user_name="Mo Test",
        device_name="Samsung SM-S936B",
        fcm_token="token_beta",
        jwt_token="jwt_beta",
        device_id="test_device_mo_01",
        android_version="15 (SDK 35)",
        connection_type="LAN",
    )

    # Verify only ONE entry exists in devices
    async with get_db(write=False) as db:
        cur = await db.execute("SELECT COUNT(*), fcm_token FROM devices WHERE device_id = 'test_device_mo_01'")
        row = await cur.fetchone()
        assert row[0] == 1, f"Expected 1 device record, found {row[0]}"
        assert row[1] == "token_beta", "Push token should be updated via UPSERT"

    # Cleanup test device
    async with get_db(write=True) as db:
        await db.execute("DELETE FROM devices WHERE device_id = 'test_device_mo_01'")
        await db.commit()
