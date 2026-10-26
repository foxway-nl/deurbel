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
    Runs 24/7 FFmpeg process recording RTSP without CPU re-encoding into
    15 x 2-second TS segments (30s capacity).
    """

    def __init__(self):
        self.ring_dir = RING_BUFFER_DIR
        self._running = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.ring_dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._supervise_loop())
        logger.info("RAM Ring Buffer supervisor started (%s)", self.ring_dir)

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
        logger.info("RAM Ring Buffer stopped")

    def get_available_segments(self) -> list[tuple[float, Path]]:
        """Return list of existing completed .ts segments sorted chronologically by mtime."""
        if not self.ring_dir.exists():
            return []
        segs = []
        for p in self.ring_dir.glob("seg_*.ts"):
            try:
                st = p.stat()
                if st.st_size > 1000:
                    segs.append((st.st_mtime, p))
            except Exception:
                pass
        segs.sort(key=lambda x: x[0])
        return segs

    async def _supervise_loop(self) -> None:
        rtsp_url = "rtsp://127.0.0.1:8554/deurbel"
        while self._running:
            cmd = [
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-c:v", "copy",
                "-c:a", "aac",
                "-f", "segment",
                "-segment_time", "2",
                "-segment_wrap", "15",
                "-reset_timestamps", "1",
                str(self.ring_dir / "seg_%03d.ts"),
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


async def record_smart_clip(
    clip_id: str,
    clip_path: str,
    preroll_seconds: float = 6.0,
    postroll_seconds: float = 6.0,
    motion_active_func: Optional[Callable[[], bool]] = None,
    max_duration: float = 45.0,
) -> float:
    """
    Dynamic Clip-Opbouw:
      1. Grabs 3 segments prior to T0 (6s Pre-Roll, capturing the lead-up + snapshot).
      2. Appends incoming 2s segments as long as motion continues.
      3. Appends 3 segments (6s Post-Roll) after motion stops.
      4. Stitches & transcodes with nice -n 15 libx264 yuv420p +faststart.
    """
    work_dir = RING_BUFFER_DIR / f"work_{clip_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    copied_files: list[Path] = []
    seen_mtimes: set[float] = set()

    try:
        # 1. Capture Pre-Roll: 3 segments (6 seconds) before trigger T0
        # Give ring buffer 0.2s to stabilize
        await asyncio.sleep(0.2)
        available = ring_buffer.get_available_segments()

        # The very latest segment is actively being written by ffmpeg, so completed segments are available[:-1]
        completed = available[:-1] if len(available) > 1 else available
        preroll_segs = completed[-3:] if len(completed) >= 3 else completed

        seq = 0
        for mtime, seg_path in preroll_segs:
            dest = work_dir / f"clip_{seq:04d}.ts"
            try:
                shutil.copy2(seg_path, dest)
                copied_files.append(dest)
                seen_mtimes.add(mtime)
                seq += 1
            except Exception:
                pass

        # 2. Active motion & 3-segment (6s) post-roll collection
        start_time = time.time()
        last_motion_time = time.time()
        postroll_segs_collected = 0

        while (time.time() - start_time) < max_duration:
            await asyncio.sleep(1.8)

            current_segs = ring_buffer.get_available_segments()
            completed_now = current_segs[:-1] if len(current_segs) > 1 else current_segs

            for mtime, seg_path in completed_now:
                if mtime not in seen_mtimes:
                    seen_mtimes.add(mtime)
                    dest = work_dir / f"clip_{seq:04d}.ts"
                    try:
                        shutil.copy2(seg_path, dest)
                        copied_files.append(dest)
                        seq += 1
                    except Exception:
                        pass

            is_motion = motion_active_func() if motion_active_func else False
            if is_motion:
                last_motion_time = time.time()
                postroll_segs_collected = 0
            else:
                postroll_segs_collected += 1
                if postroll_segs_collected >= 3:  # 3 segments = 6s post-roll
                    break

        if not copied_files:
            logger.warning("No segments collected for smart clip %s, falling back to direct record", clip_id)
            await _record(clip_id, clip_path)
            dur = await _probe_duration(clip_path)
            return dur if dur > 0 else 15.0

        # 3. Concatenate and transcode to standard H.264 yuv420p + faststart
        concat_list = work_dir / "concat_list.txt"
        with open(concat_list, "w") as f:
            for seg in copied_files:
                f.write(f"file '{seg.resolve()}'\n")

        cmd = [
            "nice", "-n", "15",
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-vf", "scale=-2:720",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "64k",
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
    """Run FFmpeg to record RTSP and transcode to 720p yuv420p faststart."""
    raw_path = f"/dev/shm/{call_id}_raw.mp4" if os.path.exists("/dev/shm") else clip_path.replace(".mp4", "_raw.mp4")
    rtsp_url = "rtsp://127.0.0.1:8554/deurbel"
    cmd = [
        "ffmpeg",
        "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(settings.RECORDING_DURATION_SECONDS),
        "-c:v", "copy",
        "-c:a", "aac",
        raw_path,
    ]
    logger.info("Recording started: %s → %s", call_id, raw_path)

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

    # Transcode raw_path to libx264 yuv420p +faststart
    if os.path.exists(raw_path) and os.path.getsize(raw_path) > 1000:
        transcode_cmd = [
            "nice", "-n", "15",
            "ffmpeg", "-y",
            "-i", raw_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-vf", "scale=-2:720",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "64k",
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
        try:
            os.remove(raw_path)
        except Exception:
            pass

    # Probe duration and persist clip metadata
    duration = await _probe_duration(clip_path)
    snapshot_path = await ensure_thumbnail(call_id, clip_path)
    await save_clip(
        clip_id=call_id,
        call_id=call_id,
        video_path=clip_path,
        snapshot_path=snapshot_path,
        duration=duration,
    )


async def ensure_thumbnail(clip_id: str, video_path: Optional[str] = None) -> str:
    """
    Ensure a thumbnail exists for a clip. If not present on disk, extract
    the first clear frame from the MP4 using FFmpeg so no clip ever has
    a gray/missing thumbnail.
    """
    snapshot_path = str(settings.SNAPSHOTS_DIR / f"{clip_id}.jpg")
    if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0:
        return snapshot_path

    if not video_path:
        video_path = str(settings.CLIPS_DIR / f"{clip_id}.mp4")

    if not os.path.exists(video_path):
        return ""

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-ss", "00:00:00.500",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            snapshot_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0:
            logger.info("Generated thumbnail from video for %s", clip_id)
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
        "-vf", "scale=-2:720",
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

