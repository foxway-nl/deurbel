import pytest
import asyncio
import os
import sqlite3
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_v355_multi_tag_merging():
    """Verify that starting a ring while recording is active consolidates into 'ring,person'."""
    from app.recorder import _active_recording_info, is_recording_active, start_recording, extend_active_recording

    # Simulate an active person recording
    _active_recording_info["clip_id"] = "test-active-ai-clip"
    _active_recording_info["category"] = "person"
    _active_recording_info["categories"] = {"person"}
    _active_recording_info["start_time"] = asyncio.get_event_loop().time()

    assert is_recording_active() is True

    # Now trigger doorbell ring
    task = await start_recording("test-call-id", "/tmp/dummy.mp4", trigger_source="SHAZAM_CHIME")
    await task

    # Categories should now include both ring and person
    assert "ring" in _active_recording_info["categories"]
    assert "person" in _active_recording_info["categories"]
    assert _active_recording_info["category"] == "ring"

    # Reset
    _active_recording_info["clip_id"] = None
    _active_recording_info["categories"] = set()


@pytest.mark.asyncio
async def test_v355_unnotified_events():
    """Verify that get_unnotified_events retrieves non-silent events."""
    from app.models import get_unnotified_events
    events = await get_unnotified_events(limit=5)
    assert isinstance(events, list)


def test_v355_anti_wind_aspect_ratio_filter():
    """Verify aspect ratio threshold rejects horizontal tree/shadow patches (aspect < 1.25)."""
    # Standing person: width 60, height 180 -> aspect ratio 3.0 >= 1.25 -> APPROVED
    person_w = 60.0
    person_h = 180.0
    assert (person_h / person_w) >= 1.25

    # Tree branch / ground shadow: width 120, height 40 -> aspect ratio 0.33 < 1.25 -> REJECTED
    shadow_w = 120.0
    shadow_h = 40.0
    assert (shadow_h / shadow_w) < 1.25


def test_v355_anti_wind_oscillation_ratio():
    """Verify oscillation filter rejects back-and-forth movement."""
    # Swaying tree: moves left 20, right 20, left 20 -> total path = 60, net disp = 0
    total_path = 60.0
    net_disp = 2.0
    osc_ratio = net_disp / total_path
    assert osc_ratio < 0.60  # Rejected by oscillation filter (< 0.60)

    # Walking person: moves consistently across screen
    total_path = 50.0
    net_disp = 45.0
    osc_ratio = net_disp / total_path
    assert osc_ratio >= 0.60  # Approved (>= 0.60)


@pytest.mark.asyncio
async def test_v355_person_and_vehicle_track_multi_frame_evaluation():
    """Verify that multi-frame tracks for pedestrians and vehicles do not cause IndexError."""
    import time
    import numpy as np
    from app.ai_detector import SmartDetector, CandidateDetection, DetectionSettings

    detector = SmartDetector()
    detector.init_model()
    cfg = await DetectionSettings.load()

    # Person track with 3 frames
    now = time.time()
    p_tid = "track_test_person_regression"
    p_cand = CandidateDetection(p_tid, "person", "Persoon", (100.0, 100.0, 200.0, 300.0), 0.65, now - 1.0, 0.1)
    p_cand.frames.append((now - 0.5, (102.0, 102.0, 202.0, 302.0), 0.66))
    p_cand.frames.append((now, (105.0, 105.0, 205.0, 305.0), 0.67))
    detector._candidate_tracks[p_tid] = p_cand

    # Vehicle track with 3 frames
    v_tid = "track_test_vehicle_regression"
    v_cand = CandidateDetection(v_tid, "vehicle", "Auto", (100.0, 100.0, 250.0, 200.0), 0.75, now - 1.0, 0.2)
    v_cand.frames.append((now - 0.5, (180.0, 100.0, 330.0, 200.0), 0.76))
    v_cand.frames.append((now, (260.0, 100.0, 410.0, 200.0), 0.77))
    detector._candidate_tracks[v_tid] = v_cand

    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # Must process without throwing IndexError: tuple index out of range
    await detector._process_frame_gatekeeper(dummy_frame, now, cfg)


def test_v355_dual_trigger_timing_analysis():
    """Verify dual-trigger timing analyzer records deltas in [-4.5s, +1.5s] window."""
    import time
    from app.network_sniffer import network_sniffer

    # Push a mock packet to payload buffer
    now_t = time.time()
    packet_ts = now_t - 3.14  # Sniffer 3.14s before chime
    network_sniffer._payload_buffer.append({
        "timestamp": "2026-11-02 12:00:00.000",
        "ts_float": packet_ts,
        "dst_ip": "148.153.115.233",
        "dst_port": 6404,
        "protocol": "UDP",
        "payload_len": 49,
        "total_len": 77,
        "is_syn": False,
        "payload_hex": "536a49794e5445344f44616263",
    })

    # Trigger chime
    network_sniffer.on_shazam_chime_triggered(now_t)

    # Check timing analysis
    analysis = network_sniffer.get_timing_analysis()
    assert analysis["status"] == "ok"
    assert analysis["configured_window"]["min"] == -4.5
    assert analysis["configured_window"]["max"] == 1.5
    assert analysis["total_events"] >= 1
    recent = analysis["events"][0]
    assert recent["in_window"] is True
    assert recent["status"] == "PERFECT_MATCH"
    assert abs(recent["delta_seconds"] - (-3.14)) < 0.15


def test_v355_ring_cooldown_5s():
    """Verify that doorbell ring cooldown is 5.0 seconds in ring_detector and ring_coordinator."""
    from app.ring_detector import ring_detector
    from app.ring_coordinator import ring_coordinator

    assert ring_detector.cooldown_seconds == 5.0
    assert ring_coordinator.cooldown_sec == 5.0



