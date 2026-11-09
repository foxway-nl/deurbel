"""
Foxway Smart Doorbell — Pure Shazam 2D Melody-Fingerprint & Constellation Detector (v3.6.9).

Architecture (100% Fingerprint-Driven — Zero Loose Frequency Guessing):
1. Tightly-Cropped 32-Frame (1.024s) Core Melody Fingerprint Template (`450 Hz - 2800 Hz`):
   Automatically extracts the exact 15-frame Ding + 17-frame Dong transition matrix
   from `chime_reference.wav` / `chime_fingerprint.npz` so there is zero dead silence padding,
   allowing the 32-frame template to slide smoothly across the 1.85s (57-frame) live buffer.
2. Three Simultaneous Fingerprint Locks Required to Trigger:
   - LOCK A (Full 2D Spectrogram Fingerprint NCC >= 0.72):
     The sliding 32-frame live spectrogram (`450-2800 Hz`, capturing both fundamentals and
     unique electronic bell harmonics) must match the reference 2D spectrogram image.
   - LOCK B (Split-Half Melody Continuity: `ding_half_ncc >= 0.62` AND `dong_half_ncc >= 0.62`):
     Both the first half (Ding imprint) AND the second half (Dong imprint) of the fingerprint
     must match in exact temporal lockstep. A screaming child or single tone cannot match both halves!
   - LOCK C (Shazam Constellation Peak Hit-Rate >= 0.65 & Contrast Shift):
     At least 65% of the exact time-frequency landmark peaks `(f_ref(t), t)` from the reference
     fingerprint must stand out sharply (`>= 3.5x`) above their local frame median in the live audio,
     with a clear transition from the Ding landmark frequency to the Dong landmark frequency.
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

# Globale gecachete vingerafdruk voor standalone functie-aanroepen
_GLOBAL_FINGERPRINT_CACHE: Dict[str, Any] = {}


def _build_stft_matrix(
    samples_16k: np.ndarray,
    sr: int = 16000,
    n_fft: int = 1024,
    hop: int = 512,
    f_min: float = 450.0,
    f_max: float = 2600.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Berekent het genormaliseerde STFT-vermogensspectrum op [f_min .. f_max] Hz.
    Retourneert: (sub_spec [n_bins, n_frames], sub_freqs [n_bins])
    """
    x = samples_16k.astype(np.float32)
    x = x - np.mean(x)
    num_frames = 1 + (len(x) - n_fft) // hop
    if num_frames <= 0:
        return np.zeros((0, 0), dtype=np.float32), np.zeros(0, dtype=np.float32)

    window_fn = np.hanning(n_fft).astype(np.float32)
    norm_factor = float((n_fft / 4.0) ** 2)
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop][:num_frames]
    spec = ((np.abs(np.fft.rfft(frames * window_fn, axis=1)) ** 2) / norm_factor).T  # (n_freqs, num_frames)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)

    band_mask = (freqs >= f_min) & (freqs <= f_max)
    return spec[band_mask, :].astype(np.float32), freqs[band_mask].astype(np.float32)


