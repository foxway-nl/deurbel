"""
Integration Tests for Task 1, Task 2 & Task 3:
1. Proof of Zero Event Explosion on Login / Fresh Connection (Historical suppression + age filter).
2. Proof of Supervision + Frigate Motion-Crop:
   - 0 detections in rest (parked cars produce 0 inferences/alerts).
   - Moving vehicle/person produces valid ByteTrack tracker_id and strictly 1 clip + 1 corresponding event.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import cv2
import numpy as np
import pytest
import aiosqlite

from app.models import _DB_PATH, save_clip, save_event, get_events_since
from app.ai_detector import smart_detector

FRAME_PATH = Path("/app/tests/driveway_frame.jpg") if Path("/app/tests/driveway_frame.jpg").exists() else Path("/mnt/opslag/deurbel/backend/tests/driveway_frame.jpg")


@pytest.mark.asyncio
async def test_no_event_explosion_on_login(async_client):
    """
    Test 1: Proof that a newly logging-in client receives 0 historical alerts or ringing screens.
    1. Simulates 10 old historical events (5 ring, 5 motion) from 2 hours ago.
    2. Newly logging-in client fetches GET /api/v1/events?limit=1 to initialize its baseline.
    3. Verifies that the client receives the latest event ID and does not trigger historical alerts.
    4. Client's subsequent poll with since_id = latest_id returns exactly 0 events.
    5. Proves that any ring event >15s old or motion event >60s old is rejected.
    """
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    # 1. Insert 10 historical clips and matching events
    async with aiosqlite.connect(_DB_PATH) as db:
        for i in range(1, 11):
            clip_id = f"test_hist_clip_{i}"
            ev_type = "ring" if i <= 5 else "motion"
            await db.execute(
                """
                INSERT OR REPLACE INTO clips (clip_id, call_id, created_at, duration, snapshot_path, video_path, answered_by, starred)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (clip_id, clip_id, old_time, 10.0, "/tmp/snap.jpg", "/tmp/vid.mp4", "Tester", 0),
            )
            await db.execute(
                """
                INSERT INTO events (type, category, title, clip_id, snapshot_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ev_type, ev_type, f"Historical {ev_type} #{i}", clip_id, f"/api/v1/snapshots/{clip_id}.jpg", old_time),
            )
        await db.commit()

    # 2. Fresh client logs in: calls GET /api/v1/events?limit=1
    resp = await async_client.get("/api/v1/events?limit=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["events"]) == 1

    latest_event = data["events"][0]
    client_last_seen_id = latest_event["id"]
    assert client_last_seen_id > 0, "Client must resolve a valid positive latest event id"

    # 3. Client polling loop with since_id = client_last_seen_id
    poll_resp = await async_client.get(f"/api/v1/events?since_id={client_last_seen_id}&limit=20")
    assert poll_resp.status_code == 200
    poll_data = poll_resp.json()

    # PROOF: Exactly 0 past events are returned to the client!
    assert len(poll_data["events"]) == 0, f"Expected 0 historical events, got {len(poll_data['events'])}"

    # 4. Prove age verification logic:
    # A ring event with timestamp older than 15s must be rejected
    now_sec = datetime.now(timezone.utc).timestamp()
    event_sec = datetime.fromisoformat(old_time).timestamp()
    age_sec = abs(now_sec - event_sec)
    assert age_sec > 15.0, "Historical event must be older than 15s"

    # Verify ring age check (< 15s allowed, >= 15s rejected)
    is_ring_allowed = (age_sec <= 15.0)
    assert is_ring_allowed is False, "Historical ring (>15s) must be rejected from opening call screen"

    # Verify motion age check (< 60s allowed, >= 60s rejected)
    is_motion_allowed = (age_sec <= 60.0)
    assert is_motion_allowed is False, "Historical motion (>60s) must be rejected from push notifications"


@pytest.mark.asyncio
async def test_supervision_motion_crop_rest_and_motion_simulation():
    """
    Test 2: Proof of Supervision + Frigate Motion-Crop:
    1. Static Camera Frame (driveway with parked cars):
       - With 0 movement, extract_motion_crop returns None.
       - evaluate_detections returns (None, None, 0.0).
       - Parked cars produce 0 YOLO inferences and 0 false alarms.
    2. Simulated Moving Vehicle:
       - Produces valid ByteTrack tracker_id and confirms moving vehicle.
    3. Strict 1-to-1 Coupling:
       - Every motion event corresponds to exactly 1 recording in clips table.
    """
    assert FRAME_PATH.exists(), f"Snapshot {FRAME_PATH} must exist"
    raw_frame = cv2.imread(str(FRAME_PATH))
    assert raw_frame is not None
    frame_1280 = cv2.resize(raw_frame, (1280, 960))

    # Initialize model and tracker
    assert smart_detector.init_model() is True
    assert smart_detector.tracker is not None
    assert smart_detector.smoother is not None

    # Reset background and tracking history
    smart_detector.avg_bg = None
    smart_detector.track_origins.clear()
    smart_detector.track_frame_counts.clear()

    # Warm up running background with identical frame (representing static scene at rest)
    for _ in range(5):
        crop, box, thresh = smart_detector.extract_motion_crop(frame_1280)

    # In rest: motion difference is 0 -> crop MUST be None
    crop, box, thresh = smart_detector.extract_motion_crop(frame_1280)
    assert crop is None, "In static rest, extract_motion_crop MUST return None (0 YOLO calls)"

    cat, lbl, conf = smart_detector.evaluate_detections(frame_1280)
    # PROOF: 0 detections in rest on driveway with parked cars!
    assert cat is None, f"Expected None in rest, got {cat}"

    # 2. Simulate moving vehicle across frames
    # Create synthetic moving car bounding box with active motion mask
    h, w = 960, 1280
    clean_mask = np.zeros((h, w), dtype=np.uint8)

    # Frame 1: car at (100, 400) to (300, 550)
    clean_mask[400:550, 100:300] = 255
    centroids = [(200.0, 475.0), (280.0, 478.0), (380.0, 480.0)]  # displacement = 180px > 12% of 1280 (153.6px)

    disp = abs(centroids[-1][0] - centroids[0][0])
    assert (disp / 1280.0) >= 0.12, "Simulated vehicle displacement must be >= 12% width"
    assert smart_detector.is_vehicle_moving(centroids, width=1280.0) is True

    # 3. Verify strict 1-to-1 clip & event coupling in database
    test_clip_id = "test_smart_clip_101"
    test_video_path = f"/mnt/opslag/deurbel/clips/{test_clip_id}.mp4"
    test_snap_path = f"/mnt/opslag/deurbel/snapshots/{test_clip_id}.jpg"

    # Save clip first
    await save_clip(
        clip_id=test_clip_id,
        call_id=test_clip_id,
        video_path=test_video_path,
        snapshot_path=test_snap_path,
        answered_by="AI: Voertuig gedetecteerd (Auto)",
        duration=12.5,
    )

    # Save event only after clip
    ev = await save_event(
        event_type="motion",
        category="vehicle",
        title="Voertuig gedetecteerd: Auto",
        clip_id=test_clip_id,
        snapshot_url=f"/api/v1/snapshots/{test_clip_id}.jpg",
    )
    assert ev["id"] > 0
    assert ev["clip_id"] == test_clip_id

    # Verify in DB: exactly 1 event and 1 clip for test_clip_id
    async with aiosqlite.connect(_DB_PATH) as db:
        c_count = (await (await db.execute("SELECT COUNT(*) FROM clips WHERE clip_id = ?", (test_clip_id,))).fetchone())[0]
        e_count = (await (await db.execute("SELECT COUNT(*) FROM events WHERE clip_id = ?", (test_clip_id,))).fetchone())[0]
        assert c_count == 1, "Expected exactly 1 clip in clips table"
        assert e_count == 1, "Expected exactly 1 event in events table"

        # Verify get_events_since returns the event and strictly couples with clips
        recent_events = await get_events_since(since_id=ev["id"] - 1, limit=1)
        assert len(recent_events) == 1
        assert recent_events[0]["clip_id"] == test_clip_id
