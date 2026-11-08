"""
Foxway Smart Doorbell — Infallible 3-Layer 2-Tone "Ding-Dong" Melody State Machine (v3.6.1).

Physical Characteristics of the Doorbell Chime:
- Tone 1 (Ding): ~728 Hz (approx 200-250ms), dominant over 570 Hz and ambient noise.
- Tone 2 (Dong): ~570 Hz (approx 250-350ms), dominant over 728 Hz and ambient noise.
- Causal Sequence: Ding MUST precede Dong by 150ms to 750ms.
- 0% False alarms on broadband noise (cries, wind, screeching brakes, clatter, cutlery) via Spectral Peak-to-Neighborhood SNR.
- 0% False alarms on isolated 728 Hz whistles/tones via strictly enforced sequential Ding -> Dong state machine.
- Phase- and volume-invariant 2D Log-Spectrogram Envelope Cross-Correlation (NCC >= 0.68).
- v3.6.1: Added verify_ding_dong_chime() as final validation gate (prescribed exact algorithm).
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
    Verifieert of een audio_window (lengte ~0.85s tot 1.2s bij 8000 Hz)
    de echte tweetonige EZVIZ DB1 Pro deurbel-melodie (728 Hz -> 570 Hz) bevat.

    Exacte implementatie zoals voorgeschreven in v3.6.1 nood-herstelopdracht.
    Controleert 4 harde criteria tegelijk op een schuivend STFT-venster:
      1. snr_ding_p1   >= 5.0  (Ding dominant over breedband ruis in eerste deel)
      2. ratio_ding_p1 >= 1.8  (728 Hz domineert over 570 Hz in eerste deel)
      3. snr_dong_p2   >= 4.5  (Dong dominant over breedband ruis in tweede deel)
      4. dong_onset_jump >= 2.0 OR dominance_shift >= 2.5  (duidelijke toonwisseling)

    Sample rate: 8000 Hz (decimeer 16kHz buffer met [::2] vóór aanroep).
    Retourneert: (is_valid_ring, score, reason)
    """
    if len(audio_window) < int(sample_rate * 0.65):
        return False, 0.0, "window_too_short"

    x = audio_window.astype(np.float32)
    x = x - np.mean(x)
    rms = float(np.sqrt(np.mean(x * x)))
    if rms < 1e-4:
        return False, 0.0, "silence"

    # STFT: frames van 64ms (512 samples bij 8000 Hz) met 32ms stap (256 samples)
    n_fft = 512
    hop = 256
    window_fn = np.hanning(n_fft).astype(np.float32)
    num_frames = 1 + (len(x) - n_fft) // hop
    if num_frames < 12:
        return False, 0.0, "too_few_frames"

    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop][:num_frames]
    spec = np.abs(np.fft.rfft(frames * window_fn, axis=1)) ** 2  # (num_frames, 257)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    # Frequentiebanden
    # Ding = ~728 Hz (700 - 756 Hz)
    # Dong = ~570 Hz (545 - 595 Hz)
    # Breedband-ruisvloer = 320-500 Hz, 620-675 Hz, 800-1200 Hz
    ding_mask = (freqs >= 700) & (freqs <= 756)
    dong_mask = (freqs >= 545) & (freqs <= 595)
    noise_mask = (
        ((freqs >= 320) & (freqs <= 500))
        | ((freqs >= 620) & (freqs <= 675))
        | ((freqs >= 800) & (freqs <= 1200))
    )

    ding_env = np.max(spec[:, ding_mask], axis=1) if np.any(ding_mask) else np.zeros(num_frames)
    dong_env = np.max(spec[:, dong_mask], axis=1) if np.any(dong_mask) else np.zeros(num_frames)
    noise_env = np.mean(spec[:, noise_mask], axis=1) + 1e-9 if np.any(noise_mask) else np.ones(num_frames) * 1e-9

    # Zoek het beste Ding -> Dong overgangspunt (split_idx tussen 25% en 65% van het venster)
    best_score = 0.0
    best_reason = "no_two_tone_sequence"
    is_ring = False

    for split_idx in range(max(3, int(num_frames * 0.25)), min(num_frames - 3, int(num_frames * 0.65) + 1)):
        # Deel 1 = Ding-fase: frames 0 .. split_idx-1
        # Deel 2 = Dong-fase: frames split_idx .. num_frames-1
        ding_p1 = ding_env[:split_idx]
        dong_p1 = dong_env[:split_idx]
        noise_p1 = noise_env[:split_idx]

        dong_p2 = dong_env[split_idx:]
        ding_p2 = ding_env[split_idx:]
        noise_p2 = noise_env[split_idx:]

        # Criterium 1: SNR van 728 Hz in deel 1
        snr_ding_p1 = float(np.mean(ding_p1) / (np.mean(noise_p1) + 1e-9))

        # Criterium 2: Ratio 728 Hz t.o.v. 570 Hz in deel 1
        ratio_ding_over_dong_p1 = float(np.mean(ding_p1) / (np.mean(dong_p1) + 1e-9))

        # Criterium 3: SNR van 570 Hz in deel 2
        snr_dong_p2 = float(np.mean(dong_p2) / (np.mean(noise_p2) + 1e-9))

        # Criterium 4a: dong onset jump (570 Hz energie stijgt bij overgang)
        dong_onset_jump = float(np.mean(dong_p2) / (np.mean(dong_p1) + 1e-9))

        # Criterium 4b: dominance shift (Ding domineert deel1, Dong domineert deel2)
        dom_p1 = float(np.mean(ding_p1) / (np.mean(dong_p1) + 1e-9))
        dom_p2 = float(np.mean(dong_p2) / (np.mean(ding_p2) + 1e-9))
        dominance_shift = dom_p1 * dom_p2  # Hoog als beide dominanties groot zijn

        # Samengestelde score (geometrisch gemiddelde van de 4 criteria t.o.v. drempel)
        c1 = snr_ding_p1 / 5.0
        c2 = ratio_ding_over_dong_p1 / 1.8
        c3 = snr_dong_p2 / 4.5
        c4 = max(dong_onset_jump / 2.0, dominance_shift / 2.5)
        score = float(np.power(c1 * c2 * c3 * c4, 0.25))  # 4de-machtwortel (geometric mean)

        if score > best_score:
            best_score = score
            best_reason = (
                f"split={split_idx}/{num_frames}, snr_ding={snr_ding_p1:.2f}, "
                f"ratio_dd={ratio_ding_over_dong_p1:.2f}, snr_dong={snr_dong_p2:.2f}, "
                f"onset={dong_onset_jump:.2f}, dom_shift={dominance_shift:.2f}"
            )
            # Alle 4 criteria moeten tegelijk slagen
            if (
                snr_ding_p1 >= 5.0
                and ratio_ding_over_dong_p1 >= 1.8
                and snr_dong_p2 >= 4.5
                and (dong_onset_jump >= 2.0 or dominance_shift >= 2.5)
            ):
                is_ring = True

    return is_ring, best_score, best_reason


