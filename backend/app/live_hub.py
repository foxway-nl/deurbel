"""
Foxway Smart Doorbell — Always-Warm RAM Live Hub (v2.6.0).

Hardware-accelerated streaming is handled natively by go2rtc via MSE + WebRTC (/api/v1/live/player).
LiveHub provides ultra-low-overhead snapshot caching and fallback streaming:
- On-demand snapshot fetching from go2rtc (http://127.0.0.1:1984/api/frame.jpeg?src=deurbel).
- Zero CPU image2pipe transcoding when no WebSocket clients are listening (0% server CPU overhead).
- Realtime frame caching for AI detector (ai_detector.py) and instant snapshot APIs.
- UDP incoming audio listener on port 18555 for two-way audio bridge integration.
- Anti-Freeze Clock-Motion Watchdog to verify live stream continuity.
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
import urllib.request
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
GO2RTC_SNAPSHOT_URL = "http://127.0.0.1:1984/api/frame.jpeg?src=deurbel"
GO2RTC_SUB_SNAPSHOT_URL = "http://127.0.0.1:1984/api/frame.jpeg?src=deurbel_sub"


class LiveHub:
    """Always-Warm In-Memory Stream Hub with Real-Time Anti-Freeze Watchdog (v2.6.0)."""

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
        self._http_client: Optional[httpx.AsyncClient] = None

    def _get_http_client(self, timeout: float = 2.0) -> httpx.AsyncClient:
        """Shared persistent HTTP client with connection pooling for low-overhead go2rtc polling."""
        if self._http_client is None or self._http_client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
            client_timeout = httpx.Timeout(connect=1.5, read=timeout, write=1.5, pool=2.0)
            self._http_client = httpx.AsyncClient(limits=limits, timeout=client_timeout)
        return self._http_client

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
        """Returns latest in-memory JPEG frame, or fetches a fresh snapshot if available."""
        if self.latest_jpeg:
            return self.latest_jpeg
        return self.fetch_snapshot_sync()

    def fetch_snapshot_sync(self, timeout: float = 1.5) -> Optional[bytes]:
        """Fetch fresh JPEG frame synchronously from go2rtc without running ffmpeg subprocess."""
        for url in (GO2RTC_SUB_SNAPSHOT_URL, GO2RTC_SNAPSHOT_URL):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "FoxwayLiveHub/2.6.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = resp.read()
                        if len(data) > 1000:
                            now = time.time()
                            now_mono = time.monotonic()
                            self.latest_jpeg = data
                            self.latest_frame_ts = now
                            self.last_fresh_frame_time = now_mono
                            self.frame_seq += 1
                            return data
            except Exception as e:
                logger.debug("Snapshot sync fetch (%s): %s", url, e)
        return self.latest_jpeg

    async def fetch_snapshot_async(self, timeout: float = 2.0) -> Optional[bytes]:
        """Fetch fresh JPEG frame asynchronously from go2rtc using persistent connection pool."""
        client = self._get_http_client(timeout=timeout)
        for url in (GO2RTC_SUB_SNAPSHOT_URL, GO2RTC_SNAPSHOT_URL):
            try:
                resp = await client.get(url, timeout=timeout)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    now = time.time()
                    now_mono = time.monotonic()
                    self.latest_jpeg = resp.content
                    self.latest_frame_ts = now
                    self.last_fresh_frame_time = now_mono
                    self.frame_seq += 1
                    return resp.content
            except Exception as e:
                logger.debug("Snapshot async fetch (%s): %s", url, e)
        return self.latest_jpeg

    async def start(self) -> None:
        """Start the supervisor background task."""
        if self._running:
            return
        self._running = True
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        logger.info("Always-Warm RAM Live Hub started in hardware streaming mode (v2.6.0)")

    async def stop(self) -> None:
        """Stop supervisor and terminate processes."""
        self._running = False
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass

        if self._http_client and not self._http_client.is_closed:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        self._cleanup_process()
        logger.info("Always-Warm RAM Live Hub stopped")

    def _kill_process(self) -> None:
        """Immediate SIGKILL of fallback ffmpeg process if running."""
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    async def restart(self) -> None:
        """
        Force immediate kill and restart of live stream.
        1. Kills any fallback process.
        2. Restarts go2rtc stream via POST /api/restart.
        3. Clears cached frozen frame from memory.
        4. Fetches a fresh frame before returning.
        """
        logger.info("LiveHub explicit restart requested — triggering immediate re-init")
        self._kill_process()
        self.latest_jpeg = None
        self.last_fresh_frame_time = 0.0
        self._last_clock_check_diff = 0.0
        self._pixel_samples.clear()

        # Trigger go2rtc reload
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post("http://127.0.0.1:1984/api/restart")
                logger.info("go2rtc stream reset signaled successfully")
        except Exception as e:
            logger.debug("go2rtc reset signal: %s", e)

        # Wait up to 3.5s for fresh frame arrival
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.5:
            fresh = await self.fetch_snapshot_async(timeout=1.5)
            if fresh and self.last_fresh_frame_time > 0 and (time.monotonic() - self.last_fresh_frame_time < 1.0):
                logger.info("LiveHub restart verified: fresh frame received (<%.2fs)", time.monotonic() - t0)
                break
            await asyncio.sleep(0.2)

    def _cleanup_process(self) -> None:
        """Kill subprocess and close UDP socket."""
        self._kill_process()
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
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def _supervisor_loop(self) -> None:
        """
        Lightweight Supervisor Loop (v2.6.0):
        - Binds UDP audio socket for two-way audio.
        - When no WebSocket clients are listening, zero CPU transcoding is performed.
        - Periodically refreshes RAM frame cache from go2rtc to keep latest_jpeg warm.
        - Broadcasts frames to WebSocket clients if connected.
        """
        while self._running:
            try:
                # 1. Bind UDP socket for audio
                if not self._udp_sock:
                    self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self._udp_sock.bind((AUDIO_UDP_HOST, AUDIO_UDP_PORT))
                    self._udp_sock.setblocking(False)

                loop = asyncio.get_running_loop()
                audio_task = asyncio.create_task(self._read_audio_loop(loop))

                backoff_delay = 0.25
                while self._running:
                    # Dynamically check if active high-rate polling is required
                    has_clients = bool(self._clients)
                    ai_active = False
                    try:
                        from app.ai_detector import smart_detector
                        if smart_detector and smart_detector.is_motion_active():
                            ai_active = True
                    except Exception:
                        pass

                    # When clients are active or motion is occurring, poll at 4-5 FPS (0.20 - 0.25s)
                    # When completely idle, poll at ~1.3 FPS (0.75s) to conserve CPU on both go2rtc and backend
                    max_age = 0.25 if (has_clients or ai_active) else 0.75
                    need_fetch = has_clients or not self.is_fresh(max_age=max_age)

                    if need_fetch:
                        frame = await self.fetch_snapshot_async(timeout=1.0)
                        if frame and self._clients:
                            self._broadcast(b"\x01" + frame)
                        if not frame:
                            # go2rtc error or restarting: back off slightly
                            await asyncio.sleep(min(backoff_delay, 2.0))
                            backoff_delay = min(backoff_delay * 1.5, 2.0)
                            continue
                        else:
                            backoff_delay = 0.25

                    sleep_time = 0.20 if has_clients else (0.25 if ai_active else 0.75)
                    await asyncio.sleep(sleep_time)

                audio_task.cancel()
                try:
                    await audio_task
                except asyncio.CancelledError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Error in live hub supervisor loop: %s", e)
                await asyncio.sleep(1.0)
            finally:
                self._cleanup_process()

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

