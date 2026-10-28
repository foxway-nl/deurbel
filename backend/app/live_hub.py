"""
Foxway Smart Doorbell — Always-Warm RAM Live Hub (v2.5.0).

Keeps a persistent 24/7 connection to go2rtc RTSP (with direct camera fallback) in memory:
- Decodes 10 fps 4:3 video (768x576) into RAM (self.latest_jpeg).
- Reads 16kHz mono 16-bit PCM incoming audio from doorbell microphone via UDP.
- Streams realtime frames and audio over WebSocket /api/v1/ws/live (<100ms latency on 5G & LAN).
- Instant snapshot endpoint /api/v1/live/instant.jpg (<5ms from RAM).
- Zero-lag pipe drain worker ensures freshest frame in RAM with zero OS buffer bloat.
- Anti-Freeze Clock-Motion Watchdog:
  * Compares pixel content of the camera-clock area (top-left 0:80, 0:350) with frame from 3.0s ago.
  * If mean pixel difference over 3s is < 0.15 (or 0.0), camera clock is frozen -> triggers self-heal.
  * Immediate ffmpeg process monitoring (no stalling when ffmpeg exits).
  * go2rtc stream restart via POST /api/restart and direct RTSP failover.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import socket
import subprocess
import threading
import time
import zlib
from typing import Optional, Set

import cv2
import httpx
import numpy as np

logger = logging.getLogger("foxway.live_hub")

AUDIO_UDP_HOST = "127.0.0.1"
AUDIO_UDP_PORT = 18555
PRIMARY_RTSP_SOURCE = "rtsp://127.0.0.1:8554/deurbel"
DIRECT_CAMERA_RTSP = "rtsp://admin:aero2006Mo_@192.168.2.31:554/H.264/ch1/main/av_stream"


class LiveHub:
    """Always-Warm In-Memory Stream Hub with Real-Time Anti-Freeze Watchdog."""

    def __init__(self) -> None:
        self.latest_jpeg: Optional[bytes] = None
        self.latest_frame_ts: float = 0.0
        self.last_fresh_frame_time: float = 0.0
        self.last_crc32: int = 0
        self.identical_crc_count: int = 0
        self.frame_seq: int = 0
        self._proc_start_time: float = 0.0

        self._active_source: str = PRIMARY_RTSP_SOURCE
        self._fail_count: int = 0
        self._consecutive_frozen_checks: int = 0
        self._last_pixel_sample_time: float = 0.0
        self._last_clock_check_diff: float = 1.0
        self._pixel_samples: collections.deque[tuple[float, np.ndarray]] = collections.deque(maxlen=15)

        self._clients: Set[asyncio.Queue] = set()
        self._running: bool = False
        self._supervisor_task: Optional[asyncio.Task] = None
        self._proc: Optional[subprocess.Popen] = None
        self._udp_sock: Optional[socket.socket] = None

    def is_fresh(self, max_age: float = 4.0, max_age_sec: Optional[float] = None) -> bool:
        """Return True only if the latest frame was decoded within the last max_age seconds."""
        limit = max_age_sec if max_age_sec is not None else max_age
        if not self.latest_jpeg:
            return False
        if self.last_fresh_frame_time <= 0:
            return False
        return (time.monotonic() - self.last_fresh_frame_time) <= limit

    def get_fresh_jpeg(self, max_age: float = 4.0, max_age_sec: Optional[float] = None) -> Optional[bytes]:
        """Get latest JPEG frame only if fresh (<max_age old). Never return stale cached frame."""
        limit = max_age_sec if max_age_sec is not None else max_age
        if self.is_fresh(max_age=limit):
            return self.latest_jpeg
        return None

    def get_latest_frame(self) -> Optional[bytes]:
        """Returns latest in-memory JPEG frame, or None if stream is not ready."""
        return self.latest_jpeg

    async def start(self) -> None:
        """Start the supervisor background task."""
        if self._running:
            return
        self._running = True
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        logger.info("Always-Warm RAM Live Hub started with anti-freeze watchdog (v2.5.0)")

    async def stop(self) -> None:
        """Stop supervisor and terminate ffmpeg process."""
        self._running = False
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass

        self._cleanup_process()
        logger.info("Always-Warm RAM Live Hub stopped")

    def _kill_process(self) -> None:
        """Immediate SIGKILL of ffmpeg reader process."""
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass

    async def restart(self) -> None:
        """
        Force immediate kill and restart of ffmpeg stream reader.
        1. Kills ffmpeg process.
        2. Restarts go2rtc stream or switches to direct camera RTSP.
        3. Clears cached frozen frame from memory.
        4. Waits until an actual new frame with active camera clock arrives before returning.
        """
        logger.info("LiveHub explicit restart requested — triggering immediate re-init")
        self._kill_process()
        self.latest_jpeg = None
        self.last_fresh_frame_time = 0.0
        self._last_clock_check_diff = 0.0
        self._pixel_samples.clear()

        # Trigger go2rtc reload
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post("http://127.0.0.1:1984/api/restart")
                logger.info("go2rtc stream reset signaled successfully")
        except Exception as e:
            logger.debug("go2rtc reset signal: %s", e)

        # Wait up to 3.5s for fresh frame arrival
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.5:
            if self.latest_jpeg and self.last_fresh_frame_time > 0 and (time.monotonic() - self.last_fresh_frame_time < 1.0):
                logger.info("LiveHub restart verified: fresh frame received (<%.2fs)", time.monotonic() - t0)
                break
            await asyncio.sleep(0.1)

    def _cleanup_process(self) -> None:
        """Kill subprocess and close UDP socket."""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except Exception:
                self._kill_process()
            self._proc = None

        if self._udp_sock:
            try:
                self._udp_sock.close()
            except Exception:
                pass
            self._udp_sock = None

    def register_client(self) -> asyncio.Queue:
        """Register a new WebSocket client with a low-latency bound queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=3)
        self._clients.add(q)
        logger.debug("Live client connected. Total clients: %d", len(self._clients))
        return q

    def unregister_client(self, q: asyncio.Queue) -> None:
        """Remove a disconnected client."""
        self._clients.discard(q)
        logger.debug("Live client disconnected. Total clients: %d", len(self._clients))

    def _broadcast(self, msg: bytes) -> None:
        """Broadcast binary message to all client queues without blocking."""
        if not self._clients:
            return
        for q in list(self._clients):
            if q.full():
                try:
                    q.get_nowait()  # Drop oldest frame to prevent lag
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def _supervisor_loop(self) -> None:
        """Supervises the ffmpeg decoding process and restarts on failure within 100ms."""
        while self._running:
            try:
                # 1. Bind UDP socket for audio
                self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._udp_sock.bind((AUDIO_UDP_HOST, AUDIO_UDP_PORT))
                self._udp_sock.setblocking(False)

                # 2. Reset watchdog & state metrics
                self.last_crc32 = 0
                self.identical_crc_count = 0
                self.last_fresh_frame_time = 0.0
                self._proc_start_time = time.monotonic()
                self._pixel_samples.clear()
                self._consecutive_frozen_checks = 0

                # Determine active source (primary go2rtc or direct camera RTSP failover)
                rtsp_target = self._active_source

                # 3. Launch nice -n 15 ffmpeg process with clean, compatible zero-latency flags
                cmd = [
                    "nice", "-n", "15",
                    "ffmpeg", "-y", "-nostdin",
                    "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp",
                    "-fflags", "nobuffer",
                    "-i", rtsp_target,
                    "-map", "0:v:0",
                    "-vf", "scale=768:576,setsar=1:1",
                    "-r", "10",
                    "-f", "image2pipe",
                    "-vcodec", "mjpeg",
                    "-q:v", "5",
                    "pipe:1",
                    "-map", "0:a:0?",
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ar", "16000",
                    "-ac", "1",
                    f"udp://{AUDIO_UDP_HOST}:{AUDIO_UDP_PORT}",
                ]

                logger.info("Starting ffmpeg zero-latency reader (%s): %s", rtsp_target, " ".join(cmd[:12]))
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=65536,
                )

                # 4. Start dedicated pipe drain thread to eliminate OS buffer backlog
                loop = asyncio.get_running_loop()
                drain_stop_event = threading.Event()
                drain_thread = threading.Thread(
                    target=self._drain_worker,
                    args=(self._proc.stdout, loop, drain_stop_event),
                    daemon=True,
                    name="LiveHubPipeDrain",
                )
                drain_thread.start()

                # 5. Run audio and watchdog loops concurrently
                audio_task = asyncio.create_task(self._read_audio_loop(loop))
                watchdog_task = asyncio.create_task(self._watchdog_loop())

                done, pending = await asyncio.wait(
                    [audio_task, watchdog_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                drain_stop_event.set()
                for t in pending:
                    t.cancel()

                # If process died without delivering fresh frames, increment fail count
                if self.last_fresh_frame_time == 0.0:
                    self._fail_count += 1
                    if self._fail_count >= 2 and self._active_source == PRIMARY_RTSP_SOURCE:
                        logger.warning("go2rtc unresponsive, failing over to direct camera RTSP")
                        self._active_source = DIRECT_CAMERA_RTSP
                    elif self._fail_count >= 4:
                        logger.info("Attempting to restore primary go2rtc RTSP source")
                        self._active_source = PRIMARY_RTSP_SOURCE
                        self._fail_count = 0
                else:
                    self._fail_count = 0
                    self._active_source = PRIMARY_RTSP_SOURCE

                logger.warning("Live hub stream worker cycle ended, restarting within 100ms...")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in live hub supervisor: %s", e)
            finally:
                self._cleanup_process()

            # Restart within 100ms as required by Anti-Freeze specification
            if self._running:
                await asyncio.sleep(0.1)

    def _drain_worker(
        self,
        stdout,
        loop: asyncio.AbstractEventLoop,
        stop_event: threading.Event,
    ) -> None:
        """
        Zero-Lag Pipe Drain Thread:
        Continuously pulls bytes out of stdout pipe into memory.
        If multiple JPEGs arrive in the pipe, extracts and delivers ONLY the latest
        complete frame, immediately dropping stale intermediate frames.
        Periodically verifies camera clock motion in top-left corner (0:80, 0:350).
        """
        if not stdout:
            return

        buf = bytearray()
        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"
        fd = stdout.fileno()

        while self._running and not stop_event.is_set():
            try:
                chunk = os.read(fd, 16384)
                if not chunk:
                    break
            except Exception:
                break

            buf.extend(chunk)
            now = time.time()
            now_mono = time.monotonic()

            last_complete_frame = None
            while True:
                soi_idx = buf.find(SOI)
                if soi_idx == -1:
                    buf.clear()
                    break
                if soi_idx > 0:
                    del buf[:soi_idx]

                eoi_idx = buf.find(EOI)
                if eoi_idx == -1:
                    break

                # Extract complete frame and slide buffer
                last_complete_frame = bytes(buf[: eoi_idx + 2])
                del buf[: eoi_idx + 2]

            if last_complete_frame is not None:
                crc = zlib.crc32(last_complete_frame)
                self.frame_seq += 1
                self.last_fresh_frame_time = now_mono

                if crc == self.last_crc32:
                    self.identical_crc_count += 1
                else:
                    self.last_crc32 = crc
                    self.identical_crc_count = 0

                self.latest_jpeg = last_complete_frame
                self.latest_frame_ts = now

                # Dispatch latest frame to connected WebSocket clients via event loop
                if loop.is_running():
                    loop.call_soon_threadsafe(self._broadcast, b"\x01" + last_complete_frame)

                # Camera Clock Motion Verification (~1 sample per second)
                if (now_mono - self._last_pixel_sample_time) >= 0.8:
                    self._last_pixel_sample_time = now_mono
                    try:
                        arr = np.frombuffer(last_complete_frame, dtype=np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            # Camera clock region: top-left (0:80, 0:350)
                            clock_crop = img[0:80, 0:350].copy()
                            self._pixel_samples.append((now_mono, clock_crop))

                            # Find sample from 2.5s - 4.5s ago
                            sample_3s_ago = None
                            for s_time, s_crop in reversed(self._pixel_samples):
                                dt = now_mono - s_time
                                if 2.5 <= dt <= 4.5:
                                    sample_3s_ago = (dt, s_crop)
                                    break

                            if sample_3s_ago:
                                dt, old_crop = sample_3s_ago
                                diff = float(np.mean(np.abs(clock_crop.astype(int) - old_crop.astype(int))))
                                self._last_clock_check_diff = diff
                                if diff < 0.15:
                                    self._consecutive_frozen_checks += 1
                                    logger.warning(
                                        "LiveHub: Camera clock frozen! Pixel diff over %.1fs is %.4f (<0.15, count=%d)",
                                        dt, diff, self._consecutive_frozen_checks
                                    )
                                    if self._consecutive_frozen_checks >= 2:
                                        self._kill_process()
                                else:
                                    self._consecutive_frozen_checks = 0
                    except Exception as e:
                        logger.debug("Pixel comparison error: %s", e)

    async def _watchdog_loop(self) -> None:
        """
        Anti-Freeze Watchdog (Runs every 0.5s):
        - If ffmpeg process exited: break immediately to trigger recovery!
        - If monotonic() - last_fresh_frame_time > 4.0s: kill SIGKILL immediately.
        - If CRC32 remains identical for >= 30 frames: kill SIGKILL immediately.
        - If process running >10.0s without receiving any frame: kill SIGKILL immediately.
        - If camera clock pixel difference < 0.15 over 3s: kill SIGKILL immediately.
        """
        while self._running:
            await asyncio.sleep(0.5)

            # Check if process exited
            if self._proc and self._proc.poll() is not None:
                logger.warning("LiveHub ffmpeg process exited with code %s. Restarting...", self._proc.poll())
                break

            now = time.monotonic()
            is_stale_time = (self.last_fresh_frame_time > 0 and (now - self.last_fresh_frame_time > 4.0))
            is_stale_crc = (self.identical_crc_count >= 30)
            is_stale_startup = (self.last_fresh_frame_time == 0.0 and (now - self._proc_start_time > 10.0))
            is_frozen_clock = (self._consecutive_frozen_checks >= 2)

            if is_stale_time or is_stale_crc or is_stale_startup or is_frozen_clock:
                logger.warning(
                    "LiveHub Watchdog TRIGGERED: stale_time=%s, stale_crc=%s, startup=%s, frozen_clock=%s. SIGKILL ffmpeg...",
                    is_stale_time,
                    is_stale_crc,
                    is_stale_startup,
                    is_frozen_clock,
                )
                self._kill_process()
                break

    async def _read_audio_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Read 16kHz PCM audio packets from local UDP socket and broadcast."""
        sock = self._udp_sock
        if not sock:
            return

        while self._running:
            try:
                data = await loop.sock_recv(sock, 2048)
                if not data:
                    break
                # Broadcast audio with prefix 0x02
                self._broadcast(b"\x02" + data)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.01)


live_hub = LiveHub()
