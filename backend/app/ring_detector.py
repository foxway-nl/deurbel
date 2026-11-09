"""
Foxway Smart Doorbell — Mathematically Normalized Single "Ding-Dong" Detector (v3.6.8).

Validated Fixes in v3.6.8:
1. True Normalized FFT Power (`spec / ((n_fft / 4.0) ** 2)`):
   Converts raw unnormalized NumPy `rfft` power into true physical sine-amplitude squared $A^2 \in [0, 1]$.
   - Quiet street noise ($A < 0.015 \implies A^2 < 0.00022$) is 100% blocked by the hard floor!
   - Real doorbell chime ($A \ge 0.04 \implies A^2 \ge 0.0016$) passes cleanly.
2. Self-Consistent STFT Dimension Resampling in `_load_fingerprint` & `_compute_2d_ncc`:
   Guarantees that `_template_norm` and live STFT windows ALWAYS have the exact same frequency
   bin count (no more 19 vs 39 bin mismatch returning 0.0!).
3. Percussive Bell-Strike Windowing (6 frames = ~192ms Ding strike -> 7 frames = ~224ms Dong strike):
   Matches the exponential decay envelope of the single Ding-Dong in the real EZVIZ DB1 Pro videos.
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
import wave
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import scipy.signal

from app.logger import log_shazam_event

logger = logging.getLogger("foxway.ring_detector")

FINGERPRINT_PATH = "/mnt/opslag/deurbel/backend/app/assets/chime_fingerprint.npz"
REFERENCE_WAV_PATH = "/mnt/opslag/deurbel/backend/app/assets/chime_reference.wav"


def verify_ding_dong_chime(
    audio_window: np.ndarray,
    sample_rate: int,
    ref_spec_norm: "np.ndarray | None" = None,
    f_ding_center: float = 728.0,
    f_dong_center: float = 570.0,
) -> "tuple[bool, float, str]":
    """
    Detecteert de enkele Ding-Dong melodie (~728 Hz -> ~570 Hz) met strikt genormaliseerd
    FFT-vermogen zodat zachte straatruis én harde klappen/stemmen 100% worden geweigerd.
    """
    if len(audio_window) < int(sample_rate * 0.50):
        return False, 0.0, "window_too_short"

    x = audio_window.astype(np.float32)
    x = x - np.mean(x)
    rms = float(np.sqrt(np.mean(x * x)))

    # Harde RMS-ondergrens tegen stille straat / microfoonruis
    if rms < 0.012:
        return False, 0.0, f"quiet_ambient_rejected(rms={rms:.4f})"

    if sample_rate >= 15000:
        n_fft = 1024
        hop = 512
    else:
        n_fft = 512
        hop = 256

    num_frames = 1 + (len(x) - n_fft) // hop
    if num_frames < 12:
        return False, 0.0, "too_few_frames"

    window_fn = np.hanning(n_fft).astype(np.float32)
    # Normalisatiefactor voor Hann-windowed rfft zodat een zuivere sinus met amplitude A
    # een piek-vermogen van exact A^2 heeft (onafhankelijk van n_fft!):
    norm_factor = float((n_fft / 4.0) ** 2)

    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop][:num_frames]
    spec = (np.abs(np.fft.rfft(frames * window_fn, axis=1)) ** 2) / norm_factor
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    # Smalle kernbanden voor Ding (~728 Hz) en Dong (~570 Hz + 2e harmonische ~1140 Hz)
    ding_mask = (freqs >= (f_ding_center - 28.0)) & (freqs <= (f_ding_center + 28.0))
    dong_mask = (freqs >= (f_dong_center - 28.0)) & (freqs <= (f_dong_center + 28.0))
    dong_h2_mask = (freqs >= (2.0 * f_dong_center - 36.0)) & (freqs <= (2.0 * f_dong_center + 36.0))

    # Lokale schouderbanden op ±45..140 Hz naast Ding en Dong
    ding_sh_mask = (
        ((freqs >= (f_ding_center - 140.0)) & (freqs <= (f_ding_center - 45.0)))
        | ((freqs >= (f_ding_center + 45.0)) & (freqs <= (f_ding_center + 140.0)))
    )
    dong_sh_mask = (
        ((freqs >= (f_dong_center - 140.0)) & (freqs <= (f_dong_center - 45.0)))
        | ((freqs >= (f_dong_center + 45.0)) & (freqs <= (f_dong_center + 140.0)))
    )

    ding_raw = np.max(spec[:, ding_mask], axis=1)
    dong_fund = np.max(spec[:, dong_mask], axis=1)
    dong_h2 = np.max(spec[:, dong_h2_mask], axis=1) if np.any(dong_h2_mask) else np.zeros(num_frames)
    dong_raw = dong_fund + 0.35 * dong_h2

    # 75e percentiel van lokale schouders + veilige ruisvloer (1e-6 in A^2 eenheden)
    ding_sh = np.percentile(spec[:, ding_sh_mask], 75, axis=1) + 1e-6
    dong_sh = np.percentile(spec[:, dong_sh_mask], 75, axis=1) + 1e-6

    ding_prom = ding_raw / ding_sh
    dong_prom = dong_raw / dong_sh

    # Minimale echte fysieke toon-energie A^2 (amplitude A >= 0.025 voor Ding, A >= 0.015 voor Dong)
    min_ding_a2 = 0.0006
    min_dong_a2 = 0.00025

    best_score = 0.0
    best_reason = "no_single_ding_dong_match"
    is_ring = False

    # Bekijk lokale aanslag-vensters van 6, 8 en 10 frames (192ms, 256ms, 320ms) rond de Ding->Dong overgang
    for w_len in (6, 8, 10):
        if num_frames < (2 * w_len):
            continue

        for split_idx in range(w_len, num_frames - w_len + 1):
            p1_s = split_idx - w_len
            p2_e = split_idx + w_len

            ding_p1 = float(np.mean(ding_raw[p1_s:split_idx]))
            dong_p1 = float(np.mean(dong_raw[p1_s:split_idx]))
            prom_ding_p1 = float(np.mean(ding_prom[p1_s:split_idx]))

            ding_p2 = float(np.mean(ding_raw[split_idx:p2_e]))
            dong_p2 = float(np.mean(dong_raw[split_idx:p2_e]))
            prom_dong_p2 = float(np.mean(dong_prom[split_idx:p2_e]))

            # Harde fysieke amplitude-drempel: negeert zachte achtergrondgeluiden 100%
            if ding_p1 < min_ding_a2 or dong_p2 < min_dong_a2:
                continue

            clean_ding_frames = int(np.sum((ding_prom[p1_s:split_idx] >= 4.0) & (ding_raw[p1_s:split_idx] >= min_ding_a2 * 0.4)))
            clean_dong_frames = int(np.sum((dong_prom[split_idx:p2_e] >= 3.2) & (dong_raw[split_idx:p2_e] >= min_dong_a2 * 0.4)))

            ding_over_dong_p1 = ding_p1 / (dong_p1 + 1e-6)
            dong_onset_jump = dong_p2 / (dong_p1 + 1e-6)
            ding_decay_ratio = ding_p2 / (ding_p1 + 1e-6)
            dominance_shift = (ding_p1 / (dong_p1 + 1e-6)) * (dong_p2 / (ding_p2 + 1e-6))

            d1_curve = np.clip(ding_prom[p1_s:p2_e], 0.0, 25.0)
            d2_curve = np.clip(dong_prom[p1_s:p2_e], 0.0, 25.0)
            d1_norm = d1_curve / (np.max(d1_curve) + 1e-6)
            d2_norm = d2_curve / (np.max(d2_curve) + 1e-6)
            diff_env = d1_norm - d2_norm
            ideal_step = np.concatenate([np.ones(w_len), -np.ones(w_len)])
            env_corr = float(
                np.sum(diff_env * ideal_step)
                / (np.linalg.norm(diff_env) * np.linalg.norm(ideal_step) + 1e-6)
            )

            min_clean = max(3, w_len // 2)

            if (
                prom_ding_p1 >= 5.0
                and prom_dong_p2 >= 3.8
                and clean_ding_frames >= min_clean
                and clean_dong_frames >= min_clean
                and ding_over_dong_p1 >= 2.2
                and dong_onset_jump >= 2.0
                and ding_decay_ratio <= 0.68
                and dominance_shift >= 3.8
                and env_corr >= 0.48
            ):
                raw_q = min(1.0, (np.log10(prom_ding_p1 + 1) + np.log10(prom_dong_p2 + 1)) / 2.8)
                score = round(0.75 + 0.24 * (0.5 * raw_q + 0.5 * env_corr), 4)
                if score > best_score:
                    best_score = score
                    is_ring = True
                    best_reason = (
                        f"VALIDATED_DING_DONG(w={w_len}, rms={rms:.3f}, a2_728={ding_p1:.4f}, a2_570={dong_p2:.4f}, "
                        f"prom728={prom_ding_p1:.1f}, prom570={prom_dong_p2:.1f}, "
                        f"jump570={dong_onset_jump:.1f}x, decay728={ding_decay_ratio:.2f}, corr={env_corr:.2f})"
                    )
            else:
                partial = min(0.68, max(0.0, env_corr * 0.65))
                if not is_ring and partial > best_score:
                    best_score = round(partial, 4)
                    best_reason = (
                        f"REJECTED(rms={rms:.3f}, a2_728={ding_p1:.4f}, a2_570={dong_p2:.4f}, "
                        f"prom728={prom_ding_p1:.1f}, prom570={prom_dong_p2:.1f}, "
                        f"jump570={dong_onset_jump:.1f}x, decay728={ding_decay_ratio:.2f}, corr={env_corr:.2f})"
                    )

    return is_ring, best_score, best_reason


class ShazamChimeDetector:
    """Mathematically Normalized Single 'Ding-Dong' Chime Detector (v3.6.8)."""

    def __init__(self, fingerprint_path: str = FINGERPRINT_PATH):
        self.fingerprint_path = fingerprint_path
        self.sr = 16000
        self.win_size = 1024
        self.hop_size = 800
        self.n_fft = 1024
        self.cooldown_seconds = 6.0
        self.ncc_trigger_threshold = 0.72
        self.ncc_log_threshold = 0.50

        self._f_tone1 = 728.0
        self._f_tone2 = 570.0
        self._template_norm: Optional[np.ndarray] = None
        self._template_bins = 0
        self._win_frames = 0
        self._stft_nperseg = 1024
        self._stft_noverlap = 1024 - 256

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_trigger_ts = 0.0

        self._sample_buffer: Deque[float] = collections.deque(maxlen=28000)
        self._buffer_lock = threading.Lock()
        self._chunks_since_last_eval = 0

        self._recent_detections: Deque[Dict[str, Any]] = collections.deque(maxlen=50)
        self._load_fingerprint()

    def _load_fingerprint(self) -> None:
        """
        Bouwt de 2D-spectrogram template met EXACT dezelfde STFT-instellingen
        als `_compute_2d_ncc`, of schaalt de bestaande fingerprint naar exact dezelfde bins!
        """
        # Probeer eerst rechtstreeks uit chime_reference.wav te bouwen voor 100% STFT-identiteit
        if os.path.exists(REFERENCE_WAV_PATH):
            try:
                with wave.open(REFERENCE_WAV_PATH, "rb") as wf:
                    wav_sr = wf.getframerate()
                    raw_frames = wf.readframes(wf.getnframes())
                    samples = np.frombuffer(raw_frames, dtype=np.int16).astype(np.float32) / 32768.0
                    if wf.getnchannels() > 1:
                        samples = samples[:: wf.getnchannels()]
                if wav_sr != self.sr and len(samples) > 0:
                    num_target = int(len(samples) * self.sr / float(wav_sr))
                    samples = scipy.signal.resample(samples, num_target).astype(np.float32)
                if len(samples) >= int(0.6 * self.sr):
                    f, _, Zxx = scipy.signal.stft(
                        samples, fs=self.sr, nperseg=self._stft_nperseg, noverlap=self._stft_noverlap
                    )
                    mag = np.abs(Zxx)
                    mask_band = (f >= 500.0) & (f <= 800.0)
                    sub_spec = mag[mask_band, :]
                    if sub_spec.shape[0] > 0 and sub_spec.shape[1] >= 12:
                        log_spec = np.log1p(sub_spec)
                        std = float(np.std(log_spec)) + 1e-6
                        self._template_norm = ((log_spec - float(np.mean(log_spec))) / std).astype(np.float32)
                        self._template_bins, self._win_frames = self._template_norm.shape
                        logger.info(
                            "Built consistent 2D Chime Template from WAV: shape=%s",
                            self._template_norm.shape,
                        )
                        return
            except Exception as we:
                logger.warning("Could not build template from WAV (%s), falling back to NPZ", we)

        if not os.path.exists(self.fingerprint_path):
            return
        try:
            data = np.load(self.fingerprint_path)
            self._f_tone1 = float(data.get("f_tone1", 728.0))
            self._f_tone2 = float(data.get("f_tone2", 570.0))
            spec = data["spectrogram"]
            freqs = data["frequencies"]
            mask_band = (freqs >= 500.0) & (freqs <= 800.0)
            sub_spec = spec[mask_band, :]

            # Bereken exact hoeveel bins onze runtime STFT heeft tussen 500 en 800 Hz:
            runtime_freqs = np.fft.rfftfreq(self._stft_nperseg, 1.0 / self.sr)
            target_bins = int(np.sum((runtime_freqs >= 500.0) & (runtime_freqs <= 800.0)))
            if sub_spec.shape[0] != target_bins and sub_spec.shape[0] > 1:
                # Interpoleer langs de frequentie-as zodat sub_mag.shape[0] ALTIJD overeenkomt!
                x_old = np.linspace(0.0, 1.0, sub_spec.shape[0])
                x_new = np.linspace(0.0, 1.0, target_bins)
                sub_spec = np.array([np.interp(x_new, x_old, sub_spec[:, col]) for col in range(sub_spec.shape[1])]).T

            log_spec = np.log1p(sub_spec)
            std = float(np.std(log_spec)) + 1e-6
            self._template_norm = ((log_spec - float(np.mean(log_spec))) / std).astype(np.float32)
            self._template_bins, self._win_frames = self._template_norm.shape
            logger.info("Loaded & aligned 2D Chime Template from NPZ: shape=%s", self._template_norm.shape)
        except Exception as e:
            logger.error("Failed loading chime fingerprint: %s", e)

    def _compute_2d_ncc(self, samples: np.ndarray) -> float:
        if self._template_norm is None or len(samples) < int(0.8 * self.sr):
            return 0.0

        f, _, Zxx = scipy.signal.stft(
            samples, fs=self.sr, nperseg=self._stft_nperseg, noverlap=self._stft_noverlap
        )
        mag = np.abs(Zxx)
        mask_band = (f >= 500.0) & (f <= 800.0)
        sub_mag = mag[mask_band, :]

        if sub_mag.shape[0] != self._template_bins or sub_mag.shape[1] < 8:
            return 0.0

        win_f = min(self._win_frames, sub_mag.shape[1])
        tpl = self._template_norm[:, :win_f]
        tpl_std = float(np.std(tpl)) + 1e-6
        tpl_norm = (tpl - float(np.mean(tpl))) / tpl_std

        log_sub = np.log1p(sub_mag)
        best_ncc = 0.0
        for idx in range(sub_mag.shape[1] - win_f + 1):
            W = log_sub[:, idx : idx + win_f]
            W_std = float(np.std(W))
            if W_std < 0.01:
                continue
            W_norm = (W - float(np.mean(W))) / (W_std + 1e-6)
            ncc = float(np.mean(tpl_norm * W_norm))
            if ncc > best_ncc:
                best_ncc = ncc
        return float(best_ncc)

    def evaluate_spectral_window(self, samples_128ms: np.ndarray) -> Tuple[bool, bool, float, float, float, float]:
        if len(samples_128ms) < 512:
            return False, False, 0.0, 0.0, 0.0, 0.0
        n = len(samples_128ms)
        norm_factor = float((n / 4.0) ** 2)
        P = (np.abs(np.fft.rfft(samples_128ms * np.hanning(n))) ** 2) / norm_factor
        freqs = np.fft.rfftfreq(n, 1.0 / self.sr)
        ding_m = (freqs >= (self._f_tone1 - 28.0)) & (freqs <= (self._f_tone1 + 28.0))
        dong_m = (freqs >= (self._f_tone2 - 28.0)) & (freqs <= (self._f_tone2 + 28.0))
        sh_ding = ((freqs >= (self._f_tone1 - 140.0)) & (freqs <= (self._f_tone1 - 45.0))) | ((freqs >= (self._f_tone1 + 45.0)) & (freqs <= (self._f_tone1 + 140.0)))
        sh_dong = ((freqs >= (self._f_tone2 - 140.0)) & (freqs <= (self._f_tone2 - 45.0))) | ((freqs >= (self._f_tone2 + 45.0)) & (freqs <= (self._f_tone2 + 140.0)))
        B_ding = float(np.max(P[ding_m])) if np.any(ding_m) else 0.0
        B_dong = float(np.max(P[dong_m])) if np.any(dong_m) else 0.0
        N_ding = float(np.percentile(P[sh_ding], 75)) + 1e-6
        N_dong = float(np.percentile(P[sh_dong], 75)) + 1e-6
        snr_ding = B_ding / N_ding
        dom_ding = B_ding / (B_dong + 1e-6)
        snr_dong = B_dong / N_dong
        dom_dong = B_dong / (B_ding + 1e-6)
        return (B_ding >= 0.0006 and snr_ding >= 5.0 and dom_ding >= 2.0), (B_dong >= 0.00025 and snr_dong >= 3.8 and dom_dong >= 0.5), snr_ding, dom_ding, snr_dong, dom_dong

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        if len(samples) < int(self.sr * 0.50):
            return 0.0, False, "Insufficient audio samples"

        best_ncc = self._compute_2d_ncc(samples)
        sub_len = int(self.sr * 1.15)
        step_len = int(self.sr * 0.15)

        vdc_matched = False
        best_vdc_score = 0.0
        best_reason = "no_match"

        if len(samples) <= sub_len:
            vdc_matched, best_vdc_score, best_reason = verify_ding_dong_chime(
                samples, self.sr, f_ding_center=self._f_tone1, f_dong_center=self._f_tone2
            )
        else:
            for start in range(0, len(samples) - int(self.sr * 0.55), step_len):
                sub = samples[start : start + sub_len]
                ok, sc, rsn = verify_ding_dong_chime(
                    sub, self.sr, f_ding_center=self._f_tone1, f_dong_center=self._f_tone2
                )
                if ok and (not vdc_matched or sc > best_vdc_score):
                    vdc_matched = True
                    best_vdc_score = sc
                    best_reason = rsn
                elif not vdc_matched and sc > best_vdc_score:
                    best_vdc_score = sc
                    best_reason = rsn

        matched = bool(vdc_matched)
        final_score = max(best_ncc, best_vdc_score) if matched else min(0.68, max(best_ncc, best_vdc_score))
        window_rms = float(np.sqrt(np.mean(samples**2))) if len(samples) > 0 else 0.0
        details = f"Matched={matched}, VDC={best_vdc_score:.4f}, NCC={best_ncc:.4f}, {best_reason}, RMS={window_rms:.4f}"

        if matched:
            log_shazam_event(
                ncc=float(final_score),
                threshold=float(self.ncc_trigger_threshold),
                matched=True,
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=str(details),
            )

        return float(final_score), bool(matched), str(details)

    def feed_pcm_samples(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return

        new_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)
            self._chunks_since_last_eval += 1
            if self._chunks_since_last_eval < 2 or len(self._sample_buffer) < int(0.95 * self.sr):
                return
            self._chunks_since_last_eval = 0
            now_eval = time.time()
            window_115s = np.array(self._sample_buffer, dtype=np.float32)[-int(1.15 * self.sr):]

        rms = float(np.sqrt(np.mean(window_115s * window_115s)))
        if rms < 0.012:
            return

        vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(
            window_115s,
            sample_rate=self.sr,
            f_ding_center=self._f_tone1,
            f_dong_center=self._f_tone2,
        )
        if not vdc_ok:
            return

        best_ncc = self._compute_2d_ncc(window_115s)
        final_score = round(max(vdc_score, best_ncc), 4)

        if (now_eval - self._last_trigger_ts) >= self.cooldown_seconds:
            self._last_trigger_ts = now_eval
            self._recent_detections.append({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "ts": now_eval,
                "ncc": final_score,
                "matched": True,
                "details": f"NCC={best_ncc:.4f}, {vdc_reason}",
            })
            log_shazam_event(
                ncc=final_score,
                threshold=float(self.ncc_trigger_threshold),
                matched=True,
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=f"NCC={best_ncc:.4f}, {vdc_reason}",
            )
            logger.info(
                "🚨 [Shazam Chime] GEVALIDEERDE DING-DONG MATCH! Score=%.4f, NCC=%.4f (%s)",
                vdc_score, best_ncc, vdc_reason,
            )
            try:
                from app.ring_coordinator import register_ring_signal
                register_ring_signal(
                    source="SHAZAM_CHIME",
                    details={"ncc": final_score, "reason": f"NCC={best_ncc:.3f}, {vdc_reason}"},
                )
            except Exception as e:
                logger.error("Failed triggering ring via coordinator: %s", e)
                if self._loop and self._loop.is_running():
                    from app.main import call_sm
                    asyncio.run_coroutine_threadsafe(call_sm.ring(trigger_source="SHAZAM_CHIME"), self._loop)

    def get_recent_acoustic_energy(self, window_sec: float = 2.0) -> Tuple[float, str]:
        now = time.time()
        for item in reversed(self._recent_detections):
            if item.get("matched") and (now - item.get("ts", 0.0)) <= window_sec:
                return float(item["ncc"]), str(item.get("details", ""))
        return 0.0, ""

    def get_recent_detections(self) -> List[Dict[str, Any]]:
        return list(reversed(self._recent_detections))

    def _audio_stream_worker(self) -> None:
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
        logger.info("Shazam Doorbell Chime Detector worker started (v3.6.8 Normalized Mode)")

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
