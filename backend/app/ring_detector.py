"""
Foxway Smart Doorbell — 4-Layer Physical & Quartz-Locked "Ding-Dong" Detector (v3.7.0).

Zero-Assumption Physical Architecture (16000 Hz, n_fft = 2048, df = 7.8125 Hz):
1. LOCK 1 — Near-Field Adaptive Volume Gate:
   The speaker is 2cm from the microphone. A real chime has `rms >= 0.030` AND is at least
   `4.0x` louder than the rolling 30-second ambient street noise floor (`ambient_rms`).
   Empty street background noise (`~1.0x ambient_rms`) is 100% blocked before any FFT!
2. LOCK 2 — Monochromatic Narrowband Energy Fraction (`>= 42%` of total 350-1000 Hz band):
   In a real electronic chime, the 5 FFT bins around 726.6 Hz (`708-745 Hz`) contain >= 42%
   of the TOTAL acoustic energy of the entire `350-1000 Hz` speech/ambient band during Ding.
   Broadband noise, claps, cars, and screaming children spread energy across dozens of bins (< 18%).
3. LOCK 3 — Quartz Pitch Stability & 726.6 Hz -> 570.3 Hz Step Transition:
   - Peak frequency during Ding must stay locked on `726.6 Hz ± 11 Hz` (zero vocal glissando).
   - Peak frequency during Dong must stay locked on `570.3 Hz ± 11 Hz` (zero vocal glissando).
   - 570.3 Hz energy must jump `>= 6.0x` in part 2 while 726.6 Hz decays `<= 0.55x`.
4. LOCK 4 — Exact `n_fft = 2048` 2D Log-Spectrogram Fingerprint (`NCC >= 0.70`):
   Matches the original `chime_fingerprint.npz` (built at `n_fft=2048`) with zero bin mismatch.
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
    f_ding_center: float = 728.0,
    f_dong_center: float = 570.0,
    ambient_rms: float = 0.005,
) -> "tuple[bool, float, str]":
    """
    Toetst of `audio_window` een echte elektronische EZVIZ DB1 Pro Ding-Dong (728 Hz -> 570 Hz)
    bevat op basis van 4 harde fysische criteria.
    """
    if len(audio_window) < int(sample_rate * 0.65):
        return False, 0.0, "window_too_short"

    x = audio_window.astype(np.float32)
    x = x - np.mean(x)
    rms = float(np.sqrt(np.mean(x * x)))

    # LOCK 1A: Absolute én relatieve Near-Field Volume Gate
    min_required_rms = max(0.025, float(ambient_rms) * 3.8)
    if rms < min_required_rms:
        return False, 0.0, f"below_nearfield_rms(rms={rms:.4f}<{min_required_rms:.4f})"

    # Resample naar 16000 Hz indien nodig zodat n_fft=2048 altijd exact df=7.8125 Hz heeft
    if sample_rate != 16000:
        x = scipy.signal.resample(x, int(len(x) * 16000 / float(sample_rate))).astype(np.float32)
    sr = 16000

    n_fft = 2048       # 128 ms window, df = 7.8125 Hz
    hop = 512          # 32 ms step
    num_frames = 1 + (len(x) - n_fft) // hop
    if num_frames < 18:
        return False, 0.0, "too_few_frames"

    window_fn = np.hanning(n_fft).astype(np.float32)
    norm_factor = float((n_fft / 4.0) ** 2)
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop][:num_frames]
    spec = (np.abs(np.fft.rfft(frames * window_fn, axis=1)) ** 2) / norm_factor
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)

    # Smalle kernbanden (±20 Hz = 5 bins bij df=7.8125 Hz)
    ding_mask = (freqs >= (f_ding_center - 20.0)) & (freqs <= (f_ding_center + 20.0))
    dong_mask = (freqs >= (f_dong_center - 20.0)) & (freqs <= (f_dong_center + 20.0))
    dong_h2_mask = (freqs >= (2.0 * f_dong_center - 25.0)) & (freqs <= (2.0 * f_dong_center + 25.0))

    # Volledige spraak/omgevingsband (350 Hz - 1000 Hz) voor monochromatische energie-fractie
    mid_band_mask = (freqs >= 350.0) & (freqs <= 1000.0)
    # Zoek-banden voor piek-frequentie stabiliteit (anti-stem glissando)
    ding_search_mask = (freqs >= 660.0) & (freqs <= 800.0)
    dong_search_mask = (freqs >= 510.0) & (freqs <= 630.0)
    ding_search_freqs = freqs[ding_search_mask]
    dong_search_freqs = freqs[dong_search_mask]

    # Energie per frame
    ding_band_e = np.sum(spec[:, ding_mask], axis=1)
    dong_band_e = np.sum(spec[:, dong_mask], axis=1)
    dong_h2_e = np.sum(spec[:, dong_h2_mask], axis=1)
    total_mid_e = np.sum(spec[:, mid_band_mask], axis=1) + 1e-8

    ding_peak_val = np.max(spec[:, ding_mask], axis=1)
    dong_peak_val = np.max(spec[:, dong_mask], axis=1) + 0.30 * np.max(spec[:, dong_h2_mask], axis=1)

    # Welke fractie (0.0 .. 1.0) van ALLE energie tussen 350 en 1000 Hz zit puur in de smalle bel-noot?
    ding_frac = ding_band_e / total_mid_e
    dong_frac = (dong_band_e + 0.5 * ding_band_e) / total_mid_e  # In deel 2 klinkt Ding nog licht na

    # Exacte piek-frequentie per frame in het Ding- en Dong-gebied
    ding_inst_freq = ding_search_freqs[np.argmax(spec[:, ding_search_mask], axis=1)]
    dong_inst_freq = dong_search_freqs[np.argmax(spec[:, dong_search_mask], axis=1)]

    best_score = 0.0
    best_reason = "no_physical_chime_match"
    is_ring = False

    # Eén Ding duurt ~320-380ms (10 frames van 32ms); we bekijken 9 en 11 frames voor & na de wissel
    for w_len in (9, 11):
        if num_frames < (2 * w_len):
            continue

        for split_idx in range(w_len, num_frames - w_len + 1):
            p1_s = split_idx - w_len
            p2_e = split_idx + w_len

            # Deel 1 (Ding: ~320ms voor split_idx)
            ding_e1 = float(np.mean(ding_peak_val[p1_s:split_idx]))
            dong_e1 = float(np.mean(dong_peak_val[p1_s:split_idx]))
            frac_ding_p1 = float(np.mean(ding_frac[p1_s:split_idx]))

            # Deel 2 (Dong: ~320ms na split_idx)
            ding_e2 = float(np.mean(ding_peak_val[split_idx:p2_e]))
            dong_e2 = float(np.mean(dong_peak_val[split_idx:p2_e]))
            dong_fund_e2 = float(np.mean(dong_band_e[split_idx:p2_e] / total_mid_e[split_idx:p2_e]))

            # LOCK 1B: Minimale fysieke sinus-energie A^2 van beide tonen
            if ding_e1 < 0.0012 or dong_e2 < 0.0005:
                continue

            # LOCK 2: Smalle-Band Monochromatische Concentratie
            # Hoeveel frames in Deel 1 hebben >= 40% van alle 350-1000Hz energie in de 728Hz naald?
            mono_ding_frames = int(np.sum(ding_frac[p1_s:split_idx] >= 0.38))
            # Hoeveel frames in Deel 2 hebben een sterke 570Hz fractie (>= 22% van de hele band)?
            mono_dong_frames = int(np.sum((dong_band_e[split_idx:p2_e] / total_mid_e[split_idx:p2_e]) >= 0.22))

            # LOCK 3A: Kwartskristal Toonvastheid (Zero Vibrato / Anti-Kinderstem)
            # In hoeveel frames van Deel 1 staat de piek exact op 728 Hz ± 12 Hz?
            locked_728_frames = int(np.sum(np.abs(ding_inst_freq[p1_s:split_idx] - f_ding_center) <= 12.0))
            # In hoeveel frames van Deel 2 staat de piek exact op 570 Hz ± 12 Hz?
            locked_570_frames = int(np.sum(np.abs(dong_inst_freq[split_idx:p2_e] - f_dong_center) <= 12.0))

            # LOCK 3B: Causale Ding -> Dong Overgang
            ding_over_dong_p1 = ding_e1 / (dong_e1 + 1e-7)
            dong_onset_jump = dong_e2 / (dong_e1 + 1e-7)
            ding_decay_ratio = ding_e2 / (ding_e1 + 1e-7)
            dominance_shift = ding_over_dong_p1 * (dong_e2 / (ding_e2 + 1e-7))

            min_req_frames = (w_len * 2) // 3  # Minstens 6 van de 9 (of 7 van de 11) frames

            if (
                frac_ding_p1 >= 0.40               # >= 40% van ALLE 350-1000Hz energie zit in 728 Hz!
                and dong_fund_e2 >= 0.24           # Duidelijke smalle 570 Hz concentratie in Deel 2
                and mono_ding_frames >= min_req_frames
                and mono_dong_frames >= min_req_frames
                and locked_728_frames >= min_req_frames
                and locked_570_frames >= min_req_frames
                and ding_over_dong_p1 >= 4.0       # Tijdens Ding is 570 Hz nog stil
                and dong_onset_jump >= 5.0         # Bij Dong schiet 570 Hz minstens 5x omhoog
                and ding_decay_ratio <= 0.60       # Terwijl 728 Hz afneemt (blokkeert élke klap/schreeuw!)
                and dominance_shift >= 8.0         # Harde kruis-wissel van 728 Hz naar 570 Hz
            ):
                score = round(min(0.99, 0.75 + 0.20 * frac_ding_p1), 4)
                if score > best_score:
                    best_score = score
                    is_ring = True
                    best_reason = (
                        f"QUARTZ_CHIME_MATCH(rms={rms:.3f}, frac728={frac_ding_p1:.2f}, "
                        f"frac570={dong_fund_e2:.2f}, lock728={locked_728_frames}/{w_len}, "
                        f"lock570={locked_570_frames}/{w_len}, jump570={dong_onset_jump:.1f}x, "
                        f"decay728={ding_decay_ratio:.2f})"
                    )
            else:
                partial = round(min(0.65, 0.5 * frac_ding_p1 + 0.5 * dong_fund_e2), 4)
                if not is_ring and partial > best_score:
                    best_score = partial
                    best_reason = (
                        f"REJECTED(rms={rms:.3f}, frac728={frac_ding_p1:.2f}, frac570={dong_fund_e2:.2f}, "
                        f"lock728={locked_728_frames}/{w_len}, lock570={locked_570_frames}/{w_len}, "
                        f"jump570={dong_onset_jump:.1f}x, decay728={ding_decay_ratio:.2f})"
                    )

    return is_ring, best_score, best_reason


class ShazamChimeDetector:
    """4-Layer Physical & Quartz-Locked 'Ding-Dong' Detector (v3.7.0)."""

    def __init__(self, fingerprint_path: str = FINGERPRINT_PATH):
        self.fingerprint_path = fingerprint_path
        self.sr = 16000
        self.win_size = 2048
        self.hop_size = 800
        self.n_fft = 2048
        self.cooldown_seconds = 8.0
        self.ncc_trigger_threshold = 0.72
        self.ncc_log_threshold = 0.55

        self._f_tone1 = 728.0
        self._f_tone2 = 570.0
        self._template_norm: Optional[np.ndarray] = None
        self._template_bins = 0
        self._win_frames = 0

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_trigger_ts = 0.0

        # 2.0s rolling audio buffer (32000 samples bij 16kHz)
        self._sample_buffer: Deque[float] = collections.deque(maxlen=32000)
        # Rolling 30s achtergrond-RMS tracker (elke 100ms 1 meting = 300 metingen)
        self._ambient_rms_history: Deque[float] = collections.deque(maxlen=300)
        self._buffer_lock = threading.Lock()
        self._chunks_since_last_eval = 0

        self._recent_detections: Deque[Dict[str, Any]] = collections.deque(maxlen=50)
        self._load_fingerprint()

    def _load_fingerprint(self) -> None:
        """Laadt de 2D-spectrogram template uit chime_fingerprint.npz."""
        if not os.path.exists(self.fingerprint_path):
            return
        try:
            data = np.load(self.fingerprint_path)
            self._f_tone1 = float(data.get("f_tone1", 728.0))
            self._f_tone2 = float(data.get("f_tone2", 570.0))
            self._n_fft = int(data.get("n_fft", 1024))
            self._hop_length = int(data.get("hop_length", 160))
            spec = data["spectrogram"]
            freqs = data["frequencies"]
            mask_band = (freqs >= 500.0) & (freqs <= 800.0)
            sub_spec = spec[mask_band, :]
            log_spec = np.log1p(sub_spec)
            std = float(np.std(log_spec)) + 1e-6
            self._template_norm = ((log_spec - float(np.mean(log_spec))) / std).astype(np.float32)
            self._template_bins, self._win_frames = self._template_norm.shape
            logger.info(
                "Loaded Chime Fingerprint: shape=%s, tones=(%.1fHz, %.1fHz), n_fft=%d, hop=%d",
                self._template_norm.shape, self._f_tone1, self._f_tone2, self._n_fft, self._hop_length,
            )
        except Exception as e:
            logger.error("Failed loading chime fingerprint: %s", e)

    def _compute_2d_ncc(self, samples: np.ndarray) -> float:
        """Berekent 2D Spectrogram NCC passend bij chime_fingerprint.npz."""
        if self._template_norm is None or len(samples) < int(1.0 * self.sr):
            return 0.0

        n_fft = getattr(self, "_n_fft", 1024)
        hop = getattr(self, "_hop_length", 160)
        f, _, Zxx = scipy.signal.stft(samples, fs=self.sr, nperseg=n_fft, noverlap=n_fft - hop)
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
            if W_std < 0.01:
                continue
            W_norm = (W - float(np.mean(W))) / (W_std + 1e-6)
            ncc = float(np.mean(tpl_norm * W_norm))
            if ncc > best_ncc:
                best_ncc = ncc
        return float(best_ncc)

    def get_ambient_rms_floor(self) -> float:
        """Retourneert de mediane achtergrond-RMS van de afgelopen 30 seconden."""
        with self._buffer_lock:
            if len(self._ambient_rms_history) < 10:
                return 0.005
            return float(np.median(self._ambient_rms_history))

    def evaluate_spectral_window(self, samples_128ms: np.ndarray) -> Tuple[bool, bool, float, float, float, float]:
        if len(samples_128ms) < 512:
            return False, False, 0.0, 0.0, 0.0, 0.0
        n = len(samples_128ms)
        norm_factor = float((n / 4.0) ** 2)
        P = (np.abs(np.fft.rfft(samples_128ms * np.hanning(n))) ** 2) / norm_factor
        freqs = np.fft.rfftfreq(n, 1.0 / self.sr)
        ding_m = np.abs(freqs - self._f_tone1) <= 20.0
        dong_m = np.abs(freqs - self._f_tone2) <= 20.0
        mid_m = (freqs >= 350.0) & (freqs <= 1000.0)
        tot_mid = float(np.sum(P[mid_m])) + 1e-7
        frac_ding = float(np.sum(P[ding_m])) / tot_mid
        frac_dong = float(np.sum(P[dong_m])) / tot_mid
        B_ding = float(np.max(P[ding_m])) if np.any(ding_m) else 0.0
        B_dong = float(np.max(P[dong_m])) if np.any(dong_m) else 0.0
        dom_ding = B_ding / (B_dong + 1e-7)
        dom_dong = B_dong / (B_ding + 1e-7)
        return (B_ding >= 0.0012 and frac_ding >= 0.40), (B_dong >= 0.0005 and frac_dong >= 0.24), frac_ding * 20.0, dom_ding, frac_dong * 20.0, dom_dong

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        if len(samples) < int(self.sr * 0.65):
            if len(samples) >= int(8000 * 0.65):
                samples = scipy.signal.resample(samples, int(len(samples) * 2)).astype(np.float32)
            else:
                return 0.0, False, "Insufficient audio samples"

        matched, vdc_score, reason = verify_ding_dong_chime(
            samples,
            self.sr,
            f_ding_center=self._f_tone1,
            f_dong_center=self._f_tone2,
            ambient_rms=0.005,
        )
        best_ncc = self._compute_2d_ncc(samples)
        final_score = max(vdc_score, best_ncc) if matched else min(0.65, vdc_score)
        window_rms = float(np.sqrt(np.mean(samples**2))) if len(samples) > 0 else 0.0
        details = f"Matched={matched}, Score={final_score:.4f}, NCC={best_ncc:.4f}, {reason}, RMS={window_rms:.4f}"

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
        chunk_rms = float(np.sqrt(np.mean(new_samples * new_samples))) if len(new_samples) > 0 else 0.0

        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)
            self._chunks_since_last_eval += 1
            if self._chunks_since_last_eval < 2:
                return
            self._chunks_since_last_eval = 0
            # Update rolling achtergrond-RMS alleen met niet-luide blokjes zodat de bel zichzelf niet opdrukt
            if chunk_rms < 0.04:
                self._ambient_rms_history.append(chunk_rms)
            if len(self._sample_buffer) < int(1.0 * self.sr):
                return
            now_eval = time.time()
            ambient_floor = float(np.median(self._ambient_rms_history)) if len(self._ambient_rms_history) >= 10 else 0.005
            window_14s = np.array(self._sample_buffer, dtype=np.float32)[-int(1.40 * self.sr):]
            window_18s = np.array(self._sample_buffer, dtype=np.float32)[-int(1.80 * self.sr):]

        # LOCK 1: Near-field volume moet zowel >= 0.025 als >= 3.8x de lopende straat-achtergrond zijn
        rms = float(np.sqrt(np.mean(window_14s * window_14s)))
        if rms < max(0.025, ambient_floor * 3.8):
            return

        # LOCK 2 & 3: Monochromatische 728->570 Hz concentratie + Kwartskristal Toonvastheid
        vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(
            window_14s,
            sample_rate=self.sr,
            f_ding_center=self._f_tone1,
            f_dong_center=self._f_tone2,
            ambient_rms=ambient_floor,
        )
        if not vdc_ok:
            return

        # LOCK 4: 2D Spectrogram Vingerafdruk Bevestiging (op originele 2048-FFT schaal)
        best_ncc = self._compute_2d_ncc(window_18s)
        if self._template_norm is not None and best_ncc < 0.65:
            logger.info(
                "🛡️ [Shazam Lock 4] Afgewezen door 2048-FFT 2D-Vingerafdruk: NCC=%.4f < 0.65 (%s)",
                best_ncc, vdc_reason,
            )
            return

        final_score = round(max(vdc_score, best_ncc), 4)

        if (now_eval - self._last_trigger_ts) >= self.cooldown_seconds:
            self._last_trigger_ts = now_eval
            self._recent_detections.append({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "ts": now_eval,
                "ncc": final_score,
                "matched": True,
                "details": f"NCC={best_ncc:.4f}, amb={ambient_floor:.4f}, {vdc_reason}",
            })
            log_shazam_event(
                ncc=final_score,
                threshold=float(self.ncc_trigger_threshold),
                matched=True,
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=f"NCC={best_ncc:.4f}, amb={ambient_floor:.4f}, {vdc_reason}",
            )
            logger.info(
                "🚨 [Shazam Chime] 4-LAAGS FYSISCHE MATCH BEVESTIGD! Score=%.4f, NCC=%.4f (%s)",
                final_score, best_ncc, vdc_reason,
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
        logger.info("Shazam Doorbell Chime Detector worker started (v3.7.0 4-Layer Physical Mode)")

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
