"""
Foxway Smart Doorbell — Door Watch AI Detection Service (door_service.py).
Supervises door_watch.py as an isolated background subprocess.
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
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger("foxway.door_service")
logger.setLevel(logging.INFO)


def _log_msg(msg: str) -> None:
    """Print to stdout and forward to standard logger."""
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{now_str}  foxway.door_service    INFO   {msg}", flush=True)
    logger.info(msg)


class DoorService:
    """
    Supervises door_watch.py as a standalone subprocess.
    Monitors process health, streams stdout/stderr lines to logger,
    and automatically restarts after 5s if terminated.
    """

    def __init__(
        self,
        rtsp_sub_url: Optional[str] = None,
        model_path: Optional[str] = None,
        zones_path: Optional[str] = None,
        webhook_url: Optional[str] = None,
        snap_dir: Optional[str] = None,
        fps: Optional[float] = None,
        imgsz: Optional[int] = None,
        threads: Optional[int] = None,
    ):
        self._rtsp_sub_url_arg = rtsp_sub_url
        self._model_path_arg = model_path
        self._zones_path_arg = zones_path
        self._webhook_url_arg = webhook_url
        self._snap_dir_arg = snap_dir
        self._fps_arg = fps
        self._imgsz_arg = imgsz
        self._threads_arg = threads

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._start_time: Optional[float] = None
        self._restarts: int = 0
        self._last_stats: str = ""
        self._last_event: Optional[Dict[str, Any]] = None
        self._last_event_time: Optional[str] = None
        self._last_motion_timestamp: float = 0.0
        self._lock = threading.Lock()

    @property
    def rtsp_sub_url(self) -> str:
        return (
            self._rtsp_sub_url_arg
            or os.environ.get("RTSP_SUB_URL")
            or getattr(settings, "RTSP_SUB_URL", None)
            or "rtsp://127.0.0.1:8554/deurbel_sub"
        )

    @property
    def model_path(self) -> str:
        if self._model_path_arg:
            return self._model_path_arg
        if os.environ.get("DOOR_MODEL_PATH"):
            return os.environ["DOOR_MODEL_PATH"]
        candidates = [
            "/mnt/opslag/deurbel/backend/app/assets/yolo11n.onnx",
            str(Path(__file__).parent / "assets" / "yolo11n.onnx"),
            "yolo11n.onnx",
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return "/mnt/opslag/deurbel/backend/app/assets/yolo11n.onnx"

    @property
    def zones_path(self) -> str:
        if self._zones_path_arg:
            return self._zones_path_arg
        if os.environ.get("DOOR_ZONES_PATH"):
            return os.environ["DOOR_ZONES_PATH"]
        candidates = [
            "/mnt/opslag/deurbel/backend/app/assets/zones.json",
            str(Path(__file__).parent / "assets" / "zones.json"),
            "zones.json",
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return "/mnt/opslag/deurbel/backend/app/assets/zones.json"

    @property
    def webhook_url(self) -> str:
        if self._webhook_url_arg:
            return self._webhook_url_arg
        if os.environ.get("DOOR_WEBHOOK_URL"):
            return os.environ["DOOR_WEBHOOK_URL"]
        port = getattr(settings, "HOST_PORT", 8090)
        return f"http://127.0.0.1:{port}/api/door-event"

    @property
    def snap_dir(self) -> str:
        if self._snap_dir_arg:
            return self._snap_dir_arg
        if os.environ.get("DOOR_SNAP_DIR"):
            return os.environ["DOOR_SNAP_DIR"]
        return str(getattr(settings, "SNAPSHOTS_DIR", "/mnt/opslag/deurbel/snapshots"))

    @property
    def fps(self) -> float:
        if self._fps_arg is not None:
            return float(self._fps_arg)
        return float(os.environ.get("DOOR_FPS", "3.0"))

    @property
    def imgsz(self) -> int:
        if self._imgsz_arg is not None:
            return int(self._imgsz_arg)
        return int(os.environ.get("DOOR_IMGSZ", "320"))

    @property
    def threads(self) -> int:
        if self._threads_arg is not None:
            return int(self._threads_arg)
        return int(os.environ.get("DOOR_THREADS", "2"))

    def _build_command(self) -> list[str]:
        script_candidates = [
            str(Path(__file__).parent / "door_watch.py"),
            "/mnt/opslag/deurbel/backend/app/door_watch.py",
        ]
        script_path = None
        for s in script_candidates:
            if os.path.exists(s):
                script_path = s
                break
        if not script_path:
            raise FileNotFoundError("door_watch.py script niet gevonden!")

        cmd = [
            sys.executable,
            "-u",
            script_path,
            "--rtsp", self.rtsp_sub_url,
            "--model", self.model_path,
            "--zones", self.zones_path,
            "--webhook", self.webhook_url,
            "--snap-dir", self.snap_dir,
            "--fps", str(self.fps),
            "--imgsz", str(self.imgsz),
            "--threads", str(self.threads),
        ]
        return cmd

    def _supervisor_loop(self) -> None:
        """Runs in dedicated background thread supervising door_watch.py."""
        _log_msg("DoorService supervisor gestart.")
        while self._running:
            try:
                cmd = self._build_command()
                _log_msg(f"Start door_watch.py: {' '.join(cmd)}")

                self._start_time = time.time()
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                _log_msg(f"✅ door_watch.py actief (PID: {self._process.pid})")

                # Stream stdout line-by-line
                assert self._process.stdout is not None
                for line in iter(self._process.stdout.readline, ""):
                    line = line.strip()
                    if not line:
                        continue
                    if "[stats]" in line:
                        with self._lock:
                            self._last_stats = line
                        _log_msg(line)
                    elif line.startswith("MELDING"):
                        with self._lock:
                            self._last_motion_timestamp = time.time()
                        _log_msg(f"🚨 {line}")
                    elif "fout" in line.lower() or "error" in line.lower():
                        logger.warning("[door_service] %s", line)
                    else:
                        logger.debug("[door_service] %s", line)

                    if not self._running:
                        break

                ret = self._process.wait()
                if not self._running:
                    break
                _log_msg(f"⚠️ door_watch.py gestopt met code {ret}. Herstart over 5 seconden...")
                with self._lock:
                    self._restarts += 1

            except Exception as e:
                logger.error("[door_service] Fout in supervisor loop: %s", e)

            # Wait 5 seconds before restarting
            for _ in range(50):
                if not self._running:
                    break
                time.sleep(0.1)

        _log_msg("DoorService supervisor beëindigd.")

    def record_event(self, event_data: Dict[str, Any]) -> None:
        """Update last recorded event and motion timestamp."""
        with self._lock:
            self._last_event = event_data
            self._last_event_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._last_motion_timestamp = time.time()

    def is_motion_active(self) -> bool:
        """Returns True if a motion alert arrived in the last 15 seconds."""
        with self._lock:
            return (time.time() - self._last_motion_timestamp) < 15.0

    def get_status(self) -> Dict[str, Any]:
        """Returns diagnostic status dict for endpoints."""
        with self._lock:
            alive = self._process is not None and self._process.poll() is None
            uptime = (time.time() - self._start_time) if (alive and self._start_time) else 0.0
            return {
                "running": alive,
                "pid": self._process.pid if (alive and self._process) else None,
                "uptime_seconds": round(uptime, 1),
                "restarts": self._restarts,
                "last_stats": self._last_stats,
                "last_event": self._last_event,
                "last_event_time": self._last_event_time,
                "config": {
                    "rtsp_sub_url": self.rtsp_sub_url,
                    "model_path": self.model_path,
                    "zones_path": self.zones_path,
                    "webhook_url": self.webhook_url,
                    "snap_dir": self.snap_dir,
                    "fps": self.fps,
                    "imgsz": self.imgsz,
                    "threads": self.threads,
                },
            }

    async def start(self) -> None:
        """Start the supervisor thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._supervisor_loop,
            daemon=True,
            name="DoorWatchSupervisor",
        )
        self._thread.start()

    async def stop(self) -> None:
        """Stop the supervisor and terminate door_watch.py."""
        self._running = False
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._wait_process)
            except Exception as e:
                logger.warning("[door_service] Fout bij stoppen process: %s", e)

    def _wait_process(self) -> None:
        if self._process:
            try:
                self._process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._process.kill()


door_service = DoorService()
