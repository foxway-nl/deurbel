"""
Foxway Smart Doorbell — Ultra-Strict Ring Verification Test Suite (v3.6.2).

Proves:
1. Simulated screaming child / voice (728->570 Hz with harmonics at 360, 650, 1100 Hz and pitch-vibrato)
   accompanied by simultaneous cloud network burst (NETWORK_SNIFF) results in 0 calls (False).
2. Pure electronic 728 Hz (220ms) -> 570 Hz (260ms) chime triggers ring directly (True).
3. RingCoordinator strictly rejects NETWORK_SNIFF and passive telemetry from ever initiating a call.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from app.ring_coordinator import RingCoordinator, ring_coordinator
from app.ring_detector import ShazamChimeDetector, ring_detector, verify_ding_dong_chime


@pytest.mark.asyncio
class TestV362StrictRing:
    """Test suite proving strict rejection of screaming voices & sniffer coincidence."""

    async def test_1_children_screaming_with_harmonics_and_cloud_burst_rejected(self):
        """
        Synthesizes a screaming child / voice:
        Contains energy at 728 Hz and 570 Hz, but with realistic human vocal formants
        at 360 Hz, 650 Hz, and 1100 Hz, plus pitch-vibrato (FM).
        Accompanied by concurrent cloud network burst.
        Asserts verify_ding_dong_chime == False and call_sm.ring is NEVER called (0 calls).
        """
        sr = 16000
        dur = 1.5
        t = np.arange(int(sr * dur)) / float(sr)

        idx_ding = (t >= 0.20) & (t < 0.42)
        idx_dong = (t >= 0.42) & (t < 0.68)

        # Voice audio with harmonics & vibrato
        voice = np.random.normal(0, 0.02, len(t)).astype(np.float32)
        vibrato_ding = 15.0 * np.sin(2 * np.pi * 6.0 * t[idx_ding])
        vibrato_dong = 15.0 * np.sin(2 * np.pi * 6.0 * t[idx_dong])

        voice[idx_ding] += (
            np.sin(2 * np.pi * (728.0 + vibrato_ding) * t[idx_ding]) * 0.5
            + np.sin(2 * np.pi * 360.0 * t[idx_ding]) * 0.45
            + np.sin(2 * np.pi * 650.0 * t[idx_ding]) * 0.40
            + np.sin(2 * np.pi * 1100.0 * t[idx_ding]) * 0.35
        ).astype(np.float32)

        voice[idx_dong] += (
            np.sin(2 * np.pi * (570.0 + vibrato_dong) * t[idx_dong]) * 0.5
            + np.sin(2 * np.pi * 360.0 * t[idx_dong]) * 0.45
            + np.sin(2 * np.pi * 650.0 * t[idx_dong]) * 0.40
            + np.sin(2 * np.pi * 1100.0 * t[idx_dong]) * 0.35
        ).astype(np.float32)

        # 1. Verification gate MUST reject voice
        is_ring, score, reason = verify_ding_dong_chime(voice, sample_rate=sr)
        assert is_ring is False, f"Child scream with vocal harmonics MUST be rejected! (score={score}, reason={reason})"

        # 2. End-to-end detector & coordinator check
        detector = ShazamChimeDetector()
        coordinator = RingCoordinator()
        coordinator.set_loop(asyncio.get_running_loop())

        mock_ring = AsyncMock()
        coordinator.set_on_ring_callback(mock_ring)

        # Feed the voice into detector and simulate simultaneous cloud packet burst
        coordinator.register_cloud_burst({"dst_port": 8820, "bytes": 450, "protocol": "TCP"})
        coordinator.register_signal("NETWORK_SNIFF", {"bytes": 450})

        # Process PCM
        pcm_bytes = (voice * 32767.0).astype(np.int16).tobytes()
        detector.feed_pcm_samples(pcm_bytes)

        await asyncio.sleep(0.05)
        assert mock_ring.call_count == 0, "NETWORK_SNIFF + voice MUST NEVER trigger a call!"

    async def test_2_pure_electronic_ding_dong_triggers_immediately(self):
        """
        Synthesizes a clean electronic Ding-Dong chime:
        728 Hz (220ms) -> 570 Hz (260ms), laser-sharp frequency, no competing formants.
        Asserts verify_ding_dong_chime == True and score >= 0.75.
        """
        sr = 16000
        dur = 1.5
        t = np.arange(int(sr * dur)) / float(sr)

        clean_chime = np.random.normal(0, 0.01, len(t)).astype(np.float32)
        idx_ding = (t >= 0.20) & (t < 0.42)  # 220ms
        clean_chime[idx_ding] += np.sin(2 * np.pi * 728.0 * t[idx_ding]).astype(np.float32) * 0.8
        idx_dong = (t >= 0.42) & (t < 0.68)  # 260ms
        clean_chime[idx_dong] += np.sin(2 * np.pi * 570.0 * t[idx_dong]).astype(np.float32) * 0.8

        is_ring, score, reason = verify_ding_dong_chime(clean_chime, sample_rate=sr)
        assert is_ring is True, f"Pure electronic Ding-Dong chime MUST trigger! (reason={reason})"
        assert score >= 0.75, f"Score must be >= 0.75, got {score}"

    async def test_3_coordinator_strict_source_enforcement(self):
        """
        Proves that RingCoordinator strictly accepts ONLY SHAZAM_CHIME, TEST_BUTTON, SHELLY_WEBHOOK.
        NETWORK_SNIFF, MOTION_SNIFF, UNKNOWN, etc. are 100% ignored.
        """
        coordinator = RingCoordinator()
        mock_ring = AsyncMock()
        coordinator.set_on_ring_callback(mock_ring)

        # These MUST be ignored
        coordinator.register_signal("NETWORK_SNIFF", {"bytes": 800})
        coordinator.register_signal("MOTION_SNIFF", {})
        coordinator.register_signal("UNKNOWN", {})
        coordinator.register_signal("AI_MOTION", {})
        await asyncio.sleep(0.05)
        assert mock_ring.call_count == 0

        # TEST_BUTTON triggers
        coordinator.register_signal("TEST_BUTTON", {})
        await asyncio.sleep(0.05)
        assert mock_ring.call_count == 1

        # SHELLY_WEBHOOK triggers after cooldown
        coordinator.reset()
        mock_ring.reset_mock()
        coordinator.set_on_ring_callback(mock_ring)
        coordinator.register_signal("SHELLY_WEBHOOK", {})
        await asyncio.sleep(0.05)
        assert mock_ring.call_count == 1

        # SHAZAM_CHIME triggers
        coordinator.reset()
        mock_ring.reset_mock()
        coordinator.set_on_ring_callback(mock_ring)
        coordinator.register_signal("SHAZAM_CHIME", {"ncc": 0.85})
        await asyncio.sleep(0.05)
        assert mock_ring.call_count == 1

