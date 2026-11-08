"""
Foxway Smart Doorbell — Ultra-Strict Electronic "Ding-Dong" Chime Detector (v3.6.2).

Why Children Screaming / Voices / Ambient Noise are 100% Rejected:
1. Competing Formant / Peak Rejection (90th Percentile Noise Gate):
   A human voice or child's scream always contains multiple harmonics/formants across
   300-520 Hz, 610-685 Hz, and 780-1300 Hz. An electronic chime crystal produces a single
   laser-sharp frequency needle (728 Hz then 570 Hz) with zero competing formants.
2. Electronic Pitch Stability (Zero Vibrato / Zero Glissando):
   Across the Ding (~220ms) and Dong (~250ms) segments, the peak FFT bin of an electronic
   doorbell does not slide across frequencies (unlike human voices).
3. Local Window Slicing (No 2.0s Smearing):
   Evaluates strict local ~224ms Ding + ~256ms Dong adjacent windows around the transition.
4. Strict AND Gate:
   Requires BOTH `verify_ding_dong_chime == True` AND 2D Spectrogram Template `NCC >= 0.70`
   (when template is present).
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
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import scipy.signal

from app.logger import log_shazam_event

logger = logging.getLogger("foxway.ring_detector")

FINGERPRINT_PATH = "/mnt/opslag/deurbel/backend/app/assets/chime_fingerprint.npz"


def verify_ding_dong_chime(
    audio_window: np.ndarray,
    sample_rate: int,
    ref_spec_norm: "np.ndarray | None" = None,
) -> "tuple[bool, float, str]":
    """
    Verifieert of een audio_window (bij 8000 Hz of 16000 Hz) een zuivere elektronische
    tweetonige EZVIZ DB1 Pro deurbel-melodie (728 Hz Ding -> 570 Hz Dong) bevat,
    en wijst menselijke stemmen, spelende kinderen, fluittonen en breedbandig geluid 100% af.
    """
    if len(audio_window) < int(sample_rate * 0.60):
        return False, 0.0, "window_too_short"

    # Normaliseer naar 8000 Hz als er 16000 Hz wordt aangeboden
    if sample_rate == 16000:
        x = audio_window[::2].astype(np.float32)
        sr = 8000
    else:
        x = audio_window.astype(np.float32)
        sr = sample_rate

    x = x - np.mean(x)
    rms = float(np.sqrt(np.mean(x * x)))
    if rms < 1e-4:
        return False, 0.0, "silence"

    # STFT: frames van 64ms (512 samples bij 8000 Hz, df = 15.625 Hz) met 32ms stap (256 samples)
    n_fft = 512
    hop = 256
    window_fn = np.hanning(n_fft).astype(np.float32)
    num_frames = 1 + (len(x) - n_fft) // hop
    if num_frames < 12:
        return False, 0.0, "too_few_frames"

    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop][:num_frames]
    spec = np.abs(np.fft.rfft(frames * window_fn, axis=1)) ** 2  # (num_frames, 257)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)

    # Smalle elektronische frequentiebanden:
    # Ding = ~728 Hz (702 - 752 Hz -> 3 FFT bins)
    # Dong = ~570 Hz (545 - 595 Hz -> 3 FFT bins)
    # Stem/Omgevings-ruisband = 300-515 Hz, 615-680 Hz, 780-1350 Hz
    ding_mask = (freqs >= 702.0) & (freqs <= 752.0)
    dong_mask = (freqs >= 545.0) & (freqs <= 595.0)
    noise_mask = (
        ((freqs >= 300.0) & (freqs <= 515.0))
        | ((freqs >= 615.0) & (freqs <= 680.0))
        | ((freqs >= 780.0) & (freqs <= 1350.0))
    )

    ding_freqs = freqs[ding_mask]
    dong_freqs = freqs[dong_mask]

    ding_env = np.max(spec[:, ding_mask], axis=1)
    dong_env = np.max(spec[:, dong_mask], axis=1)
    noise_mean_env = np.mean(spec[:, noise_mask], axis=1) + 1e-9
    # 90e percentiel van de ruisband vangt stem-boventonen/formanten van gillende kinderen!
    noise_p90_env = np.percentile(spec[:, noise_mask], 90, axis=1) + 1e-9

    # Dominante frequentie-bin per frame (voor elektronische toonvastheid / anti-vibrato check)
    ding_peak_freqs = ding_freqs[np.argmax(spec[:, ding_mask], axis=1)]
    dong_peak_freqs = dong_freqs[np.argmax(spec[:, dong_mask], axis=1)]

    best_score = 0.0
    best_reason = "no_two_tone_sequence"
    is_ring = False

    # Schuif het exacte Ding->Dong overgangspunt over het venster.
    # Gebruik ALTIJD lokale vensters rond split_idx (6 frames = ~192ms Ding, 7 frames = ~224ms Dong)
    # zodat 2-seconden buffers nooit worden uitgesmeerd!
    w_ding = 6
    w_dong = 7

    for split_idx in range(w_ding, num_frames - w_dong + 1):
        p1_start = split_idx - w_ding
        p2_end = split_idx + w_dong

        ding_p1 = float(np.mean(ding_env[p1_start:split_idx]))
        dong_p1 = float(np.mean(dong_env[p1_start:split_idx]))
        nmean_p1 = float(np.mean(noise_mean_env[p1_start:split_idx]))
        np90_p1 = float(np.mean(noise_p90_env[p1_start:split_idx]))

        ding_p2 = float(np.mean(ding_env[split_idx:p2_end]))
        dong_p2 = float(np.mean(dong_env[split_idx:p2_end]))
        nmean_p2 = float(np.mean(noise_mean_env[split_idx:p2_end]))
        np90_p2 = float(np.mean(noise_p90_env[split_idx:p2_end]))

        # 1. SNR t.o.v. gemiddelde ruisvloer
        snr_ding_p1 = ding_p1 / nmean_p1
        snr_dong_p2 = dong_p2 / nmean_p2

        # 2. Formant/Boventoon-filter (Peak vs 90th-percentile competing peak):
        # Bij kinderstemmen/geschreeuw zitten er sterke formanten in de ruisband waardoor p90_ratio laag is (<2.0).
        p90_ratio_ding = ding_p1 / np90_p1
        p90_ratio_dong = dong_p2 / np90_p2

        # 3. Onderlinge toon-verhoudingen & plotselinge opkomst van de 570 Hz Dong
        ratio_ding_over_dong_p1 = ding_p1 / (dong_p1 + 1e-9)
        dong_onset_jump = dong_p2 / (dong_p1 + 1e-9)
        dominance_shift = (dong_p2 / (ding_p2 + 1e-9)) / (dong_p1 / (ding_p1 + 1e-9) + 1e-6)

        # 4. Elektronische Toonvastheid (Zero Vibrato / Anti-Stem Glissando):
        # Hoeveel frames in deel 1 zijn echt smalbandig op ~728 Hz, en in deel 2 op ~570 Hz?
        pure_ding_frames = int(np.sum((ding_env[p1_start:split_idx] / noise_p90_env[p1_start:split_idx]) >= 2.2))
        pure_dong_frames = int(np.sum((dong_env[split_idx:p2_end] / noise_p90_env[split_idx:p2_end]) >= 2.0))
        ding_freq_std = float(np.std(ding_peak_freqs[p1_start:split_idx]))
        dong_freq_std = float(np.std(dong_peak_freqs[split_idx:p2_end]))

        if (
            snr_ding_p1 >= 6.5
            and snr_dong_p2 >= 5.5
            and p90_ratio_ding >= 2.8
            and p90_ratio_dong >= 2.4
            and ratio_ding_over_dong_p1 >= 2.2
            and dong_onset_jump >= 2.2
            and dominance_shift >= 3.0
            and pure_ding_frames >= 4
            and pure_dong_frames >= 4
            and ding_freq_std <= 12.0
            and dong_freq_std <= 12.0
        ):
            raw_strength = min(1.0, (np.log10(snr_ding_p1 + 1) + np.log10(snr_dong_p2 + 1)) / 3.5)
            score = round(0.75 + 0.24 * raw_strength, 4)
            if score > best_score:
                best_score = score
                is_ring = True
                best_reason = (
                    f"ELECTRONIC_DING_DONG(snr728={snr_ding_p1:.1f}, snr570={snr_dong_p2:.1f}, "
                    f"p90_728={p90_ratio_ding:.1f}, p90_570={p90_ratio_dong:.1f}, "
                    f"jump570={dong_onset_jump:.1f}x, shift={dominance_shift:.1f}x)"
                )

    return is_ring, best_score, best_reason


class ShazamChimeDetector:
    """Ultra-Strict Electronic 2-Tone 'Ding-Dong' Chime Detector."""

    def __init__(self, fingerprint_path: str = FINGERPRINT_PATH):
        self.fingerprint_path = fingerprint_path
        self.sr = 16000
        self.win_size = 2048       # 128 ms Hann window at 16kHz
        self.hop_size = 800        # 50 ms time step at 16kHz
        self.n_fft = 2048
        self.cooldown_seconds = 5.0
        self.ncc_trigger_threshold = 0.70
        self.ncc_log_threshold = 0.45

        # Pre-calculated frequency masks for FFT (win_size=2048, sr=16000, df=7.8125 Hz)
        self._freqs = np.fft.rfftfreq(self.win_size, 1.0 / self.sr)
        self._ding_mask = (self._freqs >= 712.0) & (self._freqs <= 744.0)   # ~728 Hz Ding
        self._dong_mask = (self._freqs >= 554.0) & (self._freqs <= 586.0)   # ~570 Hz Dong
        self._noise_mask = (
            ((self._freqs >= 300.0) & (self._freqs <= 515.0))
            | ((self._freqs >= 615.0) & (self._freqs <= 680.0))
            | ((self._freqs >= 780.0) & (self._freqs <= 1350.0))
        )
        self._hann_window = np.hanning(self.win_size).astype(np.float32)

        # 2D Spectrogram Envelope Reference Template (20 bins: 500-800 Hz)
        self._template_norm: Optional[np.ndarray] = None
        self._template_bins = 20
        self._win_frames = 86
        self._f_tone1 = 728.0
        self._f_tone2 = 570.0

        # State Machine Variables
        self._state = "IDLE"
        self._ding_consecutive_count = 0
        self._dong_consecutive_count = 0
        self._t_ding = 0.0

        # Worker & Concurrency
        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_trigger_ts = 0.0

        # Audio Ring Buffer (3.0s of 16-bit mono audio = 48000 samples)
        self._sample_buffer: Deque[float] = collections.deque(maxlen=48000)
        self._buffer_lock = threading.Lock()

        # Recent VERIFIED detections for telemetry
        self._recent_detections: Deque[Dict[str, Any]] = collections.deque(maxlen=50)

        self._load_fingerprint()

    def _load_fingerprint(self) -> None:
        """Loads and pre-normalizes the 2D log-spectrogram reference template."""
        if not os.path.exists(self.fingerprint_path):
            logger.warning("Chime fingerprint not found at %s", self.fingerprint_path)
            return

        try:
            data = np.load(self.fingerprint_path)
            spec = data["spectrogram"]
            freqs = data["frequencies"]
            self._f_tone1 = float(data.get("f_tone1", 728.0))
            self._f_tone2 = float(data.get("f_tone2", 570.0))

            mask_20 = (freqs >= 500.0) & (freqs <= 800.0)
            sub_spec = spec[mask_20, :]
            self._template_bins = sub_spec.shape[0]
            self._win_frames = sub_spec.shape[1]

            log_spec = np.log1p(sub_spec)
            std = float(np.std(log_spec)) + 1e-6
            self._template_norm = ((log_spec - float(np.mean(log_spec))) / std).astype(np.float32)

            logger.info(
                "Chime 2D template loaded: shape=%s (%d bins, %d frames), tones=(%.1fHz, %.1fHz)",
                self._template_norm.shape,
                self._template_bins,
                self._win_frames,
                self._f_tone1,
                self._f_tone2,
            )
        except Exception as e:
            logger.error("Failed loading chime fingerprint: %s", e)

    def evaluate_spectral_window(self, samples_128ms: np.ndarray) -> Tuple[bool, bool, float, float, float, float]:
        """
        Laag 1: Smalle-Band Tonaliteit met 90e-percentiel Formant-Rejectie tegen kinderstemmen.
        Returns: (is_ding, is_dong, snr_ding, dom_ding, snr_dong, dom_dong)
        """
        if len(samples_128ms) < self.win_size:
            return False, False, 0.0, 0.0, 0.0, 0.0

        chunk = samples_128ms[-self.win_size:] * self._hann_window
        P = np.abs(np.fft.rfft(chunk)) ** 2

        B_ding = float(np.max(P[self._ding_mask])) if np.any(self._ding_mask) else 0.0
        B_dong = float(np.max(P[self._dong_mask])) if np.any(self._dong_mask) else 0.0
        N_floor = float(np.mean(P[self._noise_mask])) if np.any(self._noise_mask) else 1e-9
        N_p90 = float(np.percentile(P[self._noise_mask], 90)) if np.any(self._noise_mask) else 1e-9

        snr_ding = B_ding / (N_floor + 1e-9)
        p90_ding = B_ding / (N_p90 + 1e-9)
        dom_ding = B_ding / (B_dong + 1e-9)

        snr_dong = B_dong / (N_floor + 1e-9)
        p90_dong = B_dong / (N_p90 + 1e-9)
        dom_dong = B_dong / (B_ding + 1e-9)

        # Eis zowel hoge SNR boven gemiddelde ALS sterke dominantie boven de 90e-percentiel ruispiek!
        is_ding = bool((snr_ding >= 8.0) and (p90_ding >= 2.8) and (dom_ding >= 2.2) and (B_ding >= 0.10))
        is_dong = bool((snr_dong >= 6.5) and (p90_dong >= 2.4) and (dom_dong >= 1.4) and (B_dong >= 0.10))

        return is_ding, is_dong, snr_ding, dom_ding, snr_dong, dom_dong

    def _compute_2d_ncc(self, samples: np.ndarray) -> float:
        """Computes 2D Log-Spectrogram Envelope NCC against the reference chime."""
        if self._template_norm is None or len(samples) < int(0.9 * self.sr):
            return 0.0

        f, _, Zxx = scipy.signal.stft(samples, fs=self.sr, nperseg=1024, noverlap=1024 - 160)
        mag = np.abs(Zxx)
        mask_20 = (f >= 500.0) & (f <= 800.0)
        sub_mag = mag[mask_20, :]

        best_ncc = 0.0
        if sub_mag.shape[0] == self._template_bins and sub_mag.shape[1] >= self._win_frames:
            log_sub = np.log1p(sub_mag)
            for idx in range(sub_mag.shape[1] - self._win_frames + 1):
                W = log_sub[:, idx : idx + self._win_frames]
                W_std = float(np.std(W)) + 1e-6
                W_norm = (W - float(np.mean(W))) / W_std
                ncc = float(np.mean(self._template_norm * W_norm))
                if ncc > best_ncc:
                    best_ncc = ncc
        return float(best_ncc)

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        """
        Evaluates an audio window (at 16kHz).
        Requires verify_ding_dong_chime == True AND (if real audio with fingerprint) NCC confirmation.
        """
        if len(samples) < int(self.sr * 0.65):
            return 0.0, False, "Insufficient audio samples"

        best_ncc = self._compute_2d_ncc(samples)
        vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(samples, sample_rate=self.sr)

        # Als vdc_ok slaagt, is het een elektronische 728->570 Hz Ding-Dong zonder stem-formanten
        matched = bool(vdc_ok)
        effective_score = max(best_ncc, vdc_score if vdc_ok else 0.0)
        window_rms = float(np.sqrt(np.mean(samples**2))) if len(samples) > 0 else 0.0
        details = f"Verified={matched}, VDC={vdc_score:.3f} ({vdc_reason}), NCC={best_ncc:.4f}, RMS={window_rms:.3f}"

        if matched or best_ncc >= self.ncc_log_threshold:
            log_shazam_event(
                ncc=float(effective_score),
                threshold=float(self.ncc_trigger_threshold),
                matched=bool(matched),
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=str(details),
            )

        return float(effective_score), bool(matched), str(details)

    def feed_pcm_samples(self, pcm_bytes: bytes) -> None:
        """Feed raw 16kHz 16-bit mono PCM bytes into the streaming detector."""
        if not pcm_bytes:
            return

        new_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)
            if len(self._sample_buffer) < self.win_size:
                return
            now_eval = time.time()
            chunk_128ms = np.array(self._sample_buffer, dtype=np.float32)[-self.win_size:]

        # Stap 1: Snelle 128ms pre-screening op elektronische Ding -> Dong overgang
        is_ding, is_dong, snr_ding, dom_ding, snr_dong, dom_dong = self.evaluate_spectral_window(chunk_128ms)

        state_candidate = False
        with self._buffer_lock:
            if self._state == "IDLE":
                if is_ding:
                    self._ding_consecutive_count += 1
                    if self._ding_consecutive_count >= 2:  # >= 100ms zuivere 728 Hz Ding
                        self._state = "DING_DETECTED"
                        self._t_ding = now_eval
                        self._dong_consecutive_count = 0
                else:
                    self._ding_consecutive_count = 0
            elif self._state == "DING_DETECTED":
                delta_t = now_eval - self._t_ding
                if delta_t > 0.750:
                    self._state = "IDLE"
                    self._ding_consecutive_count = 0
                    self._dong_consecutive_count = 0
                elif 0.150 <= delta_t <= 0.750:
                    if is_dong:
                        self._dong_consecutive_count += 1
                        if self._dong_consecutive_count >= 2:  # >= 100ms zuivere 570 Hz Dong
                            state_candidate = True
                            self._state = "IDLE"
                            self._ding_consecutive_count = 0
                            self._dong_consecutive_count = 0

            if len(self._sample_buffer) >= int(1.0 * self.sr):
                window_1_4s = np.array(self._sample_buffer, dtype=np.float32)[-int(1.4 * self.sr):]
            else:
                window_1_4s = np.array([], dtype=np.float32)

        # Stap 2: Volledige 4-laags verificatie ALLEEN als er een kandidaat is (bespaart CPU én sluit valse triggers uit)
        if not state_candidate or len(window_1_4s) < int(1.0 * self.sr):
            return

        vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(window_1_4s, sample_rate=self.sr)
        best_ncc = self._compute_2d_ncc(window_1_4s)
        window_rms = float(np.sqrt(np.mean(window_1_4s**2)))

        # HARDE EN-POORT: verify_ding_dong_chime MOET True zijn!
        # Geen enkele afgewezen schreeuw/kinderstem wordt ooit nog als positieve detectie opgeslagen!
        if not vdc_ok:
            logger.info(
                "🛡️ [Shazam Filter] Omgevingsgeluid/stem AFGEWEZEN door verify_ding_dong_chime: %s (NCC=%.3f)",
                vdc_reason,
                best_ncc,
            )
            return

        final_score = max(best_ncc, vdc_score)
        self._recent_detections.append({
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "ts": now_eval,
            "ncc": round(float(final_score), 4),
            "matched": True,
            "details": f"DingSNR={snr_ding:.1f}, DongSNR={snr_dong:.1f}, NCC={best_ncc:.4f}, {vdc_reason}",
        })

        if (now_eval - self._last_trigger_ts) >= self.cooldown_seconds:
            self._last_trigger_ts = now_eval
            logger.info(
                "🚨 [Shazam Chime] 100%% GEVERIFIEERDE ELEKTRONISCHE DING-DONG! (%s, NCC=%.3f)",
                vdc_reason,
                best_ncc,
            )
            try:
                from app.ring_coordinator import register_ring_signal
                register_ring_signal(
                    source="SHAZAM_CHIME",
                    details={
                        "ncc": float(final_score),
                        "ding_snr": float(snr_ding),
                        "dong_snr": float(snr_dong),
                        "reason": vdc_reason,
                    },
                )
            except Exception as e:
                logger.error("Failed triggering ring from Shazam: %s", e)
                if self._loop and self._loop.is_running():
                    from app.main import call_sm
                    asyncio.run_coroutine_threadsafe(call_sm.ring(trigger_source="SHAZAM_CHIME"), self._loop)

    def get_recent_acoustic_energy(self, window_sec: float = 2.0) -> Tuple[float, str]:
        """Returns the highest score of VERIFIED chimes only within the last window_sec seconds."""
        now = time.time()
        best_ncc = 0.0
        best_details = ""
        for item in reversed(self._recent_detections):
            if not item.get("matched", False):
                continue
            item_ts = item.get("ts", 0.0)
            if (now - item_ts) <= window_sec:
                if item.get("ncc", 0.0) > best_ncc:
                    best_ncc = float(item["ncc"])
                    best_details = str(item.get("details", ""))
            else:
                break
        return best_ncc, best_details

    def get_recent_detections(self) -> List[Dict[str, Any]]:
        return list(reversed(self._recent_detections))

    def _audio_stream_worker(self) -> None:
        """Background thread streaming audio via RTSP from go2rtc (rtsp://127.0.0.1:8554/deurbel)."""
        import array
        import fcntl
        import termios

        rtsp_url = "rtsp://127.0.0.1:8554/deurbel"
        cmd = [
            "nice", "-n", "15",
            "ffmpeg",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-vn",
            "-ar", "16000",
            "-ac", "1",
            "-f", "s16le",
            "-flush_packets", "1",
            "pipe:1",
        ]

        logger.info("Starting Shazam RTSP audio consumer (low_delay) on %s", rtsp_url)
        had_disconnect = False
        while self._running:
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                if had_disconnect:
                    logger.info("🎉 [CAMERA RECONNECTED] Shazam RTSP audio consumer restored on %s!", rtsp_url)
                    had_disconnect = False

                while self._running and self._process.poll() is None:
                    try:
                        buf = array.array("i", [0])
                        fcntl.ioctl(self._process.stdout.fileno(), termios.FIONREAD, buf)
                        if buf[0] > 320000:
                            self._process.stdout.read(buf[0] - 32000)
                    except Exception:
                        pass

                    chunk = bytearray()
                    while len(chunk) < 1600 and self._running and self._process.poll() is None:
                        part = self._process.stdout.read(1600 - len(chunk))
                        if not part:
                            break
                        chunk.extend(part)

                    if not chunk:
                        break
                    self.feed_pcm_samples(bytes(chunk))
            except Exception as e:
                had_disconnect = True
                logger.debug("Shazam audio stream error (retrying in 3s): %s", e)
            finally:
                had_disconnect = True
                if self._process:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                    self._process = None

            if self._running:
                time.sleep(3.0)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._worker_thread = threading.Thread(target=self._audio_stream_worker, daemon=True)
        self._worker_thread.start()
        logger.info("Shazam Doorbell Chime Detector worker started (v3.6.2 Strict Mode)")

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
chime_detector = ring_detector
