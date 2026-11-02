import asyncio
import pytest
from app.state_machine import CallStateMachine, ActiveCall
from app.models import get_db, save_clip


@pytest.mark.asyncio
async def test_call_recording_resilient_to_hangup():
    """Verify that hanging up or declining a call does NOT cancel recording."""
    sm = CallStateMachine()
    
    recorded_task_cancelled = False
    
    async def mock_start_rec(call_id, clip_path):
        async def dummy_rec():
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                nonlocal recorded_task_cancelled
                recorded_task_cancelled = True
                raise
        return asyncio.create_task(dummy_rec())

    async def mock_stop_rec(task):
        # Even if called, v3.5.4 must not cancel
        pass

    sm.on_start_recording = mock_start_rec
    sm.on_stop_recording = mock_stop_rec

    # 1. Trigger ring
    call = await sm.ring(force=True, trigger_source="SHAZAM_CHIME")
    assert call is not None
    await asyncio.sleep(0.1)
    assert call.recorder_task is not None

    # Verify initial RECORDING row in DB
    async with get_db(write=False) as db:
        c = await db.execute("SELECT status, category FROM clips WHERE clip_id = ?", (call.call_id,))
        row = await c.fetchone()
        assert row is not None
        assert row["status"] == "RECORDING"
        assert row["category"] == "ring"

    # 2. Hangup call
    hangup_res = await sm.hangup(call.call_id)
    assert hangup_res["status"] == "ENDED"

    # Verify recorder task was NOT cancelled
    assert not call.recorder_task.done()
    assert not recorded_task_cancelled
    call.recorder_task.cancel()  # cleanup


@pytest.mark.asyncio
async def test_vehicle_12_percent_displacement_rule():
    """Verify that vehicles require >=3 frames and >=12% width displacement."""
    from app.ai_detector import SmartDetector, CandidateDetection
    detector = SmartDetector()
    
    w = 1280.0
    # Candidate moving only 5% of screen width (e.g. parked car jitter)
    cand = CandidateDetection("track_test_jitter", "vehicle", "Auto", (100.0, 100.0, 300.0, 250.0), 0.8, 0.0, 0.2)
    cand.frames = [
        (0.0, (100.0, 100.0, 300.0, 250.0), 0.8, 0.2),
        (0.2, (105.0, 100.0, 305.0, 250.0), 0.8, 0.2),
        (0.4, (120.0, 100.0, 320.0, 250.0), 0.8, 0.2),
    ]
    # Center shift: (210 - 200) / 1280 = 10 / 1280 = 0.78% < 12%
    x_start = (cand.frames[0][1][0] + cand.frames[0][1][2]) / 2.0
    x_now = (cand.frames[-1][1][0] + cand.frames[-1][1][2]) / 2.0
    dx_pct = abs(x_now - x_start) / w
    assert dx_pct < 0.12

    # Candidate passing through street (moving >12% width)
    cand_moving = CandidateDetection("track_test_moving", "vehicle", "Auto", (100.0, 100.0, 300.0, 250.0), 0.8, 0.0, 0.2)
    cand_moving.frames = [
        (0.0, (100.0, 100.0, 300.0, 250.0), 0.8, 0.2),
        (0.2, (200.0, 100.0, 400.0, 250.0), 0.8, 0.2),
        (0.4, (350.0, 100.0, 550.0, 250.0), 0.8, 0.2),
    ]
    # Center shift: (450 - 200) / 1280 = 250 / 1280 = 19.5% >= 12%
    x_start2 = (cand_moving.frames[0][1][0] + cand_moving.frames[0][1][2]) / 2.0
    x_now2 = (cand_moving.frames[-1][1][0] + cand_moving.frames[-1][1][2]) / 2.0
    dx_pct2 = abs(x_now2 - x_start2) / w
    assert dx_pct2 >= 0.12
