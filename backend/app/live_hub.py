"""
Foxway Smart Doorbell — Always-Warm RAM Live Hub.

Keeps a persistent 24/7 connection to go2rtc RTSP in memory:
- Decodes 15 fps 4:3 video (768x576) into RAM (self.latest_jpeg).
- Reads 16kHz mono 16-bit PCM incoming audio from doorbell microphone.
- Streams realtime frames and audio over WebSocket /api/v1/ws/live (<200ms latency on 5G & LAN).
- Instant snapshot endpoint /api/v1/live/instant.jpg (<5ms from RAM).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import time
from typing import Optional, Set

logger = logging.getLogger("foxway.live_hub")

AUDIO_UDP_HOST = "127.0.0.1"
AUDIO_UDP_PORT = 18555
RTSP_SOURCE = "rtsp://127.0.0.1:8554/deurbel"


class LiveHub:
    """Always-Warm In-Memory Stream Hub for zero-latency live viewing."""

    def __init__(self) -> None:
        self.latest_jpeg: Optional[bytes] = None
        self.latest_frame_ts: float = 0.0
        self._clients: Set[asyncio.Queue] = set()
        self._running: bool = False
        self._supervisor_task: Optional[asyncio.Task] = None
        self._proc: Optional[subprocess.Popen] = None
        self._udp_sock: Optional[socket.socket] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the supervisor background task."""
        if self._running:
            return
        self._running = True
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        logger.info("Always-Warm RAM Live Hub started")

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

    def _cleanup_process(self) -> None:
        """Kill subprocess and close UDP socket."""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
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
        """Supervises the ffmpeg decoding process and restarts on failure."""
        while self._running:
            try:
                # 1. Bind UDP socket for audio
                self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._udp_sock.bind((AUDIO_UDP_HOST, AUDIO_UDP_PORT))
                self._udp_sock.setblocking(False)

                # 2. Launch nice -n 15 ffmpeg process
                cmd = [
                    "nice", "-n", "15",
                    "ffmpeg", "-y", "-nostdin",
                    "-rtsp_transport", "tcp",
                    "-i", RTSP_SOURCE,
                    "-map", "0:v:0",
                    "-vf", "scale=768:576,fps=15",
                    "-q:v", "4",
                    "-f", "image2pipe",
                    "-flush_packets", "1",
                    "-vcodec", "mjpeg",
                    "pipe:1",
                    "-map", "0:a:0?",
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ar", "16000",
                    "-ac", "1",
                    f"udp://{AUDIO_UDP_HOST}:{AUDIO_UDP_PORT}",
                ]

                logger.info("Starting ffmpeg always-warm decoder: %s", " ".join(cmd[:6]))
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=65536,
                )

                # 3. Run video and audio loops concurrently
                loop = asyncio.get_running_loop()
                video_task = asyncio.create_task(self._read_video_loop(loop))
                audio_task = asyncio.create_task(self._read_audio_loop(loop))

                done, pending = await asyncio.wait(
                    [video_task, audio_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for t in pending:
                    t.cancel()

                logger.warning("Live hub stream worker ended, restarting in 2 seconds...")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in live hub supervisor: %s", e)
            finally:
                self._cleanup_process()

            if self._running:
                await asyncio.sleep(2.0)

    async def _read_video_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Read stdout from ffmpeg, parse JPEG frames and store in memory."""
        proc = self._proc
        if not proc or not proc.stdout:
            return

        buf = bytearray()
        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"

        while self._running and proc.poll() is None:
            chunk = await loop.run_in_executor(None, proc.stdout.read, 8192)
            if not chunk:
                break

            buf.extend(chunk)
            now = time.time()

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

                jpeg_frame = bytes(buf[: eoi_idx + 2])
                del buf[: eoi_idx + 2]

                # Update RAM frame
                self.latest_jpeg = jpeg_frame
                self.latest_frame_ts = now

                # Broadcast to clients with prefix 0x01
                self._broadcast(b"\x01" + jpeg_frame)

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
