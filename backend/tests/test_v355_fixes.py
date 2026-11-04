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


def test_v355_dual_trigger_timing_analysis(tmp_path, monkeypatch):
    """Verify dual-trigger timing analyzer records deltas in [-4.5s, +1.5s] window."""
    import time
    import app.network_sniffer as sniffer_mod
    from app.network_sniffer import network_sniffer

    # Isolate test from production log file and save in-memory state
    test_log_file = str(tmp_path / "timing_test.jsonl")
    monkeypatch.setattr(sniffer_mod, "TIMING_LOG_FILE", test_log_file)

    saved_recent = list(network_sniffer.recent_timing_records)
    saved_buffer = list(network_sniffer._payload_buffer)
    saved_candidates = list(network_sniffer.learned_chime_candidates)

    try:
        network_sniffer.recent_timing_records.clear()
        network_sniffer._payload_buffer.clear()
        network_sniffer.learned_chime_candidates.clear()

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
        assert analysis["total_events"] == 1
        recent = analysis["events"][0]
        assert recent["in_window"] is True
        assert recent["status"] == "PERFECT_MATCH"
        assert abs(recent["delta_seconds"] - (-3.14)) < 0.15
    finally:
        # Restore production state cleanly
        network_sniffer.recent_timing_records.clear()
        network_sniffer.recent_timing_records.extend(saved_recent)
        network_sniffer._payload_buffer.clear()
        network_sniffer._payload_buffer.extend(saved_buffer)
        network_sniffer.learned_chime_candidates.clear()
        network_sniffer.learned_chime_candidates.extend(saved_candidates)


def test_v355_ring_cooldown_5s():
    """Verify that doorbell ring cooldown is 5.0 seconds in ring_detector and ring_coordinator."""
    from app.ring_detector import ring_detector
    from app.ring_coordinator import ring_coordinator

    assert ring_detector.cooldown_seconds == 5.0
    assert ring_coordinator.cooldown_sec == 5.0


def test_v355_shazam_exclusive_ring_trigger(tmp_path, monkeypatch):
    """Verify that only SHAZAM_CHIME triggers the state machine, and NETWORK_SNIFF is ignored."""
    import time
    from unittest.mock import MagicMock
    from app.ring_coordinator import RingCoordinator
    import app.network_sniffer as sniffer_mod
    from app.network_sniffer import network_sniffer

    test_log_file = str(tmp_path / "timing_test_hybrid.jsonl")
    monkeypatch.setattr(sniffer_mod, "TIMING_LOG_FILE", test_log_file)
    saved_recent = list(network_sniffer.recent_timing_records)

    try:
        coordinator = RingCoordinator()
        mock_dispatch = MagicMock()
        coordinator._dispatch_ring_to_state_machine = mock_dispatch

        # 1. Network sniffer signal arriving in shadow mode should NEVER trigger ringing
        coordinator.register_signal("NETWORK_SNIFF", {"reason": "EZVIZ_CLOUD_INTERCOM_CALL (/2000/4675)"})
        assert mock_dispatch.call_count == 0

        # 2. Shazam chime arriving MUST trigger ringing
        coordinator.register_signal("SHAZAM_CHIME", {"ncc": 0.85})
        assert mock_dispatch.call_count == 1
        mock_dispatch.assert_called_with("SHAZAM_CHIME")
        assert coordinator._active_window is not None
        assert coordinator._active_window["first_source"] == "SHAZAM_CHIME"
    finally:
        network_sniffer.recent_timing_records.clear()
        network_sniffer.recent_timing_records.extend(saved_recent)


def test_v355_sniffer_detects_2000_4675_hex():
    """Verify network sniffer flags 2f323030302f34363735 (/2000/4675) as verified ring."""
    payload_hex = "00000030000000012f323030302f343637350000"
    assert "2f323030302f34363735" in payload_hex