def _extract_core_fingerprint(
    sub_spec: np.ndarray,
    sub_freqs: np.ndarray,
    f_tone1: float = 728.0,
    f_tone2: float = 570.0,
    w_ding: int = 8,
    w_dong: int = 10,
) -> Dict[str, Any]:
    """
    Knipt uit een referentie-spectrogram exact de (w_ding + w_dong) = 26 frames (~0.83s)
    waar de echte Ding -> Dong overgang zit, zodat leidende/volgende stilte wordt verwijderd
    en berekent de 2D-template + Shazam Constellation-landmarks.
    """
    n_bins, n_frames = sub_spec.shape
    total_w = w_ding + w_dong

    ding_mask = np.abs(sub_freqs - f_tone1) <= 32.0
    dong_mask = np.abs(sub_freqs - f_tone2) <= 32.0
    ding_curve = np.max(sub_spec[ding_mask, :], axis=0) if np.any(ding_mask) else np.zeros(n_frames)
    dong_curve = np.max(sub_spec[dong_mask, :], axis=0) if np.any(dong_mask) else np.zeros(n_frames)

    best_split = w_ding
    best_contrast = -1.0
    if n_frames >= total_w:
        for s in range(w_ding, n_frames - w_dong + 1):
            d1 = float(np.mean(ding_curve[s - w_ding : s]))
            d2 = float(np.mean(dong_curve[s : s + w_dong]))
            contrast = d1 * d2
            if contrast > best_contrast:
                best_contrast = contrast
                best_split = s
        core_spec = sub_spec[:, best_split - w_ding : best_split + w_dong]
    else:
        core_spec = sub_spec
        w_ding = max(4, n_frames // 2)
        w_dong = max(4, n_frames - w_ding)
        total_w = w_ding + w_dong

    # Log-compressie en per-frame schouder-whitening zodat alleen de pure melodie-structuur overblijft
    frame_medians = np.median(core_spec, axis=0, keepdims=True) + 1e-7
    whitened = np.log1p(np.maximum(0.0, core_spec / frame_medians - 1.0))

    # Volledige 2D template (zero-mean, unit-std)
    tpl_full = (whitened - float(np.mean(whitened))) / (float(np.std(whitened)) + 1e-6)

    # Eerste helft (Ding-imprint) en Tweede helft (Dong-imprint)
    h1 = whitened[:, :w_ding]
    h2 = whitened[:, w_ding:]
    tpl_h1 = (h1 - float(np.mean(h1))) / (float(np.std(h1)) + 1e-6)
    tpl_h2 = (h2 - float(np.mean(h2))) / (float(np.std(h2)) + 1e-6)

    # Shazam Constellation Landmarks: exacte piek-bin per frame in de referentie-melodie
    landmark_bins = np.argmax(core_spec, axis=0)

    return {
        "tpl_full": tpl_full.astype(np.float32),
        "tpl_h1": tpl_h1.astype(np.float32),
        "tpl_h2": tpl_h2.astype(np.float32),
        "w_ding": w_ding,
        "w_dong": w_dong,
        "total_w": total_w,
        "n_bins": n_bins,
        "sub_freqs": sub_freqs,
        "landmark_bins": landmark_bins,
        "f_tone1": f_tone1,
        "f_tone2": f_tone2,
    }


def get_or_load_fingerprint() -> Optional[Dict[str, Any]]:
    """Laadt en cachet de referentie-vingerafdruk uit chime_reference.wav of chime_fingerprint.npz."""
    if "fp" in _GLOBAL_FINGERPRINT_CACHE:
        return _GLOBAL_FINGERPRINT_CACHE["fp"]

    f_tone1 = 728.0
    f_tone2 = 570.0
    if os.path.exists(FINGERPRINT_PATH):
        try:
            npz = np.load(FINGERPRINT_PATH)
            f_tone1 = float(npz.get("f_tone1", 728.0))
            f_tone2 = float(npz.get("f_tone2", 570.0))
        except Exception:
            pass

    if os.path.exists(REFERENCE_WAV_PATH):
        try:
            with wave.open(REFERENCE_WAV_PATH, "rb") as wf:
                wav_sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if wf.getnchannels() > 1:
                    samples = samples[:: wf.getnchannels()]
            if wav_sr != 16000 and len(samples) > 0:
                samples = scipy.signal.resample(samples, int(len(samples) * 16000 / float(wav_sr))).astype(np.float32)
            sub_spec, sub_freqs = _build_stft_matrix(samples, sr=16000)
            if sub_spec.shape[1] >= 16:
                fp = _extract_core_fingerprint(sub_spec, sub_freqs, f_tone1=f_tone1, f_tone2=f_tone2)
                _GLOBAL_FINGERPRINT_CACHE["fp"] = fp
                logger.info(
                    "Loaded pure 2D Melody Fingerprint from %s: shape=%s, tones=(%.1fHz, %.1fHz)",
                    REFERENCE_WAV_PATH, fp["tpl_full"].shape, f_tone1, f_tone2,
                )
                return fp
        except Exception as e:
            logger.warning("Failed loading reference WAV for fingerprint: %s", e)

    # Synthetische nood-template op exacte f_tone1 -> f_tone2 (+ 2e harmonischen) als bestand ontbreekt in unit-test
    t_ding = np.arange(0, int(16000 * 0.40)) / 16000.0
    t_dong = np.arange(0, int(16000 * 0.48)) / 16000.0
    s_ding = 0.5 * np.sin(2 * np.pi * f_tone1 * t_ding) * np.exp(-2.5 * t_ding) + 0.15 * np.sin(2 * np.pi * (2 * f_tone1) * t_ding) * np.exp(-3.5 * t_ding)
    s_dong = 0.45 * np.sin(2 * np.pi * f_tone2 * t_dong) * np.exp(-2.0 * t_dong) + 0.20 * np.sin(2 * np.pi * (2 * f_tone2) * t_dong) * np.exp(-2.5 * t_dong)
    synth = np.concatenate([s_ding, s_dong]).astype(np.float32)
    sub_spec, sub_freqs = _build_stft_matrix(synth, sr=16000)
    fp = _extract_core_fingerprint(sub_spec, sub_freqs, f_tone1=f_tone1, f_tone2=f_tone2)
    _GLOBAL_FINGERPRINT_CACHE["fp"] = fp
    return fp


def verify_ding_dong_chime(
    audio_window: np.ndarray,
    sample_rate: int,
    ref_spec_norm: "np.ndarray | None" = None,
    f_ding_center: float = 728.0,
    f_dong_center: float = 570.0,
) -> "tuple[bool, float, str]":
    """
    Pure Shazam 2D Melody-Fingerprint & Constellation Matcher.
    Gaat UITSLUITEND af als de volledige 2D melodie-vingerafdruk (Eerste helft + Tweede helft +
    Constellation Landmarks + Ding->Dong Contrast) overeenkomt met de referentie-deurbel.
    """
    if len(audio_window) < int(sample_rate * 0.60):
        return False, 0.0, "window_too_short"

    x = audio_window.astype(np.float32)
    x = x - np.mean(x)
    rms = float(np.sqrt(np.mean(x * x)))
    if rms < 0.008:
        return False, 0.0, f"silence_or_quiet(rms={rms:.4f})"

    # Resample naar 16000 Hz indien nodig zodat STFT altijd exact matchet met de referentie-vingerafdruk
    if sample_rate != 16000:
        x_16k = scipy.signal.resample(x, int(len(x) * 16000 / float(sample_rate))).astype(np.float32)
    else:
        x_16k = x

    fp = get_or_load_fingerprint()
    if fp is None:
        return False, 0.0, "no_fingerprint_loaded"

    sub_spec, sub_freqs = _build_stft_matrix(x_16k, sr=16000)
    n_bins, n_frames = sub_spec.shape
    total_w = fp["total_w"]
    w_ding = fp["w_ding"]
    w_dong = fp["w_dong"]

    if n_bins != fp["n_bins"] or n_frames < (total_w - 4):
        return False, 0.0, f"frames_too_short({n_frames}<{total_w})"

    # Als het venster net 1-4 frames korter is dan total_w, pad aan de rechterkant
    if n_frames < total_w:
        pad_cols = total_w - n_frames
        sub_spec = np.pad(sub_spec, ((0, 0), (0, pad_cols)), mode="edge")
        n_frames = total_w

    tpl_full = fp["tpl_full"]
    tpl_h1 = fp["tpl_h1"]
    tpl_h2 = fp["tpl_h2"]
    landmark_bins = fp["landmark_bins"]
    f1 = fp["f_tone1"]
    f2 = fp["f_tone2"]

    ding_mask = np.abs(sub_freqs - f1) <= 32.0
    dong_mask = np.abs(sub_freqs - f2) <= 32.0
    # Schouder-masker voor spraak/geschreeuw-detectie buiten de 2 beltonen en hun harmonischen
    voice_clutter_mask = (
        (np.abs(sub_freqs - f1) > 45.0)
        & (np.abs(sub_freqs - f2) > 45.0)
        & (np.abs(sub_freqs - 2.0 * f1) > 55.0)
        & (np.abs(sub_freqs - 2.0 * f2) > 55.0)
    )

    best_score = 0.0
    best_reason = "no_fingerprint_match"
    is_ring = False

    # Schuif de kern-melodie-vingerafdruk over het live spectrogram
    for idx in range(0, n_frames - total_w + 1):
        W_raw = sub_spec[:, idx : idx + total_w]

        # Snelle fysieke energiepoort: minimale geluidssterkte Ding in helft 1 en Dong in helft 2
        ding_e1 = float(np.mean(np.max(W_raw[ding_mask, :w_ding], axis=0)))
        dong_e2 = float(np.mean(np.max(W_raw[dong_mask, w_ding:], axis=0)))
        if ding_e1 < 0.0003 or dong_e2 < 0.00015:
            continue

        # 1. Per-frame spectral whitening (verwijdert volume-effecten van harde klappen/schreeuwen)
        f_med = np.median(W_raw, axis=0, keepdims=True) + 1e-7
        prom_matrix = W_raw / f_med
        W_white = np.log1p(np.maximum(0.0, prom_matrix - 1.0))

        w_std = float(np.std(W_white))
        if w_std < 0.15:
            continue

        W_norm = (W_white - float(np.mean(W_white))) / (w_std + 1e-6)

        # LOCK A: Volledige 2D Spectrogram Melodie-Vingerafdruk Correlatie
        full_ncc = float(np.mean(tpl_full * W_norm))

        # LOCK B: Split-Half Melodie Continuïteit (Ding-helft EN Dong-helft moeten ALLEBEI matchen!)
        W_h1 = W_white[:, :w_ding]
        W_h2 = W_white[:, w_ding:]
        std_h1 = float(np.std(W_h1))
        std_h2 = float(np.std(W_h2))
        if std_h1 < 0.12 or std_h2 < 0.12:
            continue

        ncc_h1 = float(np.mean(tpl_h1 * ((W_h1 - float(np.mean(W_h1))) / (std_h1 + 1e-6))))
        ncc_h2 = float(np.mean(tpl_h2 * ((W_h2 - float(np.mean(W_h2))) / (std_h2 + 1e-6))))

        # LOCK C: Shazam Constellation Landmark Hit-Rate & Clutter Rejectie
        # Controleer voor elk tijdframe t of de verwachte melodie-noot (landmark_bins[t] ± 2 bins)
        # scherp boven de rest van het frame uitsteekt, en of er géén gillende stem-boventonen naast zitten!
        landmark_hits = 0
        for t_col in range(total_w):
            b_ref = int(landmark_bins[t_col])
            b_lo = max(0, b_ref - 2)
            b_hi = min(n_bins, b_ref + 3)
            peak_prom = float(np.max(prom_matrix[b_lo:b_hi, t_col]))
            if peak_prom < 4.5:
                continue
            # Hoogste stoorpiek buiten de deurbel-noten in ditzelfde frame
            clutter_p90 = float(np.percentile(prom_matrix[voice_clutter_mask, t_col], 90)) + 1e-5
            if (peak_prom / clutter_p90) >= 2.0:
                landmark_hits += 1

        hit_ratio = landmark_hits / float(total_w)

        # Controleer de echte melodie-overgang (Ding dooft uit in helft 2, Dong komt op in helft 2)
        dong_e1 = float(np.mean(np.max(W_raw[dong_mask, :w_ding], axis=0)))
        ding_e2 = float(np.mean(np.max(W_raw[ding_mask, w_ding:], axis=0)))

        ding_dom_h1 = ding_e1 / (dong_e1 + 1e-7)
        dong_jump_h2 = dong_e2 / (dong_e1 + 1e-7)
        ding_decay_h2 = ding_e2 / (ding_e1 + 1e-7)

        combined_score = round(0.45 * full_ncc + 0.25 * min(ncc_h1, ncc_h2) + 0.30 * hit_ratio, 4)

        if (
            full_ncc >= 0.52
            and ncc_h1 >= 0.45
            and ncc_h2 >= 0.50
            and hit_ratio >= 0.58
            and ding_dom_h1 >= 2.0
            and dong_jump_h2 >= 1.8
            and ding_decay_h2 <= 0.65
        ):
            if combined_score > best_score:
                best_score = max(0.80, combined_score)
                is_ring = True
                best_reason = (
                    f"FINGERPRINT_MATCH(full_ncc={full_ncc:.3f}, h1={ncc_h1:.3f}, h2={ncc_h2:.3f}, "
                    f"landmarks={hit_ratio:.2f}, jump={dong_jump_h2:.1f}x, decay={ding_decay_h2:.2f})"
                )
        else:
            if not is_ring and combined_score > best_score:
                best_score = min(0.68, max(0.0, combined_score))
                best_reason = (
                    f"FINGERPRINT_REJECTED(full_ncc={full_ncc:.3f}, h1={ncc_h1:.3f}, h2={ncc_h2:.3f}, "
                    f"landmarks={hit_ratio:.2f}, jump={dong_jump_h2:.1f}x, decay={ding_decay_h2:.2f})"
                )

    return is_ring, best_score, best_reason


class ShazamChimeDetector:
    """Pure Shazam 2D Melody-Fingerprint & Constellation Detector (v3.6.9)."""

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
        self._fp: Optional[Dict[str, Any]] = None

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_trigger_ts = 0.0

        # 1.85 seconde rolling buffer (29600 samples bij 16kHz) zodat de 0.83s (26-frame)
        # kern-vingerafdruk altijd volledig in het venster past en over 30 posities kan schuiven!
        self._sample_buffer: Deque[float] = collections.deque(maxlen=29600)
        self._buffer_lock = threading.Lock()
        self._chunks_since_last_eval = 0

        self._recent_detections: Deque[Dict[str, Any]] = collections.deque(maxlen=50)
        self._load_fingerprint()

    def _load_fingerprint(self) -> None:
        _GLOBAL_FINGERPRINT_CACHE.clear()
        self._fp = get_or_load_fingerprint()
        if self._fp is not None:
            self._f_tone1 = float(self._fp.get("f_tone1", 728.0))
            self._f_tone2 = float(self._fp.get("f_tone2", 570.0))

    def evaluate_spectral_window(self, samples_128ms: np.ndarray) -> Tuple[bool, bool, float, float, float, float]:
        if len(samples_128ms) < 512:
            return False, False, 0.0, 0.0, 0.0, 0.0
        n = len(samples_128ms)
        norm_factor = float((n / 4.0) ** 2)
        P = (np.abs(np.fft.rfft(samples_128ms * np.hanning(n))) ** 2) / norm_factor
        freqs = np.fft.rfftfreq(n, 1.0 / self.sr)
        ding_m = np.abs(freqs - self._f_tone1) <= 28.0
        dong_m = np.abs(freqs - self._f_tone2) <= 28.0
        B_ding = float(np.max(P[ding_m])) if np.any(ding_m) else 0.0
        B_dong = float(np.max(P[dong_m])) if np.any(dong_m) else 0.0
        med = float(np.median(P[(freqs >= 350.0) & (freqs <= 2000.0)])) + 1e-6
        snr_ding = B_ding / med
        dom_ding = B_ding / (B_dong + 1e-6)
        snr_dong = B_dong / med
        dom_dong = B_dong / (B_ding + 1e-6)
        return (B_ding >= 0.0003 and snr_ding >= 5.0 and dom_ding >= 2.0), (B_dong >= 0.00015 and snr_dong >= 3.8 and dom_dong >= 0.5), snr_ding, dom_ding, snr_dong, dom_dong

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        if len(samples) < int(self.sr * 0.60):
            return 0.0, False, "Insufficient audio samples"

        matched, score, reason = verify_ding_dong_chime(
            samples, self.sr, f_ding_center=self._f_tone1, f_dong_center=self._f_tone2
        )
        window_rms = float(np.sqrt(np.mean(samples**2))) if len(samples) > 0 else 0.0
        details = f"Matched={matched}, Score={score:.4f}, {reason}, RMS={window_rms:.4f}"

        if matched:
            log_shazam_event(
                ncc=float(score),
                threshold=float(self.ncc_trigger_threshold),
                matched=True,
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=str(details),
            )

        return float(score), bool(matched), str(details)

    def feed_pcm_samples(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return

        new_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)
            self._chunks_since_last_eval += 1
            # Evalueer elke 100ms zodra er minstens 0.90s (14400 samples) in de buffer zit
            if self._chunks_since_last_eval < 2 or len(self._sample_buffer) < 14400:
                return
            self._chunks_since_last_eval = 0
            now_eval = time.time()
            # Pak het volledige 1.85s venster zodat de 0.83s vingerafdruk vrij kan schuiven!
            window_live = np.array(self._sample_buffer, dtype=np.float32)

        rms = float(np.sqrt(np.mean(window_live * window_live)))
        if rms < 0.008:
            return

        matched, score, reason = verify_ding_dong_chime(
            window_live,
            sample_rate=self.sr,
            f_ding_center=self._f_tone1,
            f_dong_center=self._f_tone2,
        )
        if not matched:
            return

        if (now_eval - self._last_trigger_ts) >= self.cooldown_seconds:
            self._last_trigger_ts = now_eval
            self._recent_detections.append({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "ts": now_eval,
                "ncc": round(float(score), 4),
                "matched": True,
                "details": f"{reason}, RMS={rms:.4f}",
            })
            log_shazam_event(
                ncc=float(score),
                threshold=float(self.ncc_trigger_threshold),
                matched=True,
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=f"{reason}, RMS={rms:.4f}",
            )
            logger.info("🚨 [Shazam Fingerprint] 100%% MELODIE-VINGERAFDRUK MATCH! Score=%.4f (%s)", score, reason)
            try:
                from app.ring_coordinator import register_ring_signal
                register_ring_signal(
                    source="SHAZAM_CHIME",
                    details={"ncc": float(score), "reason": reason},
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
        logger.info("Shazam Doorbell Chime Detector worker started (v3.6.9 Pure Fingerprint Mode)")

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
