"""
Foxway Smart Doorbell — Doorbell Audio Ingest & Detection Service (bell_service.py).
Integrates the new doorbell_detector.py with the backend pipeline.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from app.config import settings
from app.doorbell_detector import SR, Detector, build_template

logger = logging.getLogger("foxway.bell_service")
logger.setLevel(logging.INFO)


def _log_msg(msg: str) -> None:
    """Print to stdout and forward to standard logger."""
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{now_str}  foxway.bell_service     INFO   {msg}", flush=True)
    logger.info(msg)


def resolve_template_path() -> str:
    """Finds the ding-dong reference video/audio file."""
    candidates = [
        getattr(settings, "BELL_REF_PATH", None),
        os.environ.get("BELL_REF_PATH", None),
        "/mnt/opslag/deurbel/backend/app/assets/deurbel_ding-dong_ezviz-app.wav",
        "/mnt/opslag/deurbel/claude/deurbel_ding-dong_ezviz-app.wav",
        str(Path(__file__).parent / "assets" / "deurbel_ding-dong_ezviz-app.wav"),
        "/mnt/opslag/deurbel/backend/app/assets/bel.mp4",
        "/mnt/opslag/deurbel/claude/bel.mp4",
        str(Path(__file__).parent / "assets" / "bel.mp4"),
        str(Path(__file__).parent.parent / "claude" / "bel.mp4"),
        "bel.mp4",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return str(c)
    raise FileNotFoundError("Deurbel referentiebestand (deurbel_ding-dong_ezviz-app.wav of bel.mp4) niet gevonden!")


class BellService:
    """
    Background service that streams doorbell microphone audio via RTSP,
    feeds 1600 float32 samples to the Detector from doorbell_detector.py,
    and dispatches ring events to the app state machine.
    """

    def __init__(
        self,
        ref_path: Optional[str] = None,
        threshold: Optional[float] = None,
        cooldown: Optional[float] = None,
        rtsp_url: Optional[str] = None,
    ):
        self._ref_path_arg = ref_path
        self._threshold_arg = threshold
        self._cooldown_arg = cooldown
        self._rtsp_url_arg = rtsp_url

        self.template: Optional[np.ndarray] = None
        self.detector: Optional[Detector] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._last_heartbeat: float = 0.0
        self._last_heartbeat_best: float = 0.0
        self._recent_detections: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def ref_path(self) -> str:
        return self._ref_path_arg or getattr(settings, "BELL_REF_PATH", None) or resolve_template_path()

    @property
    def threshold(self) -> float:
        if self._threshold_arg is not None:
            return float(self._threshold_arg)
        return float(getattr(settings, "DOORBELL_THRESHOLD", 0.30))

    @property
    def cooldown(self) -> float:
        if self._cooldown_arg is not None:
            return float(self._cooldown_arg)
        return float(getattr(settings, "DOORBELL_COOLDOWN", 8.0))

    @property
    def rtsp_url(self) -> str:
        return self._rtsp_url_arg or os.environ.get("RTSP_URL") or getattr(settings, "RTSP_URL", "rtsp://127.0.0.1:8554/deurbel")

    def _ensure_template(self) -> None:
        if self.template is None:
            path = self.ref_path
            _log_msg(f"Bouw deurbelsjabloon van referentie: {path}...")
            self.template = build_template(path)
            if hasattr(self.template, "shape"):
                duration_s = self.template.shape[0] * 256 / SR
                _log_msg(f"✅ Deurbelsjabloon succesvol gebouwd (duur: {duration_s:.2f}s, vorm: {self.template.shape})")
            else:
                _log_msg(f"✅ Deurbelsjabloon succesvol gebouwd (v2 Params regel-detector)")

    def _worker(self) -> None:
        """Background thread executing ffmpeg audio-read loop."""
        try:
            self._ensure_template()
        except Exception as e:
            _log_msg(f"❌ [bell_service] Kon sjabloon niet bouwen: {e}. Luisterthread gestopt.")
            return

        self.detector = Detector(self.template, threshold=self.threshold, cooldown=self.cooldown)
        self._last_heartbeat = time.time()

        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vn",
            "-ac", "1",
            "-ar", str(SR),
            "-f", "f32le",
            "-",
        ]

        _log_msg(f"✅ [bell_service] Luisterthread gestart (RTSP={self.rtsp_url}, drempel={self.threshold:.2f}, cooldown={self.cooldown:.1f}s)")

        bytes_per_chunk = 1600 * 4  # 1600 samples * 4 bytes/float32 = 6400 bytes

        while self._running:
            try:
                _log_msg(f"[bell_service] Start ffmpeg RTSP audio ingest: {self.rtsp_url}")
                self._process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

                while self._running and self._process.poll() is None:
                    raw = bytearray()
                    while len(raw) < bytes_per_chunk and self._running and self._process.poll() is None:
                        part = self._process.stdout.read(bytes_per_chunk - len(raw))
                        if not part:
                            break
                        raw.extend(part)

                    if len(raw) < bytes_per_chunk:
                        break

                    samples = np.frombuffer(raw, dtype=np.float32)
                    events = self.detector.feed(samples)

                    for t, s in events:
                        self._on_doorbell_ring(t, s)

                    # Eens per minuut logging "luistert nog" met de hoogste score van die minuut
                    now = time.time()
                    if (now - self._last_heartbeat) >= 60.0:
                        highest = float(self.detector.best)
                        self._last_heartbeat_best = highest
                        self.detector.best = 0.0
                        self._last_heartbeat = now
                        _log_msg(f"👂 [bell_service] luistert nog — hoogste score afgelopen minuut: {highest:.4f} (drempel: {self.threshold:.2f})")

            except Exception as e:
                _log_msg(f"❌ [bell_service] Fout in ffmpeg audio stream loop: {e}")
            finally:
                if self._process:
                    try:
                        self._process.kill()
                        self._process.wait(timeout=2)
                    except Exception:
                        pass
                    self._process = None

            if self._running:
                _log_msg("⚠️ [bell_service] Stream weg/gestopt, 5 seconden wachten en opnieuw proberen...")
                time.sleep(5)

    def _on_doorbell_ring(self, t: float, score: float) -> None:
        now_dt = datetime.datetime.now()
        iso_ts = now_dt.isoformat()
        _log_msg(f"🔔 [bell_service] AANGEBELD! tijdstip={t:.2f}s ({iso_ts}), score={score:.4f} (drempel={self.threshold:.2f})")

        with self._lock:
            self._recent_detections.append({
                "time_s": round(t, 2),
                "timestamp": iso_ts,
                "score": round(score, 4),
                "threshold": self.threshold,
            })
            if len(self._recent_detections) > 50:
                self._recent_detections.pop(0)

        # Optionele black-box event logging
        try:
            from app.logger import log_shazam_event
            log_shazam_event(
                ncc=score,
                threshold=self.threshold,
                matched=True,
                f_tone1=727.0,
                f_tone2=562.0,
                details=f"doorbell_detector score={score:.4f} at t={t:.2f}s",
            )
        except Exception:
            pass

        # Roep bestaande aanbel-actie aan (zowel ring_coordinator als call_sm.ring)
        self._dispatch_existing_ring_action(score, t)

    def _dispatch_existing_ring_action(self, score: float, t: float) -> None:
        """Dispatches the ring event to the existing call state machine and notification pipeline."""
        # 1. Via ring_coordinator
        try:
            from app.ring_coordinator import register_ring_signal
            register_ring_signal(
                source="SHAZAM_CHIME",
                details={"score": score, "time": t, "reason": f"doorbell_detector score={score:.4f}"},
            )
        except Exception as e:
            logger.warning("[bell_service] register_ring_signal waarschuwing: %s", e)

        # 2. Directe state-machine aanroep voor 100% gegarandeerde notificatie/oproep
        if self._loop and self._loop.is_running():
            try:
                from app.main import call_sm
                asyncio.run_coroutine_threadsafe(
                    call_sm.ring(trigger_source="SHAZAM_CHIME"),
                    self._loop,
                )
            except Exception as e:
                logger.error("[bell_service] Fout bij aanroepen call_sm.ring: %s", e)

    async def start(self) -> None:
        """Start the background listening thread."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._worker, name="BellDetectorThread", daemon=True)
        self._thread.start()
        _log_msg("BellService gestart op event loop")

    async def stop(self) -> None:
        """Stop background worker and terminate ffmpeg subprocess."""
        self._running = False
        if self._process:
            try:
                self._process.kill()
            except Exception:
                pass
            self._process = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        _log_msg("BellService gestopt")

    def get_status(self) -> Dict[str, Any]:
        """Provides status diagnostics for the listening thread and detector."""
        thread_alive = bool(self._thread and self._thread.is_alive())
        process_running = bool(self._process and self._process.poll() is None)
        with self._lock:
            recent = list(reversed(self._recent_detections))
        return {
            "running": self._running,
            "thread_alive": thread_alive,
            "process_running": process_running,
            "rtsp_url": self.rtsp_url,
            "threshold": self.threshold,
            "cooldown": self.cooldown,
            "current_best": float(self.detector.best) if self.detector else 0.0,
            "last_heartbeat_best": float(self._last_heartbeat_best),
            "template_loaded": self.template is not None,
            "recent_detections": recent,
        }

    # Backward-compatibility properties for existing admin/monitor endpoints
    @property
    def ncc_trigger_threshold(self) -> float:
        return self.threshold

    @property
    def fingerprint_path(self) -> str:
        return self.ref_path

    def get_recent_detections(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._recent_detections))


bell_service = BellService()
