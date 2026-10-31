"""
Foxway Smart Doorbell — Media Recorder & Clip Manager.

Handles every media operation the backend needs:
  • Snapshot grabbing from go2rtc ``/api/frame.jpeg``
  • Background FFmpeg RTSP clip recording
  • HTTP Range video streaming (206 Partial Content)
  • FFmpeg stream-copy / re-encode trimming
  • FFmpeg PCMA backchannel audio injection for quick replies
  • Automatic retention cleanup
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import shutil
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.models import (
    delete_clip as db_delete_clip,
    get_clip,
    get_expired_clips,
    save_clip,
    update_clip,
)

logger = logging.getLogger("foxway.recorder")

# ──────────────────────────────────────────────────────────────────────
#  Snapshot fetcher
# ──────────────────────────────────────────────────────────────────────


async def fetch_snapshot(call_id: str) -> str:
    """
    Grab a single JPEG frame from go2rtc and save it to the snapshots
    directory.  Returns the absolute file path.
    """
    url = f"{settings.GO2RTC_API_URL}/api/frame.jpeg?src={settings.GO2RTC_STREAM_NAME}"
    snapshot_path = str(settings.SNAPSHOTS_DIR / f"{call_id}.jpg")

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    with open(snapshot_path, "wb") as f:
        f.write(resp.content)

    logger.info("Snapshot saved: %s (%d bytes)", snapshot_path, len(resp.content))
    return snapshot_path


# ──────────────────────────────────────────────────────────────────────
#  RAM-based Circular Ring Buffer (Pre-Roll & Post-Roll)
# ──────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────
#  Continuous 2-Second RAM Ring Buffer (/dev/shm/deurbel_ring)
# ──────────────────────────────────────────────────────────────────────

RING_BUFFER_DIR = Path("/dev/shm/deurbel_ring")


class RamRingBuffer:
    """
    Continuous 2-second RAM ring-buffer in /dev/shm/deurbel_ring.
    Ingests RTSP with Unix-timestamped segments (seg_%s.ts) without -segment_wrap corruption.
    Background cleaner purges segments older than 45 seconds every 5 seconds.
    """

    def __init__(self, ring_dir: Optional[Path] = None):
        self.ring_dir = ring_dir or RING_BUFFER_DIR
        self._running = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._cleaner_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.ring_dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._supervise_loop())
        self._cleaner_task = asyncio.create_task(self._cleaner_loop())
        logger.info("RAM Ring Buffer supervisor and cleaner started (%s)", self.ring_dir)

    async def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._task and not self._task.done():
            self._task.cancel()
        if self._cleaner_task and not self._cleaner_task.done():
            self._cleaner_task.cancel()
        logger.info("RAM Ring Buffer stopped")

    def get_available_segments(self) -> list[tuple[float, Path]]:
        """Return list of existing completed .ts segments sorted chronologically by epoch timestamp."""
        if not self.ring_dir.exists():
            return []
        segs = []
        for p in self.ring_dir.glob("seg_*.ts"):
            try:
                st = p.stat()
                if st.st_size > 1000:
                    stem = p.stem.replace("seg_", "")
                    epoch = float(stem) if stem.isdigit() else st.st_mtime
                    segs.append((epoch, p))
            except Exception:
                pass
        segs.sort(key=lambda x: x[0])
        return segs

    async def _cleaner_loop(self) -> None:
        """Purges TS segments older than 45 seconds from RAM to prevent memory bloat."""
        while self._running:
            try:
                await asyncio.sleep(5.0)
                now = time.time()
                for p in self.ring_dir.glob("seg_*.ts"):
                    try:
                        st = p.stat()
                        stem = p.stem.replace("seg_", "")
                        epoch = float(stem) if stem.isdigit() else st.st_mtime
                        if (now - epoch) > 45.0 or (now - st.st_mtime) > 45.0:
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Ring buffer cleaner error: %s", e)

    async def _supervise_loop(self) -> None:
        rtsp_url = "rtsp://127.0.0.1:8554/deurbel"
        while self._running:
            cmd = [
                "nice", "-n", "15",
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-c:v", "copy",
                "-c:a", "aac",
                "-f", "segment",
                "-segment_time", "2",
                "-strftime", "1",
                str(self.ring_dir / "seg_%s.ts"),
            ]
            try:
                logger.info("Starting RAM ring buffer FFmpeg ingest: %s", " ".join(cmd))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await self._proc.wait()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("RAM ring buffer ingest error: %s", e)

            if self._running:
                await asyncio.sleep(2)


ring_buffer = RamRingBuffer()

# Single recording lock to prevent overlapping clips and duration corruption
recording_lock = asyncio.Lock()
_active_recording_info: dict = {
    "clip_id": None,
    "category": "motion",
    "label": "Beweging",
    "extend_until": 0.0,
}

def is_recording_active() -> bool:
    """Return True if a smart clip recording is currently underway."""
    return recording_lock.locked()

def extend_active_recording(category: str, label: str, duration_sec: float = 30.0) -> bool:
    """
    If a recording is in progress, extends the active deadline and upgrades
    the recording category to 'person' if a person is detected.
    """
    if not recording_lock.locked():
        return False
    now = time.time()
    _active_recording_info["extend_until"] = max(
        _active_recording_info.get("extend_until", 0.0),
        now + duration_sec
    )
    if category == "person":
        _active_recording_info["category"] = "person"
        _active_recording_info["label"] = label
        logger.info("Upgraded active clip %s priority to person (%s)", _active_recording_info.get("clip_id"), label)
    else:
        logger.info("Extended active clip %s duration by %.1fs", _active_recording_info.get("clip_id"), duration_sec)
    return True


async def record_smart_clip(
    clip_id: str,
    clip_path: str,
    preroll_seconds: float = 20.0,
    postroll_seconds: float = 20.0,
    motion_active_func: Optional[Callable[[], bool]] = None,
    max_duration: float = 30.0,
    t0: Optional[float] = None,
) -> float:
    """
    Dynamic Clip-Opbouw (v2.5.0):
      1. Grabs exact segments prior to T0 within [T0 - preroll_seconds, T0] (e.g. 10x 2s = 20s pre-roll).
      2. Appends incoming 2s segments as long as motion continues (extensible via extend_active_recording).
      3. Appends exact postroll_seconds segments (e.g. 10x 2s = 20s post-roll) after motion stops.
      4. Validates strict chronological continuity (discards segments prior to any >4s time gap).
      5. Stitches with -fflags +genpts -avoid_negative_ts make_zero in native 4:3 (768x576) +faststart.
    """
    if t0 is None:
        t0 = time.time()

    _active_recording_info["clip_id"] = clip_id

    # Save immediate clean snapshot from live_hub frame if not already present
    try:
        from app.live_hub import live_hub
        snapshot_dest = settings.SNAPSHOTS_DIR / f"{clip_id}.jpg"
        latest_jpeg = live_hub.get_latest_frame()
        if latest_jpeg and len(latest_jpeg) > 1000 and (not snapshot_dest.exists() or snapshot_dest.stat().st_size < 1000):
            snapshot_dest.parent.mkdir(parents=True, exist_ok=True)
            with open(snapshot_dest, "wb") as f:
                f.write(latest_jpeg)
    except Exception as e:
        logger.debug("Could not save initial snapshot for %s: %s", clip_id, e)

    work_dir = RING_BUFFER_DIR / f"work_{clip_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    raw_collected_segments: list[tuple[float, Path]] = []
    seen_paths: set[Path] = set()

    try:
        # 1. Capture Pre-Roll: exactly within [T0 - preroll_seconds, T0]
        await asyncio.sleep(0.3)
        available = ring_buffer.get_available_segments()
        completed = available[:-1] if len(available) > 1 else available

        target_preroll_count = max(1, int(round(preroll_seconds / 2.0)))
        preroll_segs = [s for s in completed if (t0 - preroll_seconds - 2.5) <= s[0] <= (t0 + 1.0)]
        if len(preroll_segs) > target_preroll_count:
            preroll_segs = preroll_segs[-target_preroll_count:]
        elif not preroll_segs and completed:
            preroll_segs = completed[-target_preroll_count:]

        for epoch, seg_path in preroll_segs:
            if seg_path not in seen_paths:
                seen_paths.add(seg_path)
                raw_collected_segments.append((epoch, seg_path))

        logger.info(
            "Smart clip %s pre-roll captured: %d segments (%.1fs) for T0=%.2f",
            clip_id, len(raw_collected_segments), len(raw_collected_segments) * 2.0, t0,
        )

        # 2. Active motion collection (capped at max_duration or extended deadline)
        motion_start = time.time()
        while True:
            now = time.time()
            current_deadline = max(motion_start + max_duration, _active_recording_info.get("extend_until", 0.0))
            if now >= current_deadline:
                break

            await asyncio.sleep(1.8)

            current_segs = ring_buffer.get_available_segments()
            completed_now = current_segs[:-1] if len(current_segs) > 1 else current_segs

            for epoch, seg_path in completed_now:
                if seg_path not in seen_paths:
                    seen_paths.add(seg_path)
                    raw_collected_segments.append((epoch, seg_path))

            is_motion = motion_active_func() if motion_active_func else False
            # Check if active motion stopped and not extended
            if not is_motion and now >= _active_recording_info.get("extend_until", 0.0):
                break

        # 3. Post-Roll collection: exactly postroll_seconds worth of segments
        target_postroll_count = max(1, int(round(postroll_seconds / 2.0)))
        postroll_start = time.time()
        postroll_segs_collected = 0

        while (time.time() - postroll_start) < (postroll_seconds + 3.0) and postroll_segs_collected < target_postroll_count:
            await asyncio.sleep(1.8)

            current_segs = ring_buffer.get_available_segments()
            completed_now = current_segs[:-1] if len(current_segs) > 1 else current_segs

            for epoch, seg_path in completed_now:
                if seg_path not in seen_paths:
                    seen_paths.add(seg_path)
                    raw_collected_segments.append((epoch, seg_path))
                    postroll_segs_collected += 1

        if not raw_collected_segments:
            logger.warning("No segments collected for smart clip %s, falling back to direct record", clip_id)
            await _record(clip_id, clip_path)
            dur = await _probe_duration(clip_path)
            return dur if dur > 0 else 15.0

        # 4. Strict Chronological Sorting & Gap-Sanitization
        # Discard segments prior to any time-gap > 4.0s to ensure video NEVER jumps backward in time!
        raw_collected_segments.sort(key=lambda s: s[0])
        continuous_segments: list[tuple[float, Path]] = []

        for s_epoch, s_path in raw_collected_segments:
            if continuous_segments:
                gap = s_epoch - continuous_segments[-1][0]
                if gap > 4.0:
                    logger.warning(
                        "Time gap of %.1fs > 4.0s detected between segments for %s. Discarding %d prior segments to eliminate time jumps!",
                        gap, clip_id, len(continuous_segments),
                    )
                    continuous_segments = [(s_epoch, s_path)]
                else:
                    continuous_segments.append((s_epoch, s_path))
            else:
                continuous_segments.append((s_epoch, s_path))

        copied_files: list[Path] = []
        for idx, (_, seg_path) in enumerate(continuous_segments):
            dest = work_dir / f"clip_{idx:04d}.ts"
            try:
                shutil.copy2(seg_path, dest)
                copied_files.append(dest)
            except Exception:
                pass

        if not copied_files:
            logger.warning("No valid continuous segments copied for %s", clip_id)
            return 0.0

        # 5. Concatenate and transcode to standard H.264 yuv420p 4:3 (768x576) + faststart
        concat_list = work_dir / "concat_list.txt"
        with open(concat_list, "w") as f:
            for seg in copied_files:
                f.write(f"file '{seg.resolve()}'\n")

        cmd = [
            "nice", "-n", "15",
            "ffmpeg", "-y",
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-vf", "scale=768:576,setsar=1:1",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "64k",
            "-af", "aresample=async=1000",
            "-movflags", "+faststart",
            clip_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

        dur = await _probe_duration(clip_path)
        return dur if dur > 0 else (len(copied_files) * 2.0)

    finally:
        if _active_recording_info.get("clip_id") == clip_id:
            _active_recording_info["clip_id"] = None
            _active_recording_info["extend_until"] = 0.0
        shutil.rmtree(work_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
#  FFmpeg RTSP clip recorder (60s call recording)
# ──────────────────────────────────────────────────────────────────────

_active_recorders: dict[str, asyncio.subprocess.Process] = {}


async def start_recording(call_id: str, clip_path: str) -> asyncio.Task:
    """
    Spawn a background FFmpeg process that records the RTSP stream to
    an MP4 file.  Returns an ``asyncio.Task`` handle that the state
    machine can cancel/stop later.
    """
    task = asyncio.create_task(_record(call_id, clip_path))
    return task


async def _record(call_id: str, clip_path: str) -> None:
    """Run FFmpeg to record RTSP with >=6s pre-roll and transcode to 720p yuv420p faststart."""
    call_work_dir = RING_BUFFER_DIR / f"work_call_{call_id}"
    call_work_dir.mkdir(parents=True, exist_ok=True)
    preroll_files: list[Path] = []

    # 1. Grab 4 segments (8s) of pre-roll at trigger T0 to guarantee >=6s
    try:
        await asyncio.sleep(0.2)
        available = ring_buffer.get_available_segments()
        completed = available[:-1] if len(available) > 1 else available
        preroll_segs = completed[-4:] if len(completed) >= 4 else completed
        for idx, (_, seg_path) in enumerate(preroll_segs):
            dest = call_work_dir / f"preroll_{idx:03d}.ts"
            try:
                shutil.copy2(seg_path, dest)
                preroll_files.append(dest)
            except Exception:
                pass
    except Exception as e:
        logger.debug("Failed capturing pre-roll for call %s: %s", call_id, e)

    raw_ts = str(call_work_dir / "call_live.ts")
    rtsp_url = "rtsp://127.0.0.1:8554/deurbel"
    cmd = [
        "ffmpeg",
        "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(settings.RECORDING_DURATION_SECONDS),
        "-c:v", "copy",
        "-c:a", "aac",
        "-f", "mpegts",
        raw_ts,
    ]
    logger.info("Call recording started: %s (pre-roll segments: %d)", call_id, len(preroll_files))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _active_recorders[call_id] = proc
        _, stderr = await proc.communicate()
        if proc.returncode not in (0, 255):
            logger.warning("FFmpeg raw record exited %d for %s: %s", proc.returncode, call_id, stderr.decode()[-500:])
    except asyncio.CancelledError:
        logger.info("Recording cancelled for %s, terminating FFmpeg", call_id)
        proc = _active_recorders.get(call_id)
        if proc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
    finally:
        _active_recorders.pop(call_id, None)

    # 2. Combine pre-roll + live recording into final transcode
    all_segments = list(preroll_files)
    if os.path.exists(raw_ts) and os.path.getsize(raw_ts) > 1000:
        all_segments.append(Path(raw_ts))

    if all_segments:
        concat_list = call_work_dir / "concat_call.txt"
        with open(concat_list, "w") as f:
            for s in all_segments:
                f.write(f"file '{s.resolve()}'\n")

        transcode_cmd = [
            "nice", "-n", "15",
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-vf", "scale=960:720,setsar=1:1",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "64k",
            "-af", "aresample=async=1000",
            "-movflags", "+faststart",
            clip_path,
        ]
        try:
            t_proc = await asyncio.create_subprocess_exec(
                *transcode_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await t_proc.communicate()
        except Exception as e:
            logger.warning("Call record transcode failed: %s", e)

    # Cleanup temp directory
    shutil.rmtree(call_work_dir, ignore_errors=True)

    # Probe duration and persist clip metadata
    duration = await _probe_duration(clip_path)
    snapshot_path = await ensure_thumbnail(call_id, clip_path)

    # Look up call log to preserve answered_by or trigger_source badge
    ans = ""
    try:
        from app.database import get_db
        async with get_db(write=False) as db:
            c = await db.execute("SELECT trigger_source, answered_by FROM call_logs WHERE call_id = ?", (call_id,))
            row = await c.fetchone()
            if row:
                if row["answered_by"]:
                    ans = row["answered_by"]
                elif row["trigger_source"]:
                    ans = f"Aanbellen [{row['trigger_source']}]"
    except Exception:
        pass
    if not ans:
        from app.state_machine import call_sm
        if call_sm._current and call_sm._current.call_id == call_id:
            ans = call_sm._current.claimed_by or f"Aanbellen [{call_sm._current.trigger_source}]"

    await save_clip(
        clip_id=call_id,
        call_id=call_id,
        video_path=clip_path,
        snapshot_path=snapshot_path,
        duration=duration,
        answered_by=ans,
    )


async def ensure_thumbnail(clip_id: str, video_path: Optional[str] = None, preroll_seconds: float = 20.0) -> str:
    """
    Ensure a thumbnail exists for a clip. If not present on disk, extract
    the frame at preroll_seconds (the moment of detection) from the MP4 using FFmpeg
    so no clip ever has a black or gray missing thumbnail.
    """
    # Check thumb in clips dir or snapshots dir first
    thumb_path = settings.CLIPS_DIR / f"thumb_{clip_id}.jpg"
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1000:
        return str(thumb_path)

    snapshot_path = str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")
    if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 1000:
        return snapshot_path

    # Try live_hub frame if available
    try:
        from app.live_hub import live_hub
        jpeg = live_hub.get_latest_frame()
        if jpeg and len(jpeg) > 1000:
            with open(snapshot_path, "wb") as f:
                f.write(jpeg)
            return snapshot_path
    except Exception:
        pass

    if not video_path:
        video_path = str(settings.CLIPS_DIR / f"{clip_id}.mp4")

    if not os.path.exists(video_path):
        return ""

    try:
        ss_val = max(0.5, preroll_seconds)
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "15",
            "ffmpeg", "-y",
            "-ss", f"{ss_val:.2f}",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            snapshot_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0:
            logger.info("Generated thumbnail from video for %s at ss=%.1fs", clip_id, ss_val)
            try:
                await update_clip(clip_id, snapshot_path=snapshot_path)
            except Exception:
                pass
            return snapshot_path
    except Exception as e:
        logger.warning("Failed to generate thumbnail for %s: %s", clip_id, e)

    return ""


async def stop_recording(task: asyncio.Task) -> None:
    """Gracefully cancel a recording task."""
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ──────────────────────────────────────────────────────────────────────
#  Video duration probe
# ──────────────────────────────────────────────────────────────────────


async def _probe_duration(path: str) -> float:
    """Use ffprobe to get the duration of a media file in seconds."""
    if not os.path.exists(path):
        return 0.0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────────
#  HTTP Range video streaming (206 Partial Content)
# ──────────────────────────────────────────────────────────────────────


async def stream_clip(clip_id: str, request: Request) -> StreamingResponse:
    """
    Serve a clip file with full HTTP Range support so that Android
    ExoPlayer can seek/scrub efficiently.
    """
    clip = await get_clip(clip_id)
    if not clip:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Clip not found"}, status_code=404)

    file_path = clip["video_path"]
    if not os.path.exists(file_path):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Clip file missing"}, status_code=404)

    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")

    if range_header:
        # Parse Range: bytes=start-end
        range_spec = range_header.replace("bytes=", "")
        parts = range_spec.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
        end = min(end, file_size - 1)
        content_length = end - start + 1

        async def ranged_file():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            ranged_file(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
            },
        )
    else:
        async def full_file():
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return StreamingResponse(
            full_file(),
            status_code=200,
            media_type="video/mp4",
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )


# ──────────────────────────────────────────────────────────────────────
#  FFmpeg clip trimmer
# ──────────────────────────────────────────────────────────────────────


async def trim_clip(clip_id: str, start_sec: float, end_sec: float) -> Optional[str]:
    """
    Trim a clip using FFmpeg and return the path to the new trimmed file.
    Uses stream copy when possible (ultra-fast), falling back to
    re-encode with ``-preset ultrafast`` for frame accuracy.
    """
    clip = await get_clip(clip_id)
    if not clip:
        return None

    input_path = clip["video_path"]
    if not os.path.exists(input_path):
        return None

    # Output: {clip_id}_trimmed.mp4
    output_path = input_path.replace(".mp4", "_trimmed.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-to", f"{end_sec:.3f}",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path,
    ]

    logger.info("Trimming clip %s: %.1f–%.1fs", clip_id, start_sec, end_sec)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("Trim failed for %s: %s", clip_id, stderr.decode()[-500:])
        return None

    duration = await _probe_duration(output_path)
    trimmed_clip_id = f"{clip_id}_trimmed"

    await save_clip(
        clip_id=trimmed_clip_id,
        call_id=clip.get("call_id", clip_id),
        video_path=output_path,
        snapshot_path=clip.get("snapshot_path", ""),
        answered_by=clip.get("answered_by", ""),
        duration=duration,
    )

    logger.info("Trim complete: %s (%.1fs)", output_path, duration)
    return output_path


# ──────────────────────────────────────────────────────────────────────
#  Quick-reply audio injection (PCMA backchannel)
# ──────────────────────────────────────────────────────────────────────


async def inject_audio_reply(reply_id: str) -> bool:
    """
    Stream a pre-recorded WAV file into the go2rtc RTSP backchannel as
    ``pcm_alaw`` (8 kHz mono G.711 A-law), which the EZVIZ DB1 Pro
    plays through its external speaker.

    Audio files are stored in ``/app/audio_replies/{reply_id}.wav``.
    """
    audio_path = settings.AUDIO_REPLIES_DIR / f"{reply_id}.wav"
    if not audio_path.exists():
        logger.error("Audio reply file not found: %s", audio_path)
        return False

    # Primary: route directly to EZVIZ DB1 Pro speaker via hardware audio bridge
    try:
        from app.audio_bridge import play_audio_to_doorbell_async
        ok = await play_audio_to_doorbell_async(file_path=audio_path)
        if ok:
            logger.info("Audio reply '%s' played successfully via audio bridge", reply_id)
            return True
    except Exception as e:
        logger.warning("Audio bridge play failed for '%s': %s. Trying RTSP backchannel fallback...", reply_id, e)

    backchannel_url = f"{settings.GO2RTC_RTSP_URL}?backchannel=1"

    cmd = [
        "ffmpeg", "-y",
        "-re",  # Real-time playback speed
        "-i", str(audio_path),
        "-acodec", "pcm_alaw",
        "-ar", "8000",
        "-ac", "1",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        backchannel_url,
    ]

    logger.info("Injecting audio reply '%s' → backchannel", reply_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("Audio injection exited %d: %s", proc.returncode, stderr.decode()[-300:])
            return False
        logger.info("Audio reply '%s' injected successfully", reply_id)
        return True
    except Exception:
        logger.exception("Audio injection failed for '%s'", reply_id)
        return False


# ──────────────────────────────────────────────────────────────────────
#  Clip deletion
# ──────────────────────────────────────────────────────────────────────


async def remove_clip(clip_id: str) -> bool:
    """Delete clip files from disk and remove from the database."""
    clip = await get_clip(clip_id)
    if not clip:
        return False

    # Delete video file
    video_path = clip.get("video_path", "")
    if video_path and os.path.exists(video_path):
        os.remove(video_path)
        logger.info("Deleted video: %s", video_path)

    # Delete snapshot file
    snapshot_path = clip.get("snapshot_path", "")
    if snapshot_path and os.path.exists(snapshot_path):
        os.remove(snapshot_path)
        logger.info("Deleted snapshot: %s", snapshot_path)

    await db_delete_clip(clip_id)
    return True


# ──────────────────────────────────────────────────────────────────────
#  Background Video Compression (libx264 -crf 28)
# ──────────────────────────────────────────────────────────────────────


async def compress_clip_background(clip_path: str) -> bool:
    """
    Compress clip in background using nice -n 19 and libx264 -crf 28.
    Keeps audio clean with aac 48k. Reduces typical clip size to 1-4 MB
    without noticeable loss of visual clarity.
    """
    if not os.path.exists(clip_path) or "_compressed" in clip_path or "_tmp" in clip_path:
        return False

    tmp_path = clip_path.replace(".mp4", "_compressed_tmp.mp4")
    cmd = [
        "nice", "-n", "19",
        "ffmpeg", "-y",
        "-i", clip_path,
        "-vcodec", "libx264",
        "-crf", "28",
        "-preset", "veryfast",
        "-vf", "scale=960:720,setsar=1:1",
        "-pix_fmt", "yuv420p",
        "-acodec", "aac",
        "-b:a", "48k",
        "-movflags", "+faststart",
        tmp_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
            old_size = os.path.getsize(clip_path)
            new_size = os.path.getsize(tmp_path)
            if new_size <= old_size or old_size == 0:
                os.replace(tmp_path, clip_path)
                logger.info(
                    "Compressed %s: %d KB → %d KB (%.1f%% saving)",
                    Path(clip_path).name,
                    old_size // 1024,
                    new_size // 1024,
                    (1.0 - new_size / max(1, old_size)) * 100.0,
                )
            else:
                os.remove(tmp_path)
            return True
        elif os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception as e:
        logger.warning("Compression error for %s: %s", clip_path, e)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    return False


# ──────────────────────────────────────────────────────────────────────
#  Manual Live Recording
# ──────────────────────────────────────────────────────────────────────

_active_manual_recording: dict = {}


async def start_manual_recording(user_name: str = "Gezinslid", stream_name: str = "deurbel") -> dict:
    """Start manual recording from live stream during live view."""
    global _active_manual_recording
    if _active_manual_recording.get("active"):
        return {
            "active": True,
            "clip_id": _active_manual_recording.get("clip_id"),
            "started_at": _active_manual_recording.get("started_at"),
            "user_name": _active_manual_recording.get("user_name"),
        }

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    clip_id = f"manual_{timestamp_str}"
    clip_filename = f"{clip_id}.mp4"
    clip_path = str(settings.CLIPS_DIR / clip_filename)

    rtsp_url = f"rtsp://127.0.0.1:8554/{stream_name}"
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-c:v", "copy",
        "-c:a", "aac",
        "-movflags", "+faststart",
        clip_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    started_at = datetime.now(timezone.utc).isoformat()
    _active_manual_recording = {
        "active": True,
        "clip_id": clip_id,
        "clip_path": clip_path,
        "started_at": started_at,
        "user_name": user_name,
        "proc": proc,
    }
    logger.info("Manual recording started: %s by %s", clip_id, user_name)
    return {
        "active": True,
        "clip_id": clip_id,
        "started_at": started_at,
        "user_name": user_name,
    }


async def stop_manual_recording(user_name: str = "") -> Optional[dict]:
    """Stop active manual recording and save clip with thumbnail."""
    global _active_manual_recording
    if not _active_manual_recording.get("active"):
        return None

    clip_id = _active_manual_recording["clip_id"]
    clip_path = _active_manual_recording["clip_path"]
    rec_user = _active_manual_recording.get("user_name") or user_name or "Gezinslid"
    proc = _active_manual_recording.get("proc")

    if proc:
        import signal
        try:
            proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                proc.kill()

    _active_manual_recording = {}

    duration = await _probe_duration(clip_path)
    snapshot_path = await ensure_thumbnail(clip_id, clip_path)
    await save_clip(
        clip_id=clip_id,
        call_id=clip_id,
        video_path=clip_path,
        snapshot_path=snapshot_path,
        answered_by=f"Handmatige opname door {rec_user}",
        duration=duration,
    )
    logger.info("Manual recording stopped and saved: %s (%.1fs)", clip_id, duration)

    # Start compression in background
    asyncio.create_task(compress_clip_background(clip_path))

    return {
        "status": "saved",
        "clip_id": clip_id,
        "duration": duration,
        "video_url": f"/api/v1/clips/{clip_id}/stream",
        "snapshot_url": f"/api/v1/snapshots/{clip_id}.jpg",
        "answered_by": f"Handmatige opname door {rec_user}",
    }


def get_manual_recording_status() -> dict:
    """Return whether a manual recording is actively in progress."""
    if not _active_manual_recording.get("active"):
        return {"active": False}
    return {
        "active": True,
        "clip_id": _active_manual_recording.get("clip_id"),
        "started_at": _active_manual_recording.get("started_at"),
        "user_name": _active_manual_recording.get("user_name"),
    }


async def cleanup_today_false_clips() -> dict:
    """
    Purge today's false wind/leaf clips (durations >= 55s created by the old motion crop).
    Leaves user starred clips untouched.
    """
    from app.database import get_db
    today_str = datetime.now().strftime("%Y-%m-%d")
    deleted_count = 0
    freed_bytes = 0

    async with get_db(write=True) as db:
        cur = await db.execute(
            """
            SELECT clip_id, video_path, snapshot_path 
            FROM clips 
            WHERE starred = 0 
              AND duration >= 55.0 
              AND created_at >= ?
              AND (answered_by LIKE 'AI:%' OR answered_by LIKE '%Persoon%' OR answered_by LIKE '%Voertuig%')
            """,
            (today_str,),
        )
        rows = await cur.fetchall()
        for r in rows:
            cid = r["clip_id"]
            vpath = r["video_path"] or str(settings.CLIPS_DIR / f"{cid}.mp4")
            spath = r["snapshot_path"] or str(settings.SNAPSHOTS_DIR / f"{cid}.jpg")

            for p in [vpath, spath, str(settings.CLIPS_DIR / f"{cid}.mp4"), str(settings.SNAPSHOTS_DIR / f"{cid}.jpg")]:
                if os.path.exists(p):
                    try:
                        freed_bytes += os.path.getsize(p)
                        os.remove(p)
                    except Exception:
                        pass

            await db.execute("DELETE FROM clips WHERE clip_id = ?", (cid,))
            await db.execute("DELETE FROM events WHERE clip_id = ?", (cid,))
            deleted_count += 1

        await db.commit()

    logger.info("Cleaned up %d false wind clips from today (freed %.1f MB)", deleted_count, freed_bytes / (1024 * 1024))
    return {
        "status": "ok",
        "deleted_count": deleted_count,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
    }


# ──────────────────────────────────────────────────────────────────────
#  Storage Usage & Dual-Tier Retention Cleanup
# ──────────────────────────────────────────────────────────────────────


def get_storage_stats(max_storage_gb: float = 10.0) -> dict:
    """Calculate disk usage of clips directory."""
    clips_dir = settings.CLIPS_DIR
    total_bytes = 0
    clip_count = 0
    if clips_dir.exists():
        for f in clips_dir.glob("*.mp4"):
            try:
                total_bytes += f.stat().st_size
                clip_count += 1
            except Exception:
                pass
    used_mb = round(total_bytes / (1024 * 1024), 2)
    max_mb = round(max_storage_gb * 1024, 2)
    return {
        "used_mb": used_mb,
        "max_mb": max_mb,
        "max_storage_gb": max_storage_gb,
        "total_clips": clip_count,
        "usage_percent": round((used_mb / max(1.0, max_mb)) * 100.0, 1),
    }


async def cleanup_storage_and_retention(retention_days: int = 30, max_storage_gb: float = 10.0) -> dict:
    """
    Two-tier retention and storage protection:
      1. Purge unlocked clips older than retention_days.
      2. If total size exceeds max_storage_gb, purge oldest unlocked clips until within cap.
    Starred/locked clips are NEVER removed.
    """
    removed_count = 0
    freed_bytes = 0

    # 1. Retention days purge
    expired = await get_expired_clips(retention_days)
    for c in expired:
        cid = c["clip_id"]
        vpath = c.get("video_path")
        if vpath and os.path.exists(vpath):
            try:
                freed_bytes += os.path.getsize(vpath)
            except Exception:
                pass
        ok = await remove_clip(cid)
        if ok:
            removed_count += 1

    # 2. Storage cap purge
    max_bytes = int(max_storage_gb * 1024 * 1024 * 1024)
    from app.models import get_unlocked_clips_oldest_first
    unlocked = await get_unlocked_clips_oldest_first()

    total_bytes = sum(f.stat().st_size for f in settings.CLIPS_DIR.glob("*.mp4") if f.exists())
    idx = 0
    while total_bytes > max_bytes and idx < len(unlocked):
        c = unlocked[idx]
        cid = c["clip_id"]
        vpath = c.get("video_path")
        c_size = os.path.getsize(vpath) if (vpath and os.path.exists(vpath)) else 0
        ok = await remove_clip(cid)
        if ok:
            removed_count += 1
            freed_bytes += c_size
            total_bytes -= c_size
        idx += 1

    logger.info("Storage cleanup: removed %d clips, freed %.2f MB", removed_count, freed_bytes / (1024 * 1024))
    stats = get_storage_stats(max_storage_gb)
    stats["removed_count"] = removed_count
    stats["freed_mb"] = round(freed_bytes / (1024 * 1024), 2)
    return stats


async def cleanup_expired_clips() -> int:
    """Legacy helper for periodic retention cleanup."""
    stats = await cleanup_storage_and_retention(settings.CLIP_RETENTION_DAYS, 10.0)
    return stats.get("removed_count", 0)

