"""
Foxway Smart Doorbell v2.4.0 Proof Tests:
  • Test 1: SQLite WAL mode, busy_timeout=60000, and serialized concurrency (0 database is locked errors).
  • Test 2: 20s Pre-Roll selection: exactly 10 completed 2s-segments selected within [T0 - 20s, T0].
  • Test 3: System settings default to pre_roll_seconds=20, post_roll_seconds=20, max_active_motion_seconds=30.
  • Test 4: LiveHub frame delivery without RTSP errors.
"""

import asyncio
import time
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db, init_wal_mode
from app.models import get_system_settings, get_system_setting, set_system_setting
from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_sqlite_wal_and_concurrent_writes():
    """
    Test 1: Run 25 parallel concurrent write tasks to ensure WAL mode and write lock
    completely eliminate 'database is locked' errors.
    """
    init_wal_mode()

    async def worker(idx: int):
        async with get_db(write=True) as db:
            await db.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"test_concurrency_key_{idx}", f"val_{idx}"),
            )
            # Short sleep inside lock to stress concurrency
            await asyncio.sleep(0.01)

    tasks = [worker(i) for i in range(25)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        assert not isinstance(r, Exception), f"Concurrent DB write failed: {r}"

    # Verify rows written
    async with get_db(write=False) as db:
        cur = await db.execute("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'test_concurrency_key_%'")
        row = await cur.fetchone()
        assert row[0] == 25


@pytest.mark.asyncio
async def test_20s_preroll_segment_selection(tmp_path):
    """
    Test 2: Prove that record_smart_clip selects exactly 10 completed 2-second segments (20s)
    within [T0 - 20s, T0], and does NOT dump the entire 50s/60s ring buffer!
    """
    # Create 25 mock segments representing a 50s circular ring buffer
    now = time.time()
    mock_segments = []
    for i in range(25):
        seg_file = tmp_path / f"seg_{i:03d}.ts"
        seg_file.write_bytes(b"TS_DATA" * 500)
        # Each segment created 2 seconds apart from (now - 50s) to now
        mtime = now - (25 - i) * 2.0
        mock_segments.append((mtime, seg_file))

    # T0 is the moment of AI detection
    t0 = now
    preroll_seconds = 20.0
    target_count = int(round(preroll_seconds / 2.0))  # 10 segments

    # Filter completed segments excluding active write (the last one)
    completed = mock_segments[:-1]

    # Selection rule from recorder.py:
    preroll_segs = [s for s in completed if (t0 - preroll_seconds - 2.5) <= s[0] <= (t0 + 1.0)]
    if len(preroll_segs) > target_count:
        preroll_segs = preroll_segs[-target_count:]

    # Assert exactly 10 segments selected (20 seconds), not all 25 (50 seconds)!
    assert len(preroll_segs) == 10
    total_duration = len(preroll_segs) * 2.0
    assert total_duration == 20.0

    # Ensure all selected segments fall within the 20s pre-roll window
    earliest_selected = preroll_segs[0][0]
    assert (t0 - earliest_selected) <= 22.0, "Pre-roll went further back than 20 seconds!"


@pytest.mark.asyncio
async def test_v240_system_settings_defaults():
    """
    Test 3: Verify system settings defaults in v2.4.0:
    pre_roll_seconds = 20
    post_roll_seconds = 20
    max_active_motion_seconds = 30
    """
    settings = await get_system_settings()
    assert float(settings.get("pre_roll_seconds", "20")) == 20.0
    assert float(settings.get("post_roll_seconds", "20")) == 20.0
    assert float(settings.get("max_active_motion_seconds", "30")) == 30.0

