"""
Foxway Smart Doorbell — Pure FastAPI Software Ring Detector.

Replaces legacy Asterisk/SIP dependencies with native FastAPI event dispatching:
1. Continuous 728 Hz Gong / Chime Acoustic Detector on RTSP stream audio.
   - Monitors RTSP stream audio (16kHz mono PCM).
   - Fast Fourier Transform (FFT) isolates the distinct 728 Hz chime signature.
   - Triggers `call_sm.ring()` directly within the FastAPI process.
2. 15-second debounce protection against rapid double-presses.
3. Zero VoIP/SIP overhead: triggers direct WebSocket push + FCM notification + auto HD recording.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Optional

import numpy as np

from app.config import settings

logger = logging.getLogger("foxway.ring_detector")

# Tone 1 (Ding - start tone around 728 Hz)
TONE1_FREQ_MIN = 710
TONE1_FREQ_MAX = 750

# Tone 2 (Dong - following lower or complementary tone around 560-620 Hz or 800-880 Hz)
TONE2_LOW_MIN = 560
TONE2_LOW_MAX = 620
TONE2_HIGH_MIN = 800
TONE2_HIGH_MAX = 880

# General noise reference band outside chime frequencies
NOISE_REF_MIN = 350
NOISE_REF_MAX = 500

MIN_BELL_VOLUME = 500000
MIN_SNR_RATIO = 2.5
DEBOUNCE_SECONDS = 15.0

# Tone sequence timing window (seconds between Ding and Dong)
TONE_SEQ_MIN_DELAY = 0.20  # Minimum 200ms
TONE_SEQ_MAX_DELAY = 0.90  # Maximum 900ms


class DoorbellRingDetector:
    """Supervised ring detector monitoring RTSP audio for chime signature."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_ring_ts = 0.0
        self._cooldown = 0.0
        self._tone1_detected_ts = 0.0
        self._tone1_peak = 0.0

    async def start(self) -> None:
        """Start the background acoustic chime detector."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("Software Doorbell Ring Detector (Dual-Tone Sequential FFT) started")

    async def stop(self) -> None:
        """Stop detector."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Software Doorbell Ring Detector stopped")

    def process_samples(self, audio_data: np.ndarray, now: float) -> bool:
        """
        Analyze audio window with sequential dual-tone validation.
        Returns True if a valid Ding-Dong sequence triggered a ring.
        """
        if len(audio_data) < 500:
            return False

        windowed_data = audio_data * np.hanning(len(audio_data))
        fft_data = np.fft.rfft(windowed_data)
        fft_freq = np.fft.rfftfreq(len(windowed_data), 1 / 16000)
        fft_mag = np.abs(fft_data)

        noise_mask = (fft_freq >= NOISE_REF_MIN) & (fft_freq <= NOISE_REF_MAX)
        noise_avg = float(np.mean(fft_mag[noise_mask])) + 1.0 if np.any(noise_mask) else 1.0

        t1_mask = (fft_freq >= TONE1_FREQ_MIN) & (fft_freq <= TONE1_FREQ_MAX)
        t1_peak = float(np.max(fft_mag[t1_mask])) if np.any(t1_mask) else 0.0
        t1_snr = t1_peak / noise_avg

        t2_mask = (
            ((fft_freq >= TONE2_LOW_MIN) & (fft_freq <= TONE2_LOW_MAX))
            | ((fft_freq >= TONE2_HIGH_MIN) & (fft_freq <= TONE2_HIGH_MAX))
        )
        t2_peak = float(np.max(fft_mag[t2_mask])) if np.any(t2_mask) else 0.0
        t2_snr = t2_peak / noise_avg

        global_mean = float(np.mean(fft_mag)) + 1.0
        t1_sharpness = t1_peak / global_mean
        t2_sharpness = t2_peak / global_mean

        # 1. Expire stale Tone 1 if older than TONE_SEQ_MAX_DELAY
        if self._tone1_detected_ts > 0.0 and (now - self._tone1_detected_ts) > TONE_SEQ_MAX_DELAY:
            logger.debug("Tone 1 expired without Tone 2 (delta=%.2fs)", now - self._tone1_detected_ts)
            self._tone1_detected_ts = 0.0
            self._tone1_peak = 0.0

        # 2. Check for Tone 1 (Ding ~728 Hz)
        # Must be loud, sharp peak (not broad noise/scream), and dominant over Tone 2
        if t1_peak >= MIN_BELL_VOLUME and t1_sharpness >= 4.0 and (t1_peak / (t2_peak + 1.0)) >= 1.8:
            if self._tone1_detected_ts == 0.0:
                self._tone1_detected_ts = now
                self._tone1_peak = t1_peak
                logger.info(
                    "🔔 Tone 1 (Ding ~728Hz) detected (Vol: %.0f | Sharpness: %.1fx). Awaiting Tone 2 within 0.2-0.9s...",
                    t1_peak, t1_sharpness,
                )
            return False

        # 3. Check for Tone 2 (Dong ~580 Hz) following Tone 1 within 0.2s - 0.9s
        # Must be sharp peak and dominant over Tone 1 (Ding has faded)
        if self._tone1_detected_ts > 0.0:
            time_delta = now - self._tone1_detected_ts
            if TONE_SEQ_MIN_DELAY <= time_delta <= TONE_SEQ_MAX_DELAY:
                if t2_peak >= (MIN_BELL_VOLUME * 0.5) and t2_sharpness >= 3.5 and (t2_peak / (t1_peak + 1.0)) >= 1.3:
                    if now > self._cooldown:
                        self._cooldown = now + DEBOUNCE_SECONDS
                        logger.info(
                            "🚨 DUAL-TONE CHIME VERIFIED! Tone 1 (%.0f) → Tone 2 (%.0f) Δt=%.2fs → Ringing!",
                            self._tone1_peak, t2_peak, time_delta,
                        )
                        self._tone1_detected_ts = 0.0
                        self._tone1_peak = 0.0
                        return True
                    else:
                        logger.debug("Ring ignored due to active debounce cooldown")
                        self._tone1_detected_ts = 0.0
                        self._tone1_peak = 0.0

        return False

    async def _listen_loop(self) -> None:
        """Continuous listener loop with auto-reconnect."""
        chunk_size = 2000  # 125ms of 16kHz audio
        rtsp_url = settings.GO2RTC_RTSP_URL

        while self._running:
            proc: Optional[subprocess.Popen] = None
            try:
                cmd = [
                    "nice", "-n", "15",
                    "ffmpeg", "-y", "-nostdin",
                    "-fflags", "nobuffer", "-flags", "low_delay",
                    "-rtsp_transport", "tcp", "-i", rtsp_url,
                    "-vn",
                    "-f", "s16le", "-ac", "1", "-ar", "16000",
                    "-loglevel", "quiet", "-"
                ]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=4096)

                while self._running:
                    raw_audio = await asyncio.to_thread(proc.stdout.read, chunk_size * 2)
                    if not raw_audio or len(raw_audio) < chunk_size * 2:
                        logger.warning("RTSP audio stream closed or ended, reconnecting in 3s...")
                        break

                    audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                    now = time.time()
                    triggered = self.process_samples(audio_data, now)
                    if triggered:
                        from app.main import call_sm
                        asyncio.create_task(call_sm.ring())

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in ring detector loop: %s", e)
            finally:
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                await asyncio.sleep(3.0)


ring_detector = DoorbellRingDetector()

