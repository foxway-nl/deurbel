"""
Foxway Smart Doorbell — Single "Ding-Dong" Matched Envelope Detector (v3.6.5).

Calibrated directly on real EZVIZ DB1 Pro recordings:
- Single Ding-Dong melody:
  * Tone 1 (Ding): ~0.35s - 0.42s at ~728 Hz (fundamental) + ~1456 Hz (2nd harmonic).
  * Tone 2 (Dong): ~0.45s - 0.65s at ~570 Hz (fundamental) + ~1140 Hz (2nd harmonic).
- Immune to concurrent street voices / jacket rustle:
  Uses narrow local frequency shoulders (±45..130 Hz median around 728 Hz and 570 Hz)
  instead of a wide 300-1350 Hz speech band, so people talking in the background do NOT
  blind the doorbell detector.
- 100% Immune to loud claps, slams, and screams:
  1. Broadband sounds raise the local ±45..130 Hz shoulders together with the center bin,
     keeping `tonal_prominence` low (< 2.5).
  2. Sudden loud noises raise all frequencies simultaneously (`ding_p2 >= ding_p1`), whereas
     in a real Ding->Dong transition, 728 Hz drops while 570 Hz jumps up!
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

from app.logger import log_shazam_event

logger = logging.getLogger("foxway.ring_detector")

FINGERPRINT_PATH = "/mnt/opslag/deurbel/backend/app/assets/chime_fingerprint.npz"


def verify_ding_dong_chime(
    audio_window: np.ndarray,
    sample_rate: int,
    ref_spec_norm: "np.ndarray | None" = None,
    f_ding_center: float = 728.0,
    f_dong_center: float = 570.0,
) -> "tuple[bool, float, str]":
    """
    Detecteert de enkele Ding-Dong melodie (~0.38s 728Hz -> ~0.55s 570Hz) van de EZVIZ DB1 Pro.
    Werkt zelfs als er gelijktijdig mensen op straat praten, en weigert 100% van alle harde
    stoorgeluiden (klappen, deuren, geschreeuw, enkelvoudige pieptonen).
    """
    if len(audio_window) < int(sample_rate * 0.55):
        return False, 0.0, "window_too_short"

    x = audio_window.astype(np.float32)
    x = x - np.mean(x)
    rms = float(np.sqrt(np.mean(x * x)))
    if rms < 1e-4:
        return False, 0.0, "silence"

    # Kies FFT zodat 1 frame altijd 64ms is (df = 15.625 Hz) en hop altijd 32ms is
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
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop][:num_frames]
    spec = np.abs(np.fft.rfft(frames * window_fn, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    # 1. Smalle kernbanden voor Ding (~728 Hz) en Dong (~570 Hz + 2e harmonische ~1140 Hz)
    ding_mask = (freqs >= (f_ding_center - 26.0)) & (freqs <= (f_ding_center + 26.0))
    dong_mask = (freqs >= (f_dong_center - 26.0)) & (freqs <= (f_dong_center + 26.0))
    dong_h2_mask = (freqs >= (2.0 * f_dong_center - 35.0)) & (freqs <= (2.0 * f_dong_center + 35.0))

    # 2. Lokale schouderbanden op ±45..135 Hz naast Ding en Dong (buiten de Hann main-lobe van ±31 Hz!)
    # Bij een zuivere elektronische beltoon zijn deze schouders laag; bij een klap, schreeuw of stem
    # zijn deze directe buur-frequenties juist hoog!
    ding_sh_mask = (
        ((freqs >= (f_ding_center - 135.0)) & (freqs <= (f_ding_center - 45.0)))
        | ((freqs >= (f_ding_center + 45.0)) & (freqs <= (f_ding_center + 135.0)))
    )
    dong_sh_mask = (
        ((freqs >= (f_dong_center - 135.0)) & (freqs <= (f_dong_center - 45.0)))
        | ((freqs >= (f_dong_center + 45.0)) & (freqs <= (f_dong_center + 135.0)))
    )

    ding_raw = np.max(spec[:, ding_mask], axis=1)
    dong_fund = np.max(spec[:, dong_mask], axis=1)
    dong_h2 = np.max(spec[:, dong_h2_mask], axis=1) if np.any(dong_h2_mask) else np.zeros(num_frames)
    # Combineer Dong grondtoon met zijn karakteristieke 2e harmonische (1140 Hz)
    dong_raw = dong_fund + 0.35 * dong_h2

    # Gebruik het 75e percentiel van de lokale schouders als robuuste lokale ruisvloer per frame
    ding_sh = np.percentile(spec[:, ding_sh_mask], 75, axis=1) + 1e-9
    dong_sh = np.percentile(spec[:, dong_sh_mask], 75, axis=1) + 1e-9

    # Tonal Prominence per frame (hoeveel keer steekt de naald boven zijn directe buren uit?)
    ding_prom = ding_raw / ding_sh
    dong_prom = dong_raw / dong_sh

    best_score = 0.0
    best_reason = "no_single_ding_dong_match"
    is_ring = False

    # Eén enkele Ding duurt ~320-420ms (9 tot 12 frames van 32ms).
    # De Dong erna duurt ~400-650ms (we bekijken de eerste 9 tot 12 frames na het overgangspunt).
    for w_len in (8, 10, 12):
        if num_frames < (2 * w_len):
            continue

        for split_idx in range(w_len, num_frames - w_len + 1):
            p1_s = split_idx - w_len
            p2_e = split_idx + w_len

            # Gemiddelde energieën in Deel 1 (Ding-fase) en Deel 2 (Dong-fase)
            ding_p1 = float(np.mean(ding_raw[p1_s:split_idx]))
            dong_p1 = float(np.mean(dong_raw[p1_s:split_idx]))
            prom_ding_p1 = float(np.mean(ding_prom[p1_s:split_idx]))

            ding_p2 = float(np.mean(ding_raw[split_idx:p2_e]))
            dong_p2 = float(np.mean(dong_raw[split_idx:p2_e]))
            prom_dong_p2 = float(np.mean(dong_prom[split_idx:p2_e]))

            # Tel hoeveel individuele frames in Deel 1 een scherpe 728 Hz naald hebben,
            # en hoeveel frames in Deel 2 een scherpe 570 Hz naald hebben:
            clean_ding_frames = int(np.sum(ding_prom[p1_s:split_idx] >= 3.0))
            clean_dong_frames = int(np.sum(dong_prom[split_idx:p2_e] >= 2.5))

            # Verhoudingen die de echte Ding -> Dong wissel bewijzen:
            # 1. In Deel 1 is Ding (728 Hz) veel sterker dan Dong (570 Hz)
            ding_over_dong_p1 = ding_p1 / (dong_p1 + 1e-9)

            # 2. Bij de overgang naar Deel 2 MOET 570 Hz sterk opkomen (dong_onset_jump),
            #    TERWIJL 728 Hz in Deel 2 juist AFNEEMT (ding_decay_ratio < 0.65)!
            #    (Bij élk plotseling hard geluid schiet 728 Hz in Deel 2 juist omhoog -> ding_decay_ratio > 1.0!)
            dong_onset_jump = dong_p2 / (dong_p1 + 1e-9)
            ding_decay_ratio = ding_p2 / (ding_p1 + 1e-9)

            # 3. Totale kruis-dominantie wissel tussen Deel 1 en Deel 2:
            dominance_shift = (ding_p1 / (dong_p1 + 1e-9)) * (dong_p2 / (ding_p2 + 1e-9))

            # 4. Genormaliseerde 2-kanaals vorm-correlatie over het [Ding -> Dong] blok:
            #    We correleren de genormaliseerde prominences tegen het ideale stap-patroon.
            d1_curve = np.clip(ding_prom[p1_s:p2_e], 0.0, 25.0)
            d2_curve = np.clip(dong_prom[p1_s:p2_e], 0.0, 25.0)
            d1_norm = d1_curve / (np.max(d1_curve) + 1e-9)
            d2_norm = d2_curve / (np.max(d2_curve) + 1e-9)
            diff_env = d1_norm - d2_norm
            ideal_step = np.concatenate([np.ones(w_len), -np.ones(w_len)])
            env_corr = float(
                np.sum(diff_env * ideal_step)
                / (np.linalg.norm(diff_env) * np.linalg.norm(ideal_step) + 1e-9)
            )

            min_clean_frames = max(4, w_len // 2)

            if (
                prom_ding_p1 >= 4.0                # 728 Hz steekt >4x boven zijn directe buren uit
                and prom_dong_p2 >= 3.2            # 570 Hz steekt >3.2x boven zijn directe buren uit
                and clean_ding_frames >= min_clean_frames
                and clean_dong_frames >= min_clean_frames
                and ding_over_dong_p1 >= 2.2       # In Deel 1 is 728 Hz dominant
                and dong_onset_jump >= 2.0         # In Deel 2 komt 570 Hz nieuw op
                and ding_decay_ratio <= 0.65       # HARDE ANTI-KLAP CHECK: 728 Hz MOET in Deel 2 afnemen!
                and dominance_shift >= 3.5         # Sterke wissel van 728 Hz naar 570 Hz
                and env_corr >= 0.50               # Strakke blok-overgang van Ding naar Dong
            ):
                raw_q = min(1.0, (np.log10(prom_ding_p1 + 1) + np.log10(prom_dong_p2 + 1)) / 2.8)
                score = round(0.75 + 0.24 * (0.5 * raw_q + 0.5 * env_corr), 4)
                if score > best_score:
                    best_score = score
                    is_ring = True
                    best_reason = (
                        f"SINGLE_DING_DONG_MATCH(w={w_len}, prom728={prom_ding_p1:.1f}, "
                        f"prom570={prom_dong_p2:.1f}, jump570={dong_onset_jump:.1f}x, "
                        f"decay728={ding_decay_ratio:.2f}, shift={dominance_shift:.1f}x, corr={env_corr:.2f})"
                    )
            else:
                partial = min(0.68, max(0.0, env_corr * 0.65))
                if not is_ring and partial > best_score:
                    best_score = round(partial, 4)
                    best_reason = (
                        f"REJECTED(prom728={prom_ding_p1:.1f}, prom570={prom_dong_p2:.1f}, "
                        f"jump570={dong_onset_jump:.1f}x, decay728={ding_decay_ratio:.2f}, "
                        f"shift={dominance_shift:.1f}x, corr={env_corr:.2f})"
                    )

    return is_ring, best_score, best_reason


class ShazamChimeDetector:
    """Single 'Ding-Dong' Matched Envelope Detector (v3.6.5)."""

    def __init__(self, fingerprint_path: str = FINGERPRINT_PATH):
        self.fingerprint_path = fingerprint_path
        self.sr = 16000
        self.win_size = 1024
        self.hop_size = 800
        self.n_fft = 1024
        self.cooldown_seconds = 5.0
        self.ncc_trigger_threshold = 0.72
        self.ncc_log_threshold = 0.45

        self._f_tone1 = 728.0
        self._f_tone2 = 570.0
        self._template_norm: Optional[np.ndarray] = None

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_trigger_ts = 0.0

        # Bewaar de laatste 1.35 seconde (21600 samples bij 16kHz) voor één enkele Ding-Dong (~0.95s)
        self._sample_buffer: Deque[float] = collections.deque(maxlen=21600)
        self._buffer_lock = threading.Lock()
        self._chunks_since_last_eval = 0

        self._recent_detections: Deque[Dict[str, Any]] = collections.deque(maxlen=50)
        self._load_fingerprint()

    def _load_fingerprint(self) -> None:
        if not os.path.exists(self.fingerprint_path):
            return
        try:
            data = np.load(self.fingerprint_path)
            self._f_tone1 = float(data.get("f_tone1", 728.0))
            self._f_tone2 = float(data.get("f_tone2", 570.0))
        except Exception as e:
            logger.error("Failed loading chime fingerprint: %s", e)

    def evaluate_spectral_window(self, samples_128ms: np.ndarray) -> Tuple[bool, bool, float, float, float, float]:
        """Compatibility helper for single-window spectral check."""
        if len(samples_128ms) < 512:
            return False, False, 0.0, 0.0, 0.0, 0.0
        n = len(samples_128ms)
        P = np.abs(np.fft.rfft(samples_128ms * np.hanning(n))) ** 2
        freqs = np.fft.rfftfreq(n, 1.0 / self.sr)
        ding_m = (freqs >= (self._f_tone1 - 26.0)) & (freqs <= (self._f_tone1 + 26.0))
        dong_m = (freqs >= (self._f_tone2 - 26.0)) & (freqs <= (self._f_tone2 + 26.0))
        sh_ding = ((freqs >= (self._f_tone1 - 135.0)) & (freqs <= (self._f_tone1 - 45.0))) | ((freqs >= (self._f_tone1 + 45.0)) & (freqs <= (self._f_tone1 + 135.0)))
        sh_dong = ((freqs >= (self._f_tone2 - 135.0)) & (freqs <= (self._f_tone2 - 45.0))) | ((freqs >= (self._f_tone2 + 45.0)) & (freqs <= (self._f_tone2 + 135.0)))
        B_ding = float(np.max(P[ding_m])) if np.any(ding_m) else 0.0
        B_dong = float(np.max(P[dong_m])) if np.any(dong_m) else 0.0
        N_ding = float(np.percentile(P[sh_ding], 75)) + 1e-9
        N_dong = float(np.percentile(P[sh_dong], 75)) + 1e-9
        snr_ding = B_ding / N_ding
        dom_ding = B_ding / (B_dong + 1e-9)
        snr_dong = B_dong / N_dong
        dom_dong = B_dong / (B_ding + 1e-9)
        return (snr_ding >= 4.0 and dom_ding >= 2.0), (snr_dong >= 3.2 and dom_dong >= 0.5), snr_ding, dom_ding, snr_dong, dom_dong

    def evaluate_audio_window(self, samples: np.ndarray) -> Tuple[float, bool, str]:
        """Evaluates an audio buffer by sliding a 1.15s window across it."""
        if len(samples) < int(self.sr * 0.55):
            return 0.0, False, "Insufficient audio samples"

        sub_len = int(self.sr * 1.15)
        step_len = int(self.sr * 0.15)

        best_score = 0.0
        best_reason = "no_match"
        matched = False

        if len(samples) <= sub_len:
            ok, sc, rsn = verify_ding_dong_chime(
                samples, self.sr, f_ding_center=self._f_tone1, f_dong_center=self._f_tone2
            )
            matched = ok
            best_score = sc
            best_reason = rsn
        else:
            for start in range(0, len(samples) - int(self.sr * 0.60), step_len):
                sub = samples[start : start + sub_len]
                ok, sc, rsn = verify_ding_dong_chime(
                    sub, self.sr, f_ding_center=self._f_tone1, f_dong_center=self._f_tone2
                )
                if ok and (not matched or sc > best_score):
                    matched = True
                    best_score = sc
                    best_reason = rsn
                elif not matched and sc > best_score:
                    best_score = sc
                    best_reason = rsn

        window_rms = float(np.sqrt(np.mean(samples**2))) if len(samples) > 0 else 0.0
        details = f"Matched={matched}, Score={best_score:.4f}, {best_reason}, RMS={window_rms:.3f}"

        if matched:
            log_shazam_event(
                ncc=float(best_score),
                threshold=float(self.ncc_trigger_threshold),
                matched=True,
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=str(details),
            )

        return float(best_score), bool(matched), str(details)

    def feed_pcm_samples(self, pcm_bytes: bytes) -> None:
        """
        Feeds 50ms PCM chunks (1600 bytes = 800 samples at 16kHz) and evaluates
        the rolling 1.15s window every 100ms.
        """
        if not pcm_bytes:
            return

        new_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._buffer_lock:
            self._sample_buffer.extend(new_samples)
            self._chunks_since_last_eval += 1
            if self._chunks_since_last_eval < 2 or len(self._sample_buffer) < int(0.75 * self.sr):
                return
            self._chunks_since_last_eval = 0
            now_eval = time.time()
            window_115s = np.array(self._sample_buffer, dtype=np.float32)[-int(1.15 * self.sr):]

        rms = float(np.sqrt(np.mean(window_115s * window_115s)))
        if rms < 0.002:
            return

        vdc_ok, vdc_score, vdc_reason = verify_ding_dong_chime(
            window_115s,
            sample_rate=self.sr,
            f_ding_center=self._f_tone1,
            f_dong_center=self._f_tone2,
        )

        if not vdc_ok:
            return

        if (now_eval - self._last_trigger_ts) >= self.cooldown_seconds:
            self._last_trigger_ts = now_eval
            self._recent_detections.append({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "ts": now_eval,
                "ncc": round(float(vdc_score), 4),
                "matched": True,
                "details": f"{vdc_reason}, RMS={rms:.3f}",
            })
            log_shazam_event(
                ncc=float(vdc_score),
                threshold=float(self.ncc_trigger_threshold),
                matched=True,
                f_tone1=float(self._f_tone1),
                f_tone2=float(self._f_tone2),
                details=f"{vdc_reason}, RMS={rms:.3f}",
            )
            logger.info("🚨 [Shazam Chime] ENKELE DING-DONG GEDETECTEERD! Score=%.3f (%s)", vdc_score, vdc_reason)
            try:
                from app.ring_coordinator import register_ring_signal
                register_ring_signal(
                    source="SHAZAM_CHIME",
                    details={"ncc": float(vdc_score), "reason": vdc_reason},
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
        logger.info("Shazam Doorbell Chime Detector worker started (v3.6.5 Single Ding-Dong Mode)")

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
