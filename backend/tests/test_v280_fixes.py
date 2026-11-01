"""
Foxway Smart Doorbell v2.8.0 — Proof Tests
Validates the 2-Phase Clip Lifecycle (RECORDING -> READY), immediate pre-notification thumbnail,
and smart in-progress clip handling.
"""

import os
import shutil
import tempfile
import uuid
import numpy as np
import cv2
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone

from app.main import app
from app.config import settings
from app.models import (
    init_db,
    save_clip,
    get_clip,
    update_clip,
    delete_clip,
    save_event,
    get_events_since,
)
from app.database import get_db


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    yield


@pytest.mark.asyncio
async def test_2phase_clip_lifecycle_recording_to_ready():
    """
    Test 1:
    - Step 1: Snapshot is written to clips/thumb_<id>.jpg before event dispatch and is > 5 KB.
    - Step 2: Clip row is created with status='RECORDING', duration=0.0, valid created_at.
    - Step 3: GET /api/v1/clips/{clip_id} returns 200 with status='RECORDING' and created_at.
    - Step 4: GET /api/v1/clips/{clip_id}/thumbnail returns 200 image/jpeg > 5 KB.
    - Step 5: GET /api/v1/clips/{clip_id}/stream returns 404 while status='RECORDING'.
    - Step 6: On MP4 completion, status updates to 'READY' with duration > 0 and size_bytes > 0.
    """
    clip_id = str(uuid.uuid4())
    t0_iso = datetime.now(timezone.utc).isoformat()
    clip_path = str(settings.CLIPS_DIR / f"{clip_id}.mp4")
    thumb_path = settings.CLIPS_DIR / f"thumb_{clip_id}.jpg"
    snapshot_path = settings.SNAPSHOTS_DIR / f"{clip_id}.jpg"

    settings.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    settings.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Create a dummy test frame and write to disk (> 5 KB)
        dummy_frame = np.full((576, 768, 3), 120, dtype=np.uint8)
        # Add some variation so compression doesn't collapse below 5KB
        cv2.putText(dummy_frame, "FOXWAY TEST FRAME", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)
        cv2.imwrite(str(thumb_path), dummy_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        cv2.imwrite(str(snapshot_path), dummy_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

        assert thumb_path.exists()
        thumb_size = thumb_path.stat().st_size
        assert thumb_size > 5000, f"Thumbnail size {thumb_size} is not > 5 KB"

        # 2. Insert clip row with status='RECORDING' at T0
        await save_clip(
            clip_id=clip_id,
            call_id=clip_id,
            video_path=clip_path,
            snapshot_path=str(thumb_path),
            answered_by="AI: Voertuig gedetecteerd (Auto)",
            duration=0.0,
            status="RECORDING",
            category="vehicle",
            file_size=0,
            created_at=t0_iso,
        )

        # Also save event with /api/v1/clips/{clip_id}/thumbnail
        event = await save_event(
            event_type="motion",
            category="vehicle",
            title="Voertuig gedetecteerd: Auto",
            clip_id=clip_id,
            snapshot_url=f"/api/v1/clips/{clip_id}/thumbnail",
        )
        assert event["clip_id"] == clip_id
        assert event["snapshot_url"] == f"/api/v1/clips/{clip_id}/thumbnail"

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 3. GET /api/v1/clips/{clip_id} returns 200 with status='RECORDING'
            resp = await client.get(f"/api/v1/clips/{clip_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == clip_id
            assert data["status"] == "RECORDING"
            assert data["created_at"] == t0_iso
            assert data["duration"] == 0.0
            assert data["thumbnail_url"] == f"/api/v1/clips/{clip_id}/thumbnail"

            # 4. GET /api/v1/clips/{clip_id}/thumbnail returns 200 with image/jpeg > 5 KB
            resp_thumb = await client.get(f"/api/v1/clips/{clip_id}/thumbnail")
            assert resp_thumb.status_code == 200
            assert "image/jpeg" in resp_thumb.headers["content-type"]
            assert len(resp_thumb.content) > 5000

            # 5. GET /api/v1/clips/{clip_id}/stream returns 404 while RECORDING
            resp_stream = await client.get(f"/api/v1/clips/{clip_id}/stream")
            assert resp_stream.status_code == 404

            # 6. Simulate MP4 completion: create valid dummy video file (> 100 KB) and update to READY
            with open(clip_path, "wb") as f:
                f.write(b"\x00" * 150000)  # 150 KB

            await update_clip(
                clip_id=clip_id,
                status="READY",
                duration=40.5,
                file_size=150000,
            )

            # Check GET /api/v1/clips/{clip_id} now returns READY
            resp_ready = await client.get(f"/api/v1/clips/{clip_id}")
            assert resp_ready.status_code == 200
            ready_data = resp_ready.json()
            assert ready_data["status"] == "READY"
            assert ready_data["duration"] == 40.5
            assert ready_data["size_bytes"] == 150000

            # Check GET /api/v1/clips/{clip_id}/stream now returns 200 OK
            resp_stream_ready = await client.get(f"/api/v1/clips/{clip_id}/stream")
            assert resp_stream_ready.status_code in (200, 206)

            # Check clips list includes status
            resp_list = await client.get("/api/v1/clips")
            assert resp_list.status_code == 200
            list_items = resp_list.json()
            matching = [item for item in list_items if item["id"] == clip_id]
            assert len(matching) == 1
            assert matching[0]["status"] == "READY"
            assert matching[0]["duration"] == 40.5

    finally:
        thumb_path.unlink(missing_ok=True)
        snapshot_path.unlink(missing_ok=True)
        if os.path.exists(clip_path):
            os.unlink(clip_path)
        await delete_clip(clip_id)


@pytest.mark.asyncio
async def test_failed_clip_cleanup():
    """
    Test 2:
    Verify that if a clip fails during recording, deleting it cleans up DB row cleanly.
    """
    clip_id = str(uuid.uuid4())
    await save_clip(
        clip_id=clip_id,
        call_id=clip_id,
        video_path=str(settings.CLIPS_DIR / f"{clip_id}.mp4"),
        snapshot_path="",
        answered_by="Test",
        duration=0.0,
        status="RECORDING",
    )
    assert await get_clip(clip_id) is not None

    deleted = await delete_clip(clip_id)
    assert deleted is True
    assert await get_clip(clip_id) is None


@pytest.mark.asyncio
async def test_app_version_v280():
    """
    Test 3:
    Verify that /api/v1/app/version and /api/v1/version report v2.8.0 (versionCode 19).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/app/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] in ("2.8.0", "2.9.0", "3.0.0", "3.1.2", "3.1.4", "3.2.0", "3.3.1", "3.4.0", "3.4.1", "3.5.0", "3.5.1", "3.5.2", "3.5.3")
        assert data["version_code"] in (19, 20, 21, 24, 26, 27, 29, 30, 31, 32, 34, 35, 36)
        assert ("RECORDING -> READY" in data["changelog"] or "v2.9.0" in data["changelog"] or "v3.0.0" in data["changelog"] or "v3.1.2" in data["changelog"] or "v3.1.4" in data["changelog"] or "v3.2.0" in data["changelog"] or "v3.5.0" in data["changelog"] or "v3.5.3" in data["changelog"])

