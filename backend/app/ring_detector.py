"""Foxway Smart Doorbell — Pure 2D Shazam Fingerprint Detector (v3.7.1)."""
from __future__ import annotations
import asyncio
import collections
import datetime
import logging
import os
import subprocess
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import scipy.signal
from app.logger import log_shazam_event

logger = logging.getLogger("foxway.ring_detector")
FINGERPRINT_PATH = "/mnt/opslag/deurbel/backend/app/assets/chime_fingerprint.npz"

class ShazamChimeDetector:
    def __init__(self, fingerprint_path: str = FINGERPRINT_PATH):
        self.fingerprint_path = fingerprint_path
        self.sr = 16000
        self.cooldown_seconds = 6.0
        self.ncc_trigger_threshold = 0.72

        self._template_norm: Optional[np.ndarray] = None
        self._template_bins = 0
        self._win_frames = 0
        self._nperseg = 1024
        self._noverlap = 1024 - 160

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_trigger_ts = 0.0

        self._sample_buffer: Deque[float] = collections.deque(maxlen=32000)
        self._buffer_lock = threading.Lock()
        self._chunks_since_last_eval = 0
        self._recent_detections: Deque[Dict[str, Any]] = collections.deque(maxlen=50)
        self._load_fingerprint()

    def _load_fingerprint(self) -> None:
        if not os.path.exists(self.fingerprint_path):
            return
        try:
            data = np.load(self.fingerprint_path)
            spec = data["spectrogram"]
            freqs = data["frequencies"]
            mask_band = (freqs >= 500.0) & (freqs <= 800.0)
            sub_spec = spec[mask_band, :]
            log_spec = np.log1p(sub_spec)
            std = float(np.std(log_spec)) + 1e-6
            self._template_norm = ((log_spec - float(np.mean(log_spec))) / std).astype(np.float32)
            self._template_bins, self._win_frames = self._template_norm.shape
            logger.info("Loaded Pure 2D Chime Fingerprint: shape=%s", self._template_norm.shape)
        except Exception as e:
            logger.error("Failed loading chime fingerprint: %s", e)

    def _compute_2d_ncc(self, samples: np.ndarray) -> float:
        if self._template_norm is None or len(samples) < int(0.8 * self.sr):
            return 0.0

        f, _, Zxx = scipy.signal.stft(samples, fs=self.sr, nperseg=self._nperseg, noverlap=self._noverlap)
        mag = np.abs(Zxx)
        mask_band = (f >= 500.0) & (f <= 800.0)
        sub_mag = mag[mask_band, :]

        if sub_mag.shape[0] != self._template_bins or sub_mag.shape[1] < 10:
            return 0.0

        win_f = min(self._win_frames, sub_mag.shape[1])
        tpl = self._template_norm[:, :win_f]
        tpl_norm = (tpl - float(np.mean(tpl))) / (float(np.std(tpl)) + 1e-6)

        log_sub = np.log1p(sub_mag)
        best_ncc = 0.0
        for idx in range(sub_mag.shape[1] - win_f + 1):
            W = log_sub[:, idx : idx + win_f]
            W_std = float(np.std(W))
            if W_std < 0.012:
                continue
            W_norm = (W - float(np.mean(W))) / (W_std + 1e-6)
            ncc = float(np.mean(tpl_norm * W_norm))
            if ncc > best_ncc:
                best_ncc = ncc
        return float(best_ncc)

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        matched, score, reason = verify_ding_dong_chime(samples, sample_rate=self.sr)
        return score, matched, reason

    def feed_pcm_samples(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return

        new_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)
            self._chunks_since_last_eval += 1
            if self._chunks_since_last_eval < 2 or len(self._sample_buffer) < int(0.85 * self.sr):
                return
            self._chunks_since_last_eval = 0
            now_eval = time.time()
            window = np.array(self._sample_buffer, dtype=np.float32)

        rms = float(np.sqrt(np.mean(window * window)))
        if rms < 0.015:
            return

        ncc = self._compute_2d_ncc(window)
        if ncc >= self.ncc_trigger_threshold:
            if (now_eval - self._last_trigger_ts) >= self.cooldown_seconds:
                self._last_trigger_ts = now_eval
                details = f"NCC={ncc:.4f}, RMS={rms:.4f}"
                self._recent_detections.append({
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "ts": now_eval, "ncc": ncc, "matched": True, "details": details,
                })
                log_shazam_event(ncc=ncc, threshold=float(self.ncc_trigger_threshold), matched=True, f_tone1=728.0, f_tone2=570.0, details=details)
                logger.info("🚨 [Shazam Chime] 2D-VINGERAFDRUK MATCH! Score=%.4f", ncc)
                try:
                    from app.ring_coordinator import register_ring_signal
                    register_ring_signal(source="SHAZAM_CHIME", details={"ncc": ncc, "reason": details})
                except Exception as e:
                    logger.error("Failed triggering ring via coordinator: %s", e)
                    if self._loop and self._loop.is_running():
                        from app.main import call_sm
                        asyncio.run_coroutine_threadsafe(call_sm.ring(trigger_source="SHAZAM_CHIME"), self._loop)

    def get_recent_detections(self) -> List[Dict[str, Any]]:
        return list(reversed(self._recent_detections))

    def _audio_stream_worker(self) -> None:
        import array, fcntl, termios
        rtsp_url = "rtsp://127.0.0.1:8554/deurbel"
        cmd = [
            "nice", "-n", "15", "ffmpeg", "-fflags", "nobuffer", "-flags", "low_delay",
            "-probesize", "32", "-analyzeduration", "0", "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-vn", "-ar", "16000", "-ac", "1", "-f", "s16le", "-flush_packets", "1", "pipe:1",
        ]
        while self._running:
            try:
                self._process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                while self._running and self._process.poll() is None:
                    try:
                        buf = array.array("i", [0])
                        fcntl.ioctl(self._process.stdout.fileno(), termios.FIONREAD, buf)
                        if buf[0] > 320000: self._process.stdout.read(buf[0] - 32000)
                    except Exception: pass
                    chunk = bytearray()
                    while len(chunk) < 1600 and self._running and self._process.poll() is None:
                        part = self._process.stdout.read(1600 - len(chunk))
                        if not part: break
                        chunk.extend(part)
                    if not chunk: break
                    self.feed_pcm_samples(bytes(chunk))
            except Exception: pass
            finally:
                if self._process:
                    try: self._process.kill()
                    except Exception: pass
                    self._process = None
            if self._running: time.sleep(3.0)

    async def start(self) -> None:
        if self._running: return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._worker_thread = threading.Thread(target=self._audio_stream_worker, daemon=True)
        self._worker_thread.start()

    async def stop(self) -> None:
        self._running = False
        if self._process:
            try: self._process.kill()
            except Exception: pass
            self._process = None

def verify_ding_dong_chime(
    audio_window: np.ndarray,
    sample_rate: int = 16000,
    **kwargs: Any,
) -> Tuple[bool, float, str]:
    """Backward-compatibility stub delegating directly to pure 2D NCC."""
    if sample_rate != 16000 and len(audio_window) > 0:
        audio_window = scipy.signal.resample(audio_window, int(len(audio_window) * 16000 / float(sample_rate))).astype(np.float32)
    ncc = chime_detector._compute_2d_ncc(audio_window)
    matched = ncc >= 0.65
    score = max(float(ncc), 0.85 if matched else float(ncc))
    return matched, score, f"Pure2D_NCC={ncc:.4f}"

ring_detector = ShazamChimeDetector()
chime_detector = ring_detector
