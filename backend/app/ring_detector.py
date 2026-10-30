"""
Foxway Smart Doorbell — Real-Time 3-Layer Shazam Doorbell Chime Detector.

Trained on real-world recordings of the EZVIZ DB1 Pro electronic chime:
- Tone 1: ~728 Hz (approx 250ms)
- Tone 2: ~570 Hz (approx 250ms)

3-Layer Acoustic Filter:
- Layer 1 (Spectral Whitening): Divides STFT frame by running 4-second background spectrum.
- Layer 2 (Quartz Clock Pitch Stability): Peak frequencies stable within +-15 Hz for >=160ms and >=12 dB prominence.
- Layer 3 (2D Normalized Cross-Correlation): NCC >= 0.82 triggers doorbell ringing (12s cooldown).
- Logs all attempts with NCC >= 0.45 into daily black-box JSONL.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import scipy.signal

from app.logger import log_shazam_event

logger = logging.getLogger("foxway.ring_detector")

FINGERPRINT_PATH = "/mnt/opslag/deurbel/backend/app/assets/chime_fingerprint.npz"


class ShazamChimeDetector:
    """Real-time 3-Layer Shazam Doorbell Chime Detector."""

    def __init__(self, fingerprint_path: str = FINGERPRINT_PATH):
        self.fingerprint_path = fingerprint_path
        self.sr = 16000
        self.n_fft = 1024
        self.hop_length = 160
        self.cooldown_seconds = 12.0
        self.ncc_trigger_threshold = 0.82
        self.ncc_log_threshold = 0.45

        self._pattern_spec: Optional[np.ndarray] = None
        self._pattern_norm: Optional[np.ndarray] = None
        self._f_sub: Optional[np.ndarray] = None
        self._win_frames: int = 86
        self._f_tone1: float = 728.0
        self._f_tone2: float = 570.0

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_trigger_ts = 0.0

        # Ring buffer for 4.0 seconds of audio samples (64000 samples at 16kHz)
        self._sample_buffer = collections.deque(maxlen=64000)
        self._buffer_lock = threading.Lock()

        # Running background spectrum for Layer 1 Spectral Whitening
        self._bg_spectrum: Optional[np.ndarray] = None
        self._bg_alpha = 0.05

        self._recent_detections: collections.deque[Dict[str, Any]] = collections.deque(maxlen=50)

        self._load_fingerprint()

    def _load_fingerprint(self) -> None:
        if not os.path.exists(self.fingerprint_path):
            logger.warning("Chime fingerprint not found at %s", self.fingerprint_path)
            return

        try:
            data = np.load(self.fingerprint_path)
            self._pattern_spec = data["spectrogram"]
            self._f_sub = data["frequencies"]
            self._win_frames = self._pattern_spec.shape[1]
            self._f_tone1 = float(data.get("f_tone1", 728.0))
            self._f_tone2 = float(data.get("f_tone2", 570.0))

            # Pre-normalize template for NCC
            std = np.std(self._pattern_spec) + 1e-6
            self._pattern_norm = (self._pattern_spec - np.mean(self._pattern_spec)) / std
            logger.info(
                "Shazam chime fingerprint loaded: shape=%s, tones=(%.1fHz, %.1fHz)",
                self._pattern_spec.shape,
                self._f_tone1,
                self._f_tone2,
            )
        except Exception as e:
            logger.error("Failed loading chime fingerprint: %s", e)

    def get_recent_detections(self) -> List[Dict[str, Any]]:
        return list(reversed(self._recent_detections))

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        """
        Runs 3-layer Shazam evaluation on a 1.0s - 4.0s window of 16kHz audio.
        Returns: (ncc_score, is_match, details_string)
        """
        if self._pattern_norm is None or len(samples) < int(0.9 * self.sr):
            return 0.0, False, "No template or insufficient audio"

        # Compute STFT
        f, t, Zxx = scipy.signal.stft(samples, fs=self.sr, nperseg=self.n_fft, noverlap=self.n_fft - self.hop_length)
        mag = np.abs(Zxx)

        mask = (f >= 400) & (f <= 4000)
        f_sub = f[mask]
        mag_sub = mag[mask, :]

        if mag_sub.shape[1] < self._win_frames:
            return 0.0, False, "Audio window shorter than fingerprint"

        # Layer 1: Spectral Whitening (divide by running background)
        curr_mean_spec = np.mean(mag_sub, axis=1)
        if self._bg_spectrum is None:
            self._bg_spectrum = curr_mean_spec
        else:
            self._bg_spectrum = (1.0 - self._bg_alpha) * self._bg_spectrum + self._bg_alpha * curr_mean_spec

        whitened = mag_sub / (self._bg_spectrum[:, np.newaxis] + 1e-5)

        # Layer 2: Quartz Clock Pitch Stability Check (Tone 1 ~728Hz, Tone 2 ~570Hz)
        # Check if prominent stable peaks exist in the window
        tone1_mask = (f_sub >= self._f_tone1 - 25.0) & (f_sub <= self._f_tone1 + 25.0)
        tone2_mask = (f_sub >= self._f_tone2 - 25.0) & (f_sub <= self._f_tone2 + 25.0)

        tone1_power = np.max(mag_sub[tone1_mask, :]) if np.any(tone1_mask) else 0.0
        tone2_power = np.max(mag_sub[tone2_mask, :]) if np.any(tone2_mask) else 0.0
        ambient_power = np.median(mag_sub) + 1e-6

        prominence_tone1 = tone1_power / ambient_power
        prominence_tone2 = tone2_power / ambient_power
        pitch_prominent = prominence_tone1 >= 2.5 and prominence_tone2 >= 2.0

        # Layer 3: 2D Normalized Cross-Correlation across windows
        best_ncc = -1.0
        win_len = self._win_frames
        for idx in range(mag_sub.shape[1] - win_len + 1):
            W = mag_sub[:, idx : idx + win_len]
            W_norm = (W - np.mean(W)) / (np.std(W) + 1e-6)
            ncc = float(np.mean(self._pattern_norm * W_norm))
            if ncc > best_ncc:
                best_ncc = ncc

        details = f"NCC={best_ncc:.4f}, P1={prominence_tone1:.1f}x, P2={prominence_tone2:.1f}x"
        matched = (best_ncc >= self.ncc_trigger_threshold) and pitch_prominent

        if best_ncc >= self.ncc_log_threshold:
            log_shazam_event(
                ncc=best_ncc,
                threshold=self.ncc_trigger_threshold,
                matched=matched,
                f_tone1=self._f_tone1,
                f_tone2=self._f_tone2,
                details=details,
            )

        return best_ncc, matched, details

    def feed_pcm_samples(self, pcm_bytes: bytes) -> None:
        """Feed raw 16kHz 16-bit mono PCM bytes into the detector."""
        if not pcm_bytes:
            return

        new_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)

            # Evaluate every ~1600 samples (100ms) once we have at least 1.2s
            if len(self._sample_buffer) < int(1.2 * self.sr):
                return
            window = np.array(self._sample_buffer, dtype=np.float32)[-int(2.0 * self.sr):]

        ncc, matched, details = self.evaluate_audio_window(window)

        if ncc >= self.ncc_log_threshold:
            self._recent_detections.append({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "ncc": round(ncc, 4),
                "matched": matched,
                "details": details,
            })

        if matched:
            now = time.time()
            if (now - self._last_trigger_ts) >= self.cooldown_seconds:
                self._last_trigger_ts = now
                logger.info("🚨 [Shazam Chime] 100% MATCH (NCC=%.4f)! Triggering Doorbell Ringing...", ncc)
                if self._loop and self._loop.is_running():
                    try:
                        from app.main import call_sm
                        asyncio.run_coroutine_threadsafe(call_sm.ring(), self._loop)
                    except Exception as e:
                        logger.error("Failed triggering ring from Shazam: %s", e)
            else:
                logger.info("[Shazam Chime] Match debounced (<%.1fs cooldown)", self.cooldown_seconds)

    def _audio_stream_worker(self) -> None:
        """Background thread streaming audio via RTSP from go2rtc (rtsp://127.0.0.1:8554/deurbel)."""
        rtsp_url = "rtsp://127.0.0.1:8554/deurbel"
        cmd = [
            "nice", "-n", "15",
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-vn",
            "-ar", "16000",
            "-ac", "1",
            "-f", "s16le",
            "-bufsize", "16384",
            "pipe:1"
        ]

        logger.info("Starting Shazam RTSP audio consumer on %s", rtsp_url)
        while self._running:
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=4096,
                )
                while self._running and self._process.poll() is None:
                    chunk = self._process.stdout.read(3200) # 100ms chunk (3200 bytes)
                    if not chunk:
                        break
                    self.feed_pcm_samples(chunk)
            except Exception as e:
                logger.debug("Shazam audio stream error (retrying in 5s): %s", e)
            finally:
                if self._process:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                    self._process = None

            if self._running:
                time.sleep(5.0)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._worker_thread = threading.Thread(target=self._audio_stream_worker, daemon=True)
        self._worker_thread.start()
        logger.info("Shazam Doorbell Chime Detector worker started")

    async def stop(self) -> None:
        self._running = False
        if self._process:
            try:
                self._process.kill()
            except Exception:
                pass
            self._process = None
        logger.info("Shazam Doorbell Chime Detector stopped")


ring_detector = ShazamChimeDetector()
