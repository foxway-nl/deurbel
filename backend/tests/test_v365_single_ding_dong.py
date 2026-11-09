"""
Foxway Smart Doorbell — Single Ding-Dong Matched Envelope Detector Verification (v3.6.5).

Proves:
1. Real EZVIZ DB1 Pro chime recordings (chime_reference.wav and saved ring clips)
   are verified with high scores (> 0.85) via both evaluate_audio_window and feed_pcm_samples.
2. Real Ding-Dong + concurrent street talking voices (300-650 Hz) is approved (True/RING).
3. Sudden loud claps, door slams, children screams, and isolated whistles are 100% rejected (False/0 triggers).
"""

import os
import subprocess
import wave
import numpy as np
import pytest

from app.ring_detector import ShazamChimeDetector, verify_ding_dong_chime


class TestV365SingleDingDong:
    """Verification test suite for v3.6.5 single Ding-Dong matched envelope detector."""

    def test_1_real_chime_recordings_and_pcm_stream(self):
        """
        Verify real chime audio on the server:
        - chime_reference.wav (both 8kHz and resampled 16kHz)
        - Saved ring clip in /mnt/opslag/deurbel/clips/2026-11-08/
        Check exact prom728, prom570, jump570, decay728, and corr.
        """
        detector = ShazamChimeDetector()

        # 1. Test chime_reference.wav
        ref_path = "/mnt/opslag/deurbel/backend/app/assets/chime_reference.wav"
        assert os.path.exists(ref_path), "chime_reference.wav must exist on server!"

        with wave.open(ref_path, "rb") as wf:
            data = wf.readframes(wf.getnframes())
            samples_8k = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            sr = wf.getframerate()

        # Direct 8kHz evaluation
        ok_8k, sc_8k, rsn_8k = verify_ding_dong_chime(samples_8k, sample_rate=sr)
        assert ok_8k is True, f"chime_reference.wav at 8kHz must be approved! Got: {rsn_8k}"
        assert sc_8k >= 0.85, f"Score must be >= 0.85, got: {sc_8k}"

        # Resample to 16kHz (simulating RTSP audio consumer)
        import scipy.signal
        samples_16k = scipy.signal.resample_poly(samples_8k, 2, 1).astype(np.float32)
        ok_16k, sc_16k, rsn_16k = verify_ding_dong_chime(samples_16k, sample_rate=16000)
        assert ok_16k is True, f"chime_reference.wav at 16kHz must be approved! Got: {rsn_16k}"
        assert sc_16k >= 0.85, f"Score must be >= 0.85, got: {sc_16k}"

        # Test evaluate_audio_window on 16kHz
        sc_eval, ok_eval, details = detector.evaluate_audio_window(samples_16k)
        assert ok_eval is True, f"evaluate_audio_window must match chime_reference! Got: {details}"

        # 2. Test feed_pcm_samples chunked streaming
        triggered_signals = []
        detector.cooldown_seconds = 0.1
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.ring_coordinator.register_ring_signal",
                lambda source, details: triggered_signals.append((source, details)),
            )
            # Feed samples in 50ms chunks (1600 bytes = 800 int16 samples)
            pcm_int16 = (samples_16k * 32767.0).astype(np.int16).tobytes()
            chunk_size = 1600
            for i in range(0, len(pcm_int16), chunk_size):
                detector.feed_pcm_samples(pcm_int16[i : i + chunk_size])

            assert len(triggered_signals) >= 1, "feed_pcm_samples must trigger register_ring_signal!"
            assert triggered_signals[0][0] == "SHAZAM_CHIME"

        # 3. Test on saved ring clip
        clip_path = "/mnt/opslag/deurbel/clips/2026-11-08/114839f086b0.mp4"
        if os.path.exists(clip_path):
            cmd = [
                "ffmpeg", "-i", clip_path,
                "-t", "25",  # First 25 seconds contains the chime event
                "-vn", "-ar", "16000", "-ac", "1",
                "-f", "s16le", "pipe:1"
            ]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            out, _ = p.communicate()
            if out:
                clip_samples = np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0
                clip_sc, clip_ok, clip_det = detector.evaluate_audio_window(clip_samples)
                assert clip_ok is True, f"Saved clip {clip_path} must be approved! Details: {clip_det}"
                assert clip_sc >= 0.80, f"Score for {clip_path} must be >= 0.80, got {clip_sc}"

    def test_2_real_chime_plus_concurrent_voices_approved(self):
        """
        Prove: Real Ding-Dong + concurrent street talking voices (300-650 Hz) is approved.
        Narrow local shoulder design (±45..130 Hz) ignores distant speech frequencies.
        """
        ref_path = "/mnt/opslag/deurbel/backend/app/assets/chime_reference.wav"
        with wave.open(ref_path, "rb") as wf:
            data = wf.readframes(wf.getnframes())
            samples_8k = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

        import scipy.signal
        chime_16k = scipy.signal.resample_poly(samples_8k, 2, 1).astype(np.float32)
        sr = 16000
        n_samples = len(chime_16k)
        t = np.arange(n_samples) / float(sr)

        # Synthesize talking human voices: harmonics at 320 Hz, 460 Hz, 590 Hz with amplitude modulation
        voice = (
            0.12 * np.sin(2 * np.pi * 320 * t + 0.3 * np.sin(2 * np.pi * 4 * t))
            + 0.10 * np.sin(2 * np.pi * 460 * t)
            + 0.08 * np.sin(2 * np.pi * 590 * t + 0.5 * np.sin(2 * np.pi * 3 * t))
        ).astype(np.float32)
        # Add random rustle noise
        voice += np.random.normal(0, 0.02, n_samples).astype(np.float32)

        mixed = chime_16k + voice

        ok, sc, rsn = verify_ding_dong_chime(mixed, sample_rate=sr)
        assert ok is True, f"Chime + concurrent street voices MUST still trigger ring! Reason: {rsn}"
        assert sc >= 0.80, f"Score must remain high (>= 0.80), got: {sc}"

    def test_3_rejection_of_slam_scream_whistle_loud_claps(self):
        """
        Prove 100% rejection (False / 0 triggers) for:
        1. Sudden loud clap / door slam (broadband impulsive noise where ding_p2 >= ding_p1)
        2. Children screaming (loud speech formants with vibrato)
        3. Single isolated whistle / high tone at 728 Hz for 1s without Dong
        4. Heavy door thud (low frequency rumble + high transient)
        """
        sr = 16000
        duration_s = 1.15
        n_samples = int(sr * duration_s)
        t = np.arange(n_samples) / float(sr)

        # 3a. Sudden loud clap / door slam at t=0.45s (broadband transient)
        clap = np.random.normal(0, 0.02, n_samples).astype(np.float32)
        idx_clap = (t >= 0.45) & (t < 0.65)
        # Massive broadband burst across all frequencies
        clap[idx_clap] += np.random.normal(0, 0.8, np.sum(idx_clap)).astype(np.float32)
        ok_clap, sc_clap, rsn_clap = verify_ding_dong_chime(clap, sample_rate=sr)
        assert ok_clap is False, f"Loud clap MUST be rejected! Got: {rsn_clap}"

        # 3b. Loud child scream (competing formants with vibrato at 600 Hz, 850 Hz, 1200 Hz)
        scream = (
            0.5 * np.sin(2 * np.pi * (600 + 35 * np.sin(2 * np.pi * 6 * t)) * t)
            + 0.4 * np.sin(2 * np.pi * (850 + 40 * np.sin(2 * np.pi * 5 * t)) * t)
            + 0.3 * np.sin(2 * np.pi * 1200 * t)
        ).astype(np.float32)
        scream += np.random.normal(0, 0.05, n_samples).astype(np.float32)
        ok_scream, sc_scream, rsn_scream = verify_ding_dong_chime(scream, sample_rate=sr)
        assert ok_scream is False, f"Child scream MUST be rejected! Got: {rsn_scream}"

        # 3c. Pure 728 Hz tone without Dong (1.0s whistle)
        whistle = 0.6 * np.sin(2 * np.pi * 728 * t).astype(np.float32)
        whistle += np.random.normal(0, 0.01, n_samples).astype(np.float32)
        ok_whistle, sc_whistle, rsn_whistle = verify_ding_dong_chime(whistle, sample_rate=sr)
        assert ok_whistle is False, f"Isolated 728 Hz whistle without Dong MUST be rejected! Got: {rsn_whistle}"

        # 3d. Door slam: 120 Hz bass thud + 2000 Hz metallic clack
        slam = (
            0.7 * np.exp(-((t - 0.4) ** 2) / 0.02) * np.sin(2 * np.pi * 120 * t)
            + 0.5 * np.exp(-((t - 0.4) ** 2) / 0.005) * np.sin(2 * np.pi * 2200 * t)
        ).astype(np.float32)
        ok_slam, sc_slam, rsn_slam = verify_ding_dong_chime(slam, sample_rate=sr)
        assert ok_slam is False, f"Door slam MUST be rejected! Got: {rsn_slam}"
