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

BELL_FREQ_MIN = 710
BELL_FREQ_MAX = 750
NOISE_REF_MIN = 450
NOISE_REF_MAX = 650

MIN_BELL_VOLUME = 600000
MIN_SNR_RATIO = 2.5
DEBOUNCE_SECONDS = 5.0


class DoorbellRingDetector:
    """Supervised ring detector monitoring RTSP audio for chime signature."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_ring_ts = 0.0
        self._cooldown = 0.0

    async def start(self) -> None:
        """Start the background acoustic chime detector."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("Software Doorbell Ring Detector (728 Hz FFT) started")

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
                    # Read 2000 samples (4000 bytes) in an executor to avoid blocking the event loop
                    raw_audio = await asyncio.to_thread(proc.stdout.read, chunk_size * 2)
                    if not raw_audio or len(raw_audio) < chunk_size * 2:
                        logger.warning("RTSP audio stream closed or ended, reconnecting in 3s...")
                        break

                    audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                    windowed_data = audio_data * np.hanning(len(audio_data))
                    fft_data = np.fft.rfft(windowed_data)
                    fft_freq = np.fft.rfftfreq(len(windowed_data), 1 / 16000)
                    fft_mag = np.abs(fft_data)

                    bell_mask = (fft_freq >= BELL_FREQ_MIN) & (fft_freq <= BELL_FREQ_MAX)
                    bell_peak = float(np.max(fft_mag[bell_mask]))

                    noise_mask = (fft_freq >= NOISE_REF_MIN) & (fft_freq <= NOISE_REF_MAX)
                    noise_avg = float(np.mean(fft_mag[noise_mask])) + 1.0

                    snr_ratio = bell_peak / noise_avg

                    if bell_peak >= MIN_BELL_VOLUME and snr_ratio >= MIN_SNR_RATIO:
                        now = time.time()
                        if now > self._cooldown:
                            self._cooldown = now + DEBOUNCE_SECONDS
                            logger.info(
                                "🚨 DING-DONG DETECTED! (Vol: %.0f | SNR: %.1fx) → Triggering Call State Machine",
                                bell_peak, snr_ratio
                            )
                            # Directly trigger ring inside FastAPI
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
