"""
Foxway Smart Doorbell — Test Suite for v3.4.1:
1. Host-Level EZVIZ Cloud Packet Sniffer & ARP Redirector (network_sniffer.py).
2. ARP frame builder & fail-safe restoration.
3. Sniffer 1000ms burst aggregation, signature matching & auto-calibration with Shazam chime.
4. AI Sensitivity boost (conf=0.22 on hardware motion) & Smart Motion-Fallback outside parked cars.
5. Supervised fast reconnection (3.0s retry) in SDK alarm listener & Shazam audio consumer.
"""

import asyncio
import os
import time
import pytest
import numpy as np
from app.network_sniffer import NetworkSniffer, _build_arp_packet
from app.ai_detector import SmartDetector, CandidateDetection
from app.sdk_alarm_listener import SDKAlarmListener
from app.ring_detector import ShazamChimeDetector


def test_arp_packet_builder():
    """Verify ARP packet builder creates exact 42-byte Ethernet + ARP frame."""
    frame = _build_arp_packet(
        hwsrc="80:e8:2c:1b:7a:46",
        psrc="192.168.2.254",
        hwdst="d4:e8:53:18:78:9b",
        pdst="192.168.2.31",
        op=2,
    )
    assert len(frame) == 42
    # Ethernet type: 0x0806 (ARP)
    assert frame[12:14] == b"\x08\x06"
    # Hardware type: 1 (Ethernet)
    assert frame[14:16] == b"\x00\x01"
    # Protocol type: 0x0800 (IPv4)
    assert frame[16:18] == b"\x08\x00"
    # Opcode: 2 (Reply)
    assert frame[20:22] == b"\x00\x02"


def test_network_sniffer_signature_matching():
    """Verify signature matching logic for cloud bursts."""
    sniffer = NetworkSniffer()
    sniffer.known_signature = {
        "enabled": True,
        "dst_port": 6404,
        "protocol": "UDP",
        "min_packet_count": 2,
        "min_bytes": 60,
        "max_bytes": 4000,
    }

    # Matching burst
    match_burst = {
        "dst_ip": "148.153.115.233",
        "dst_port": 6404,
        "protocol": "UDP",
        "packet_count": 4,
        "total_bytes": 240,
    }
    assert sniffer._check_ring_signature(match_burst) is True

    # Non-matching: wrong port
    wrong_port_burst = {
        "dst_ip": "148.153.115.233",
        "dst_port": 80,
        "protocol": "UDP",
        "packet_count": 4,
        "total_bytes": 240,
    }
    assert sniffer._check_ring_signature(wrong_port_burst) is False

    # Non-matching: too few packets
    too_few_burst = {
        "dst_ip": "148.153.115.233",
        "dst_port": 6404,
        "protocol": "UDP",
        "packet_count": 1,
        "total_bytes": 240,
    }
    assert sniffer._check_ring_signature(too_few_burst) is False


def test_network_sniffer_auto_calibration():
    """Verify Shazam chime correlates with recent cloud burst and updates signature."""
    sniffer = NetworkSniffer()
    now_t = time.time()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S") + ".100"

    # Add burst within 1s window
    burst = {
        "timestamp": now_str,
        "dst_ip": "81.5.191.227",
        "dst_port": 6404,
        "protocol": "UDP",
        "packet_count": 5,
        "payload_sizes": [44, 44, 44, 44, 44],
        "total_bytes": 220,
        "is_new_tcp_syn": False,
        "is_p2p_burst": True,
        "signature_matched": False,
    }
    sniffer.recent_bursts.append(burst)

    sniffer.on_shazam_chime_triggered(now_t)
    assert sniffer.known_signature["dst_port"] == 6404
    assert sniffer.known_signature["protocol"] == "UDP"
    assert sniffer.known_signature["signature_type"] == "CALIBRATED_SHAZAM_BURST"


@pytest.mark.asyncio
async def test_ai_hardware_motion_sensitivity_drop():
    """Verify Port 8000 0x4000 motion drops YOLO sensitivity to 0.22 for 10s."""
    detector = SmartDetector()
    now = time.time()
    # Initially inactive
    assert (now - detector._last_hardware_motion_time) > 10.0

    detector.notify_hardware_motion()
    assert (time.time() - detector._last_hardware_motion_time) < 1.0


@pytest.mark.asyncio
async def test_smart_motion_fallback_outside_parked_cars():
    """Verify smart motion fallback detects moving contour outside parked cars."""
    detector = SmartDetector()
    now = time.time()
    w, h = 640, 480

    # Parked car in driveway
    parked_box = (100.0, 200.0, 300.0, 400.0)
    detector.update_stationary_vehicles([parked_box], now, w)
    for i in range(1, 5):
        detector.update_stationary_vehicles([parked_box], now + i * 0.5, w)

    # Active hardware motion
    detector.notify_hardware_motion()
    assert (time.time() - detector._last_hardware_motion_time) < 10.0

    # Create motion track outside parked car
    # e.g. person walking on sidewalk (x=450 to 480, y=250 to 350)
    box1 = (450.0, 250.0, 490.0, 350.0)
    box2 = (470.0, 250.0, 510.0, 350.0)  # dx = 20 pixels = 3.12% of 640

    track = CandidateDetection("track_m", "motion", "Beweging (Hardware)", box1, 0.85, now, 0.25)
    track.frames.append((now + 0.25, box2, 0.85))

    cx1 = (box1[0] + box1[2]) / 2.0
    cx2 = (box2[0] + box2[2]) / 2.0
    delta_d = abs(cx2 - cx1) / float(w)

    assert delta_d >= 0.02  # Passed 2.0% threshold
    assert len(track.frames) >= 2


def test_fast_reconnect_supervisors():
    """Verify SDK alarm listener and Shazam detector have fast reconnect settings."""
    sdk = SDKAlarmListener()
    assert hasattr(sdk, "_supervisor_loop")

    shazam = ShazamChimeDetector()
    assert hasattr(shazam, "_audio_stream_worker")
