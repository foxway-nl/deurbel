"""
Foxway Smart Doorbell — EZVIZ :8820 MQTT Deciphering & 50-Triggers Benchmark Verification (v3.6.6).

Verifies:
1. test_1_decode_doorbell_packet_mqtt_and_burst:
   - Verifies MQTT PUBLISH on /2000/4103 parsing.
   - Verifies 205B and 336B payloads trigger SIG_A_MQTT_EVENT.
   - Verifies LAN :8000 alarm payload triggers LAN_8000_ALARM.
2. test_2_joint_shazam_and_sniffer_match_records_trial:
   - Sniffer trigger at T=0 and Shazam chime at T=0.280s.
   - Asserts SQLite table ring_comparison_trials contains BOTH_MATCHED row with delta_ms = -280.0.
3. test_3_shazam_primary_mode_ignores_standalone_sniffer:
   - SHAZAM_PRIMARY mode ignores standalone sniffer from ringing phone.
   - Resolves to SNIFFER_ONLY trial after window.
4. test_4_admin_endpoints_and_mode_switching:
   - Tests GET /api/v1/admin/ring-comparison-benchmark.
   - Tests POST /api/v1/admin/ring-trigger-mode.
"""

import asyncio
import time
from unittest.mock import AsyncMock
import pytest

from app.network_sniffer import decode_doorbell_packet
from app.ring_coordinator import RingCoordinator, ring_coordinator
from app.models import (
    get_ring_comparison_summary,
    get_recent_comparison_trials,
)
from app.admin import create_session_token, SESSION_COOKIE_NAME