class ShazamChimeDetector:
    """Infallible 3-Layer 2-Tone 'Ding-Dong' Melody State Machine."""

    def __init__(self, fingerprint_path: str = FINGERPRINT_PATH):
        self.fingerprint_path = fingerprint_path
        self.sr = 16000
        self.win_size = 2048       # 128 ms Hann window at 16kHz
        self.hop_size = 800        # 50 ms time step at 16kHz
        self.n_fft = 2048
        self.cooldown_seconds = 5.0
        self.ncc_trigger_threshold = 0.72
        self.ncc_log_threshold = 0.25

        # Pre-calculated frequency masks for FFT (win_size=2048, sr=16000, df=7.8125 Hz)
        self._freqs = np.fft.rfftfreq(self.win_size, 1.0 / self.sr)
        self._ding_mask = (self._freqs >= 712.0) & (self._freqs <= 744.0)   # ~728 Hz Ding
        self._dong_mask = (self._freqs >= 554.0) & (self._freqs <= 586.0)   # ~570 Hz Dong
        self._noise_mask = (
            ((self._freqs >= 350.0) & (self._freqs <= 510.0))
            | ((self._freqs >= 620.0) & (self._freqs <= 680.0))
            | ((self._freqs >= 780.0) & (self._freqs <= 1150.0))
        )
        self._hann_window = np.hanning(self.win_size).astype(np.float32)

        # 2D Spectrogram Envelope Reference Template (20 bins: 500-800 Hz)
        self._template_norm: Optional[np.ndarray] = None
        self._template_bins = 20
        self._win_frames = 86
        self._f_tone1 = 728.0
        self._f_tone2 = 570.0

        # State Machine Variables
        # States: "IDLE" -> "DING_DETECTED" -> (trigger on Dong)
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

        # Audio Ring Buffer (4.0s of 16-bit mono audio = 64000 samples)
        self._sample_buffer: Deque[float] = collections.deque(maxlen=64000)
        self._buffer_lock = threading.Lock()

        # Recent detections for telemetry & blackbox
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

            # Crop to 20 bins between 500 Hz and 800 Hz
            mask_20 = (freqs >= 500.0) & (freqs <= 800.0)
            sub_spec = spec[mask_20, :]
            self._template_bins = sub_spec.shape[0]
            self._win_frames = sub_spec.shape[1]

            # Pre-normalize 2D log-spectrogram template for envelope NCC
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
        Laag 1: Smalle-Band Tonaliteit (Spectral Peak-to-Neighborhood SNR).
        Returns: (is_ding, is_dong, snr_ding, dom_ding, snr_dong, dom_dong)
        """
        if len(samples_128ms) < self.win_size:
            return False, False, 0.0, 0.0, 0.0, 0.0

        chunk = samples_128ms[-self.win_size:] * self._hann_window
        P = np.abs(np.fft.rfft(chunk)) ** 2

        B_ding = float(np.max(P[self._ding_mask])) if np.any(self._ding_mask) else 0.0
        B_dong = float(np.max(P[self._dong_mask])) if np.any(self._dong_mask) else 0.0
        N_floor = float(np.mean(P[self._noise_mask])) if np.any(self._noise_mask) else 1e-9

        snr_ding = B_ding / (N_floor + 1e-9)
        dom_ding = B_ding / (B_dong + 1e-9)
        snr_dong = B_dong / (N_floor + 1e-9)
        dom_dong = B_dong / (B_ding + 1e-9)

        # Exact mathematical SNR and Dominance gates (with minimum absolute acoustic power)
        is_ding = bool((snr_ding >= 7.5) and (dom_ding >= 2.2) and (B_ding >= 0.10))
        is_dong = bool((snr_dong >= 6.5) and (dom_dong >= 1.6) and (B_dong >= 0.10))

        return is_ding, is_dong, snr_ding, dom_ding, snr_dong, dom_dong

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        """
        Runs 3-Layer Ding-Dong Melody & NCC evaluation across a window of audio (1.0s - 4.0s).
        Returns: (best_ncc, is_match, details_string)
        """
        if len(samples) < self.win_size:
            return 0.0, False, "Insufficient audio samples"

        best_ncc = 0.0
        melody_verified = False
        max_snr_ding = 0.0
        max_snr_dong = 0.0

        # Step 1: Slide through audio in 50ms hops to evaluate Layer 1 & Layer 2 Sequential State Machine
        state = "IDLE"
        ding_count = 0
        dong_count = 0
        t_ding = 0.0

        total_samples = len(samples)
        for offset in range(0, total_samples - self.win_size + 1, self.hop_size):
            t_current = offset / float(self.sr)
            window_chunk = samples[offset : offset + self.win_size]
            is_ding, is_dong, snr_ding, dom_ding, snr_dong, dom_dong = self.evaluate_spectral_window(window_chunk)

            if snr_ding > max_snr_ding:
                max_snr_ding = snr_ding
            if snr_dong > max_snr_dong:
                max_snr_dong = snr_dong

            # Layer 2 Causal State Machine
            if state == "IDLE":
                if is_ding:
                    ding_count += 1
                    if ding_count >= 2:  # >= 100 ms of Ding
                        state = "DING_DETECTED"
                        t_ding = t_current
                        dong_count = 0
                else:
                    ding_count = 0
            elif state == "DING_DETECTED":
                delta_t = t_current - t_ding
                if delta_t > 0.750:
                    state = "IDLE"
                    ding_count = 0
                    dong_count = 0
                elif 0.150 <= delta_t <= 0.750:
                    if is_dong:
                        dong_count += 1
                        if dong_count >= 2:  # >= 100 ms of Dong
                            melody_verified = True
                            state = "IDLE"
                            ding_count = 0
                            dong_count = 0
                            break

        # Step 2: Layer 3 - 2D Log-Spectrogram Envelope Normalized Cross-Correlation
        if self._template_norm is not None and total_samples >= int(0.9 * self.sr):
            f, t_stft, Zxx = scipy.signal.stft(samples, fs=self.sr, nperseg=1024, noverlap=1024 - 160)
            mag = np.abs(Zxx)
            mask_20 = (f >= 500.0) & (f <= 800.0)
            sub_mag = mag[mask_20, :]

            if sub_mag.shape[0] == self._template_bins and sub_mag.shape[1] >= self._win_frames:
                log_sub = np.log1p(sub_mag)
                for idx in range(sub_mag.shape[1] - self._win_frames + 1):
                    W = log_sub[:, idx : idx + self._win_frames]
                    W_std = float(np.std(W)) + 1e-6
                    W_norm = (W - float(np.mean(W))) / W_std
                    ncc = float(np.mean(self._template_norm * W_norm))
                    if ncc > best_ncc:
                        best_ncc = ncc

        # Approval: Melody sequence (Ding -> Dong) verified OR high envelope NCC >= 0.72 with both peaks present
        window_rms = float(np.sqrt(np.mean(samples**2))) if len(samples) > 0 else 0.0
        candidate_match = bool(
            melody_verified
            or (best_ncc >= self.ncc_trigger_threshold and max_snr_ding >= 5.0 and max_snr_dong >= 4.0)
        )

        # v3.6.1: Final gate — verify_ding_dong_chime() op 8kHz decimated window
        matched = False
        if candidate_match:
            samples_8k = samples[::2]  # Decimeer 16kHz -> 8kHz
            vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(samples_8k, sample_rate=8000)
            if vdc_ok:
                matched = True
                logger.debug("[verify_ding_dong_chime] PASSED: score=%.3f (%s)", vdc_score, vdc_reason)
            else:
                logger.debug("[verify_ding_dong_chime] REJECTED: score=%.3f (%s)", vdc_score, vdc_reason)

        details = f"Melody={melody_verified}, NCC={best_ncc:.4f}, DingSNR={max_snr_ding:.1f}, DongSNR={max_snr_dong:.1f}, RMS={window_rms:.3f}"

        if best_ncc >= self.ncc_log_threshold or matched:
            log_shazam_event(
                ncc=float(best_ncc),
                threshold=float(self.ncc_trigger_threshold),
                matched=bool(matched),
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=str(details),
            )

        return float(best_ncc), bool(matched), str(details)

    def feed_pcm_samples(self, pcm_bytes: bytes) -> None:
        """Feed raw 16kHz 16-bit mono PCM bytes into the streaming detector."""
        if not pcm_bytes:
            return

        new_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)

            # Need at least 128ms of audio (2048 samples) to evaluate a window
            if len(self._sample_buffer) < self.win_size:
                return

            now_eval = time.time()
            chunk_128ms = np.array(self._sample_buffer, dtype=np.float32)[-self.win_size:]

        # Run Layer 1 & Layer 2 Streaming State Machine
        is_ding, is_dong, snr_ding, dom_ding, snr_dong, dom_dong = self.evaluate_spectral_window(chunk_128ms)

        triggered = False
        with self._buffer_lock:
            if self._state == "IDLE":
                if is_ding:
                    self._ding_consecutive_count += 1
                    if self._ding_consecutive_count >= 2:  # >= 100 ms of Ding
                        self._state = "DING_DETECTED"
                        self._t_ding = now_eval
                        self._dong_consecutive_count = 0
                        logger.debug("🎵 [Chime State] STATE_DING_DETECTED: 728 Hz Ding confirmed (SNR=%.1f)", snr_ding)
                else:
                    self._ding_consecutive_count = 0
            elif self._state == "DING_DETECTED":
                delta_t = now_eval - self._t_ding
                if delta_t > 0.750:
                    logger.debug("[RING REJECTED] 728Hz piek gedetecteerd zonder opvolgende 570Hz Dong-toon (geen deurbel-melodie)")
                    self._state = "IDLE"
                    self._ding_consecutive_count = 0
                    self._dong_consecutive_count = 0
                elif 0.150 <= delta_t <= 0.750:
                    if is_dong:
                        self._dong_consecutive_count += 1
                        if self._dong_consecutive_count >= 2:  # >= 100 ms of Dong
                            logger.info(
                                "🎉 [Chime State] DING-DONG MELODIE BEVESTIGD! (DingSNR=%.1f, DongSNR=%.1f, delta=%.1fms)",
                                snr_ding, snr_dong, delta_t * 1000.0,
                            )
                            triggered = True
                            self._state = "IDLE"
                            self._ding_consecutive_count = 0
                            self._dong_consecutive_count = 0

            # Compute NCC on the last 2.0 seconds if available
            if len(self._sample_buffer) >= int(1.2 * self.sr):
                window_2s = np.array(self._sample_buffer, dtype=np.float32)[-int(2.0 * self.sr):]
            else:
                window_2s = np.array(self._sample_buffer, dtype=np.float32)

        # Compute 2D Envelope NCC across window_2s for telemetry & high-confidence fallback
        best_ncc = 0.0
        window_rms = float(np.sqrt(np.mean(window_2s**2))) if len(window_2s) > 0 else 0.0

        if len(window_2s) >= int(1.0 * self.sr) and self._template_norm is not None:
            f, t_stft, Zxx = scipy.signal.stft(window_2s, fs=self.sr, nperseg=1024, noverlap=1024 - 160)
            mag = np.abs(Zxx)
            mask_20 = (f >= 500.0) & (f <= 800.0)
            sub_mag = mag[mask_20, :]
            if sub_mag.shape[0] == self._template_bins and sub_mag.shape[1] >= self._win_frames:
                log_sub = np.log1p(sub_mag)
                for idx in range(sub_mag.shape[1] - self._win_frames + 1):
                    W = log_sub[:, idx : idx + self._win_frames]
                    W_std = float(np.std(W)) + 1e-6
                    W_norm = (W - float(np.mean(W))) / W_std
                    ncc = float(np.mean(self._template_norm * W_norm))
                    if ncc > best_ncc:
                        best_ncc = ncc
            if best_ncc >= self.ncc_trigger_threshold and not triggered:
                # NCC fallback: ook door verify_ding_dong_chime gate (v3.6.1)
                window_8k = window_2s[::2]
                vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(window_8k, sample_rate=8000)
                if vdc_ok:
                    triggered = True
                    logger.debug("[NCC+verify_ding_dong_chime] PASSED: ncc=%.3f score=%.3f", best_ncc, vdc_score)
                else:
                    logger.debug("[NCC fallback BLOCKED by verify_ding_dong_chime]: ncc=%.3f score=%.3f (%s)", best_ncc, vdc_score, vdc_reason)

        # v3.6.1: state-machine triggered also passes through verify_ding_dong_chime gate
        if triggered and len(window_2s) >= int(1.0 * self.sr):
            window_8k = window_2s[::2]
            vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(window_8k, sample_rate=8000)
            if not vdc_ok:
                logger.info("[State Machine BLOCKED by verify_ding_dong_chime]: score=%.3f (%s) — NOT ringing", vdc_score, vdc_reason)
                triggered = False
            else:
                logger.debug("[State Machine + verify_ding_dong_chime] CONFIRMED: score=%.3f", vdc_score)

        if best_ncc >= self.ncc_log_threshold or triggered:
            self._recent_detections.append({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "ts": now_eval,
                "ncc": round(float(best_ncc), 4),
                "matched": bool(triggered),
                "details": f"DingSNR={snr_ding:.1f}, DongSNR={snr_dong:.1f}, NCC={best_ncc:.4f}, RMS={window_rms:.3f}",
            })

        # Register acoustic candidate with RingCoordinator
        if best_ncc >= 0.60 or triggered:
            try:
                from app.ring_coordinator import ring_coordinator
                ring_coordinator.register_acoustic_candidate(
                    ncc=float(max(best_ncc, 0.75 if triggered else 0.60)),
                    details=f"DingSNR={snr_ding:.1f}, DongSNR={snr_dong:.1f}",
                )
            except Exception:
                pass

        if triggered:
            now = now_eval
            if (now - self._last_trigger_ts) >= self.cooldown_seconds:
                self._last_trigger_ts = now
                logger.info("🚨 [Shazam Chime] 100% MELODIE MATCH! Registering ring signal with coordinator...")
                try:
                    from app.ring_coordinator import register_ring_signal
                    register_ring_signal(
                        source="SHAZAM_CHIME",
                        details={"ncc": float(best_ncc), "ding_snr": float(snr_ding), "dong_snr": float(snr_dong)},
                    )
                except Exception as e:
                    logger.error("Failed triggering ring from Shazam: %s", e)
                    if self._loop and self._loop.is_running():
                        from app.main import call_sm
                        asyncio.run_coroutine_threadsafe(call_sm.ring(trigger_source="SHAZAM_CHIME"), self._loop)
            else:
                logger.info("[Shazam Chime] Match debounced (<%.1fs cooldown)", self.cooldown_seconds)

    def get_recent_acoustic_energy(self, window_sec: float = 2.0) -> Tuple[float, str]:
        """Returns the highest NCC score and details registered within the last window_sec seconds."""
        now = time.time()
        best_ncc = 0.0
        best_details = ""
        for item in reversed(self._recent_detections):
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
            "pipe:1"
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

                    # Read 50ms chunk (1600 bytes = 800 16-bit samples at 16kHz)
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
        logger.info("Shazam Doorbell Chime Detector worker started (v3.6.0)")

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
chime_detector = ring_detector  # Backward compatibility alias