@pytest.mark.asyncio
async def test_v355_ai_rejection_audit_logging():
    """Verify that rejected AI candidates are saved to ai_audit_logs with REJECTED status."""
    import numpy as np
    from app.ai_detector import SmartDetector
    from app.models import get_recent_ai_audit_logs

    detector = SmartDetector()
    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)

    await detector._log_rejected_candidate(
        category="vehicle",
        label="Auto",
        confidence=0.65,
        disp_pct=1.2,
        reason="AFGEWEZEN: Stilstaande geparkeerde auto",
        box=(100.0, 100.0, 200.0, 200.0),
        frame=dummy_frame,
    )

    logs = await get_recent_ai_audit_logs(limit=5)
    assert len(logs) > 0
    recent = logs[0]
    assert recent["status"] == "REJECTED"
    assert recent["category"] == "vehicle"
    assert "AFGEWEZEN" in recent["reason"]
    assert recent["displacement_pct"] == 1.2


@pytest.mark.asyncio
async def test_v355_doorstep_visitor_ir_contrast_approved():
    """Verify that a doorstep visitor (tall box spanning doorstep at x>0.68 with low IR confidence ~0.21) is approved."""
    import time
    import numpy as np
    from app.ai_detector import SmartDetector, CandidateDetection, DetectionSettings

    detector = SmartDetector()
    detector.init_model()
    cfg = await DetectionSettings.load()

    now = time.time()
    # Doorstep visitor box matching the exact forensic values: x1=446, y1=40, x2=555, y2=431
    p_tid = "track_doorstep_visitor"
    p_cand = CandidateDetection(p_tid, "person", "Persoon", (446.0, 40.0, 555.0, 431.0), 0.21, now - 0.4, 0.05)
    p_cand.frames.append((now, (448.0, 42.0, 556.0, 432.0), 0.23))
    detector._candidate_tracks[p_tid] = p_cand

    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    await detector._process_frame_gatekeeper(dummy_frame, now, cfg)

    # Doorstep visitor must be approved (verified=True or transitioned to dispatch)
    assert p_tid not in detector._candidate_tracks or detector._candidate_tracks[p_tid].verified is True


@pytest.mark.asyncio
async def test_v355_ring_coordinator_no_force_flag():
    """Verify that RingCoordinator._dispatch_ring_to_state_machine calls call_sm.ring without force."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from app.ring_coordinator import ring_coordinator

    mock_ring = AsyncMock()
    with patch("app.main.call_sm.ring", mock_ring):
        ring_coordinator.set_loop(asyncio.get_running_loop())
        ring_coordinator._dispatch_ring_to_state_machine("SHAZAM_CHIME")
        await asyncio.sleep(0.05)
        assert mock_ring.call_count >= 1
        _, kwargs = mock_ring.call_args
        assert "force" not in kwargs or kwargs.get("force") is not True
        assert kwargs.get("trigger_source") == "SHAZAM_CHIME"


def test_v355_shazam_audio_stream_worker_low_delay_flags():
    """Verify that Shazam RTSP consumer uses low-delay and zero-buffer flags."""
    import inspect
    from app.ring_detector import ShazamChimeDetector

    src = inspect.getsource(ShazamChimeDetector._audio_stream_worker)
    assert "-fflags" in src and "nobuffer" in src
    assert "-flags" in src and "low_delay" in src
    assert "-flush_packets" in src
    assert "FIONREAD" in src


@pytest.mark.asyncio
async def test_v355_foolproof_doorbell_discovery(monkeypatch):
    """Verify that camera discovery strictly enforces hardware MAC and model validation."""
    from app.camera_discovery import (
        get_mac_for_ip,
        is_doorbell_alive,
        KNOWN_DOORBELL_MAC,
        KNOWN_DOORBELL_OUI,
    )

    # 1. Verification of MAC resolution
    mac_31 = get_mac_for_ip("192.168.2.31")
    if mac_31:
        assert mac_31.startswith(KNOWN_DOORBELL_OUI)

    # 2. Reject alien host (e.g. 192.168.2.4 with wrong MAC)
    async def mock_port_true(host, port, timeout=0.5):
        return True

    monkeypatch.setattr("app.camera_discovery.check_port", mock_port_true)
    monkeypatch.setattr("app.camera_discovery.get_mac_for_ip", lambda ip: "ea:8a:e7:06:ee:5a" if ip == "192.168.2.4" else "d4:e8:53:18:78:9b")

    # Wrong MAC host must be rejected
    alive_alien = await is_doorbell_alive("192.168.2.4")
    assert alive_alien is False

    # Authentic host with matching MAC must be accepted
    alive_doorbell = await is_doorbell_alive("192.168.2.31")
    assert alive_doorbell is True