class TestV366FiftyTrials:
    """Test suite for v3.6.6 MQTT decoding, benchmark engine, and mode switching."""

    def test_1_decode_doorbell_packet_mqtt_and_burst(self):
        """
        Verify MQTT 3.1.1 protocol decoding on TCP :8820 and LAN :8000 alarms.
        """
        # 1. Standard keepalive packet: 191 bytes, /2000/4103
        keepalive_hex = "31bc01000a2f323030302f34313033" + "9a8a00" + "00" * (191 - 18)
        keepalive_bytes = bytes.fromhex(keepalive_hex)
        res_ka = decode_doorbell_packet("TCP", 45000, 8820, keepalive_bytes)
        assert res_ka["is_mqtt_publish"] is True
        assert res_ka["topic"] == "/2000/4103"
        assert res_ka["bytes_len"] == 191
        assert res_ka["is_ring_payload"] is False

        # 2. Ring event Signature A (205 bytes)
        ring_205_hex = "31ca01000a2f323030302f34313033" + "aabbcc" + "00" * (205 - 18)
        ring_205_bytes = bytes.fromhex(ring_205_hex)
        res_205 = decode_doorbell_packet("TCP", 45000, 8820, ring_205_bytes)
        assert res_205["is_mqtt_publish"] is True
        assert res_205["bytes_len"] == 205
        assert res_205["is_ring_payload"] is True
        assert res_205["signature"] == "SIG_A_MQTT_EVENT"

        # 3. Ring event Signature A (336 bytes)
        ring_336_hex = "31cd02000a2f323030302f34313033" + "ddeeff" + "00" * (336 - 18)
        ring_336_bytes = bytes.fromhex(ring_336_hex)
        res_336 = decode_doorbell_packet("TCP", 45000, 8820, ring_336_bytes)
        assert res_336["is_mqtt_publish"] is True
        assert res_336["bytes_len"] == 336
        assert res_336["is_ring_payload"] is True
        assert res_336["signature"] == "SIG_A_MQTT_EVENT"

        # 4. LAN :8000 alarm packet (0x1100 intercom alarm)
        lan_alarm = b"\x00\x00\x11\x00" + b"\x00" * 40
        res_lan = decode_doorbell_packet("TCP", 50000, 8000, lan_alarm)
        assert res_lan["is_ring_payload"] is True
        assert res_lan["signature"] == "LAN_8000_ALARM"

    @pytest.mark.asyncio
    async def test_2_joint_shazam_and_sniffer_match_records_trial(self):
        """
        Simulate sniffer at T=0, Shazam chime at T=0.280s.
        Verify BOTH_MATCHED trial recorded with delta_ms = -280.0, faster_method = 'SNIFFER'.
        """
        loop = asyncio.get_running_loop()
        coordinator = RingCoordinator(verification_window_sec=3.5)
        coordinator.set_loop(loop)
        mock_ring = AsyncMock()
        coordinator.set_on_ring_callback(mock_ring)

        t0 = time.time()
        # 1. Sniffer trigger arrives at t0
        sniffer_ev = {
            "timestamp": t0,
            "signature": "SIG_A_MQTT_EVENT",
            "bytes_len": 205,
            "hex_preview": "31ca01000a2f323030302f34313033",
            "topic": "/2000/4103",
        }
        coordinator.register_sniffer_trigger(sniffer_ev)

        # 2. Shazam chime arrives 280ms later
        coordinator.register_signal(
            source="SHAZAM_CHIME",
            details={
                "ncc": 0.94,
                "freq": 728.0,
                "reason": "DING_DONG_VERIFIED",
                "timestamp": t0 + 0.280,
            },
        )

        # Allow async event loop to process
        await asyncio.sleep(0.08)

        # Check DB
        trials = await get_recent_comparison_trials(limit=10)
        assert len(trials) >= 1
        latest = trials[0]
        assert latest["result"] == "BOTH_MATCHED"
        assert latest["shazam_score"] == pytest.approx(0.94, abs=0.01)
        assert latest["sniffer_sig"] == "SIG_A_MQTT_EVENT"
        assert latest["delta_ms"] == pytest.approx(-280.0, abs=15.0)
        assert latest["faster_method"] == "SNIFFER"

        summary = await get_ring_comparison_summary()
        assert summary["joint_matches"] >= 1
        assert summary["sniffer_faster_count"] >= 1

    @pytest.mark.asyncio
    async def test_3_shazam_primary_mode_ignores_standalone_sniffer(self):
        """
        In SHAZAM_PRIMARY mode, a standalone sniffer packet NEVER triggers call_sm.ring(),
        and resolves after window to SNIFFER_ONLY.
        """
        loop = asyncio.get_running_loop()
        coordinator = RingCoordinator(verification_window_sec=0.2)  # fast window for test
        coordinator.set_loop(loop)
        coordinator.set_mode("SHAZAM_PRIMARY")
        mock_ring = AsyncMock()
        coordinator.set_on_ring_callback(mock_ring)

        sniffer_ev = {
            "timestamp": time.time(),
            "signature": "SIG_B_MQTT_BURST",
            "bytes_len": 693,
            "hex_preview": "31bc01000a2f323030302f34313033",
            "topic": "/2000/4103",
        }
        coordinator.register_sniffer_trigger(sniffer_ev)

        # Immediately assert mock_ring was NOT called
        mock_ring.assert_not_called()

        # Wait for timeout
        await asyncio.sleep(0.3)

        # Still NOT called
        mock_ring.assert_not_called()

        # Check DB trial recorded as SNIFFER_ONLY
        trials = await get_recent_comparison_trials(limit=10)
        assert len(trials) >= 1
        latest = trials[0]
        assert latest["result"] == "SNIFFER_ONLY"
        assert latest["faster_method"] in ("NONE", "N/A")

    @pytest.mark.asyncio
    async def test_4_admin_endpoints_and_mode_switching(self, async_client):
        """
        Test GET /api/v1/admin/ring-comparison-benchmark and POST /api/v1/admin/ring-trigger-mode.
        """
        token = create_session_token("admin")
        cookies = {SESSION_COOKIE_NAME: token}

        # 1. GET benchmark
        resp = await async_client.get("/api/v1/admin/ring-comparison-benchmark", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "success")
        assert "summary" in data
        assert data["summary"]["target"] == 50
        assert "trials" in data
        assert "current_mode" in data

        # 2. Switch mode to SNIFFER_PRIMARY
        resp_mode = await async_client.post(
            "/api/v1/admin/ring-trigger-mode",
            json={"mode": "SNIFFER_PRIMARY"},
            cookies=cookies,
        )
        assert resp_mode.status_code == 200
        assert resp_mode.json()["mode"] == "SNIFFER_PRIMARY"
        assert ring_coordinator.mode == "SNIFFER_PRIMARY"

        # Verify reflected in benchmark
        resp_after = await async_client.get("/api/v1/admin/ring-comparison-benchmark", cookies=cookies)
        assert resp_after.json()["current_mode"] == "SNIFFER_PRIMARY"

        # 3. Switch back to SHAZAM_PRIMARY
        resp_back = await async_client.post(
            "/api/v1/admin/ring-trigger-mode",
            json={"mode": "SHAZAM_PRIMARY"},
            cookies=cookies,
        )
        assert resp_back.status_code == 200
        assert ring_coordinator.mode == "SHAZAM_PRIMARY"
