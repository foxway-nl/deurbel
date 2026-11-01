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


def get_day_clip_dir(dt: Optional[datetime] = None) -> Path:
    """Returns /mnt/opslag/deurbel/clips/YYYY-MM-DD/, creating it automatically."""
    today_str = (dt or datetime.now()).strftime("%Y-%m-%d")
    day_dir = settings.CLIPS_DIR / today_str
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


def resolve_clip_path(target: str, ext: Optional[str] = None) -> Optional[Path]:
    """
    Robust clip and snapshot resolver (v3.5.0):
    1. If target is an existing path on disk, returns it immediately.
    2. Searches day subdirectories (/clips/YYYY-MM-DD/) in reverse chronological order.
    3. Fallback searches root /clips/.
    4. Fallback searches /snapshots/.
    """
    if not target:
        return None

    p_direct = Path(target)
    if p_direct.is_file() and p_direct.stat().st_size > 0:
        return p_direct

    base = p_direct.name
    search_names = [base]
    if ext and not base.endswith(ext):
        search_names.append(f"{base}{ext}")

    if ext == ".jpg" or base.endswith(".jpg") or "thumb" in base:
        clean_id = base.replace(".jpg", "").replace("thumb_", "")
        for variant in [f"thumb_{clean_id}.jpg", f"{clean_id}.jpg", f"{clean_id}_debug.jpg"]:
            if variant not in search_names:
                search_names.append(variant)
    elif ext == ".mp4" or base.endswith(".mp4"):
        clean_id = base.replace(".mp4", "")
        for variant in [f"{clean_id}.mp4", f"{clean_id}_trimmed.mp4"]:
            if variant not in search_names:
                search_names.append(variant)

    # 1. Search day folders (/clips/YYYY-MM-DD/) in reverse chronological order
    if settings.CLIPS_DIR.exists():
        day_dirs = []
        try:
            for d in settings.CLIPS_DIR.iterdir():
                if d.is_dir() and len(d.name) == 10 and d.name[4] == '-' and d.name[7] == '-':
                    day_dirs.append(d)
        except Exception:
            pass
        day_dirs.sort(key=lambda d: d.name, reverse=True)

        for d in day_dirs:
            for sname in search_names:
                cand = d / sname
                if cand.is_file() and cand.stat().st_size > 0:
                    return cand

    # 2. Search root /clips/
    if settings.CLIPS_DIR.exists():
        for sname in search_names:
            cand = settings.CLIPS_DIR / sname
            if cand.is_file() and cand.stat().st_size > 0:
                return cand

    # 3. Search /snapshots/
    if settings.SNAPSHOTS_DIR.exists():
        for sname in search_names:
            cand = settings.SNAPSHOTS_DIR / sname
            if cand.is_file() and cand.stat().st_size > 0:
                return cand

    return None

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
#  Continuous 1-Second RAM Ring Buffer (/dev/shm/deurbel_preroll)
# ──────────────────────────────────────────────────────────────────────

RING_BUFFER_DIR = Path("/dev/shm/deurbel_preroll")


class RamRingBuffer:
    """
    Continuous 1-second RAM ring-buffer in /dev/shm/deurbel_preroll.
    Ingests RTSP with Unix-timestamped segments (seg_%s.ts) without -segment_wrap corruption.
    Background cleaner purges segments older than 60 seconds every 5 seconds.
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
        # Backward compatibility symlink for /dev/shm/deurbel_ring
        legacy_dir = Path("/dev/shm/deurbel_ring")
        if not legacy_dir.exists():
            try:
                legacy_dir.symlink_to(self.ring_dir, target_is_directory=True)
            except Exception:
                pass
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
        dirs_to_check = [self.ring_dir]
        legacy_dir = Path("/dev/shm/deurbel_ring")
        if legacy_dir.exists() and not legacy_dir.is_symlink():
            dirs_to_check.append(legacy_dir)

        segs = []
        seen_stems = set()
        for d in dirs_to_check:
            for p in d.glob("seg_*.ts"):
                try:
                    st = p.stat()
                    if st.st_size > 1000 and p.stem not in seen_stems:
                        seen_stems.add(p.stem)
                        stem = p.stem.replace("seg_", "")
                        epoch = float(stem) if stem.isdigit() else st.st_mtime
                        segs.append((epoch, p))
                except Exception:
                    pass
        segs.sort(key=lambda x: x[0])
        return segs

    async def _cleaner_loop(self) -> None:
        """Purges TS segments older than 60 seconds from RAM to prevent memory bloat."""
        while self._running:
            try:
                await asyncio.sleep(5.0)
                now = time.time()
                for p in self.ring_dir.glob("seg_*.ts"):
                    try:
                        st = p.stat()
                        stem = p.stem.replace("seg_", "")
                        epoch = float(stem) if stem.isdigit() else st.st_mtime
                        if (now - epoch) > 60.0 or (now - st.st_mtime) > 60.0:
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
                "-segment_time", "1",
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
    "last_motion_timestamp": 0.0,
}

def is_recording_active() -> bool:
    """Return True if a smart clip recording is currently underway."""
    return recording_lock.locked()

def notify_ongoing_motion(category: Optional[str] = None, label: Optional[str] = None) -> bool:
    """
    Signaled by AI detector (on active verified motion) or Port 8000 SDK (0x4000).
    Updates last_motion_timestamp so the active recording keeps rolling until post_roll_seconds
    after motion completely stops.
    """
    if not _active_recording_info.get("clip_id") and not recording_lock.locked():
        return False
    now = time.time()
    _active_recording_info["last_motion_timestamp"] = now
    if category == "person":
        _active_recording_info["category"] = "person"
        if label:
            _active_recording_info["label"] = label
    return True

def extend_active_recording(category: str, label: str, duration_sec: float = 30.0) -> bool:
    """
    If a recording is in progress, extends the active deadline and upgrades
    the recording category to 'person' if a person is detected.
    """
    if not recording_lock.locked():
        return False
    now = time.time()
    _active_recording_info["last_motion_timestamp"] = now
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
    max_duration: float = 180.0,
    t0: Optional[float] = None,
) -> float:
    """
    Dynamic Keep-Recording-While-Motion Clip-Opbouw (v3.5.2):
      1. Grabs exact 1s segments prior to T0 within [T0 - preroll_seconds, T0] (e.g. 20s pre-roll).
      2. Appends incoming segments continuously as long as motion continues (via notify_ongoing_motion or motion_active_func).
      3. Continues recording until postroll_seconds after motion has completely stopped (safety cap: max_duration, default 180s).
      4. Stitches segments via fast stream-copy (-c copy), with automatic fallback to ultrafast transcode if needed.
    """
    if t0 is None:
        t0 = time.time()

    _active_recording_info["clip_id"] = clip_id
    _active_recording_info["last_motion_timestamp"] = time.time()

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

        target_preroll_count = max(1, int(round(preroll_seconds)))
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
            "Smart clip %s pre-roll captured: %d segments (%.1fs) for T0=%.2f (target=%.1fs)",
            clip_id, len(raw_collected_segments), float(len(raw_collected_segments)), t0, preroll_seconds,
        )

        # 2. Dynamic Motion Collection: Keep recording WHILE motion lasts + postroll_seconds
        motion_start = time.time()
        _active_recording_info["last_motion_timestamp"] = motion_start

        while True:
            now = time.time()
            # Check if active motion was detected by AI detector / Port 8000
            is_motion = motion_active_func() if motion_active_func else False
            if is_motion:
                _active_recording_info["last_motion_timestamp"] = now

            last_motion = _active_recording_info.get("last_motion_timestamp", motion_start)
            time_since_motion = now - last_motion

            # Stop when motion has ceased for at least postroll_seconds
            if time_since_motion >= postroll_seconds and now >= _active_recording_info.get("extend_until", 0.0):
                logger.info(
                    "Smart clip %s motion ended (silent for %.1fs >= postroll %.1fs). Finalizing recording.",
                    clip_id, time_since_motion, postroll_seconds,
                )
                break

            # Safety cap (default 180s)
            if (now - motion_start) >= max_duration:
                logger.warning(
                    "Smart clip %s reached safety max_duration (%.1fs). Finalizing clip to prevent infinite recording.",
                    clip_id, max_duration,
                )
                break

            await asyncio.sleep(1.0)

            # Ingest newly completed segments
            current_segs = ring_buffer.get_available_segments()
            completed_now = current_segs[:-1] if len(current_segs) > 1 else current_segs

            for epoch, seg_path in completed_now:
                if seg_path not in seen_paths:
                    seen_paths.add(seg_path)
                    raw_collected_segments.append((epoch, seg_path))

        if not raw_collected_segments:
            logger.warning("No segments collected for smart clip %s, falling back to direct record", clip_id)
            direct_cmd = [
                "nice", "-n", "15",
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", "rtsp://127.0.0.1:8554/deurbel",
                "-t", str(int(postroll_seconds)),
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "26",
                "-vf", "scale=1280:960,setsar=1:1",
                "-r", "15",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "64k",
                "-movflags", "+faststart",
                clip_path,
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *direct_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(), timeout=40.0)
            except Exception as e:
                logger.error("Direct record fallback failed for %s: %s", clip_id, e)
            dur = await _probe_duration(clip_path)
            return dur if dur > 0 else 15.0

        # 3. Strict Chronological Sorting & Gap-Sanitization
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

        # 4. Concatenate and assemble MP4: try fast stream-copy first (0% CPU, 10x faster)
        concat_list = work_dir / "concat_list.txt"
        with open(concat_list, "w") as f:
            for seg in copied_files:
                f.write(f"file '{seg.resolve()}'\n")

        cmd_copy = [
            "nice", "-n", "15",
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            "-movflags", "+faststart",
            clip_path,
        ]

        copy_success = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_copy,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if os.path.exists(clip_path) and os.path.getsize(clip_path) > 10000:
                copy_success = True
        except Exception as e:
            logger.debug("Fast copy concat failed or timed out for %s: %s (falling back to transcode)", clip_id, e)

        # Fallback to ultrafast transcode if copy failed or was too small
        if not copy_success:
            cmd_transcode = [
                "nice", "-n", "15",
                "ffmpeg", "-y",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "26",
                "-vf", "scale=1280:960,setsar=1:1",
                "-r", "15",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "64k",
                "-af", "aresample=async=1000",
                "-movflags", "+faststart",
                clip_path,
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd_transcode,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(), timeout=35.0)
            except Exception as e:
                logger.warning("Concat FFmpeg error or timeout for %s: %s", clip_id, e)

        dur = await _probe_duration(clip_path)
        return dur if dur > 0 else float(len(copied_files))

    finally:
        if _active_recording_info.get("clip_id") == clip_id:
            _active_recording_info["clip_id"] = None
            _active_recording_info["extend_until"] = 0.0
            _active_recording_info["last_motion_timestamp"] = 0.0
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
        logger.info("Recording cancelled for %s, terminating FFmpeg raw capture", call_id)
        proc = _active_recorders.get(call_id)
        if proc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
    finally:
        _active_recorders.pop(call_id, None)
        # Finalize and transcode the clip in a detached background task so cancellation does not abort saving
        asyncio.create_task(_finalize_call_recording(call_id, clip_path, preroll_files, raw_ts, call_work_dir))


async def _finalize_call_recording(
    call_id: str,
    clip_path: str,
    preroll_files: list[Path],
    raw_ts: str,
    call_work_dir: Path,
) -> None:
    """Non-cancellable background worker that stitches and saves the completed call clip (v3.5.0)."""
    try:
        all_segments = list(preroll_files)
        if os.path.exists(raw_ts) and os.path.getsize(raw_ts) > 1000:
            all_segments.append(Path(raw_ts))

        if all_segments:
            concat_list = call_work_dir / "concat_call.txt"
            with open(concat_list, "w") as f:
                for s in all_segments:
                    f.write(f"file '{s.resolve()}'\n")

            os.makedirs(os.path.dirname(clip_path), exist_ok=True)
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

        # Probe duration and persist clip metadata
        duration = await _probe_duration(clip_path)
        snapshot_path = await ensure_thumbnail(call_id, clip_path)
        file_size = os.path.getsize(clip_path) if os.path.exists(clip_path) else 0

        # Look up call log to preserve answered_by or trigger_source badge
        ans = "Aanbellen"
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
        if not ans or ans == "Aanbellen":
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
            status="READY",
            category="ring",
            file_size=file_size,
        )
        logger.info("✅ Saved completed call recording: %s (%.1fs, %d bytes)", clip_path, duration, file_size)
    except Exception as e:
        logger.error("Failed finalizing call recording for %s: %s", call_id, e)
    finally:
        shutil.rmtree(call_work_dir, ignore_errors=True)


async def ensure_thumbnail(clip_id: str, video_path: Optional[str] = None, preroll_seconds: float = 20.0) -> str:
    """
    Ensure a thumbnail exists for a clip. If not present on disk, extract
    the frame at preroll_seconds from the MP4 using FFmpeg (v3.5.0 Day-Folder aware).
    """
    # 1. Check if thumbnail already exists anywhere on disk
    resolved_thumb = resolve_clip_path(f"thumb_{clip_id}.jpg", ext=".jpg")
    if resolved_thumb and resolved_thumb.stat().st_size > 1000:
        return str(resolved_thumb)

    resolved_snap = resolve_clip_path(f"{clip_id}.jpg", ext=".jpg")
    if resolved_snap and resolved_snap.stat().st_size > 1000:
        return str(resolved_snap)

    resolved_debug = resolve_clip_path(f"{clip_id}_debug.jpg", ext=".jpg")
    if resolved_debug and resolved_debug.stat().st_size > 1000:
        return str(resolved_debug)

    # 2. Try live_hub frame if available
    day_dir = get_day_clip_dir()
    snapshot_path = str(day_dir / f"thumb_{clip_id}.jpg")
    try:
        from app.live_hub import live_hub
        jpeg = live_hub.get_latest_frame()
        if jpeg and len(jpeg) > 1000:
            with open(snapshot_path, "wb") as f:
                f.write(jpeg)
            return snapshot_path
    except Exception:
        pass

    # 3. Find video path via resolve_clip_path
    if not video_path:
        v_found = resolve_clip_path(clip_id, ext=".mp4")
        if v_found:
            video_path = str(v_found)

    if not video_path or not os.path.exists(video_path):
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
            logger.info("Generated thumbnail from video for %s at ss=%.1fs in %s", clip_id, ss_val, snapshot_path)
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
    """Delete clip files from disk and remove from the database (v3.5.0 Day-Folder aware)."""
    clip = await get_clip(clip_id)
    if not clip:
        return False

    def _safe_remove(fpath: Optional[Path]) -> None:
        if not fpath or not fpath.exists():
            return
        # Never remove APK
        if fpath.name == "foxway-deurbel.apk":
            return
        parent_dir = fpath.parent
        try:
            fpath.unlink(missing_ok=True)
            logger.info("Deleted file: %s", fpath)
        except Exception as e:
            logger.warning("Error deleting file %s: %s", fpath, e)

        # If parent_dir is a day folder (YYYY-MM-DD) and now empty, clean it up
        if parent_dir != settings.CLIPS_DIR and parent_dir.is_dir():
            try:
                if not any(parent_dir.iterdir()):
                    parent_dir.rmdir()
                    logger.info("Cleaned up empty day folder: %s", parent_dir)
            except Exception:
                pass

    # 1. Delete video file
    v_raw = clip.get("video_path")
    v_target = Path(v_raw) if (v_raw and os.path.exists(v_raw)) else resolve_clip_path(f"{clip_id}.mp4", ext=".mp4")
    _safe_remove(v_target)

    # 2. Delete snapshot/thumbnail file
    s_raw = clip.get("snapshot_path")
    s_target = Path(s_raw) if (s_raw and os.path.exists(s_raw)) else resolve_clip_path(f"thumb_{clip_id}.jpg", ext=".jpg")
    _safe_remove(s_target)

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
    day_dir = get_day_clip_dir()
    clip_path = str(day_dir / clip_filename)

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


def get_storage_stats(max_storage_gb: float = 50.0) -> dict:
    """Calculate disk usage of clips directory (including day subfolders, v3.5.0 50GB limit)."""
    clips_dir = settings.CLIPS_DIR
    total_bytes = 0
    clip_count = 0
    if clips_dir.exists():
        for f in clips_dir.rglob("*.mp4"):
            try:
                # Exclude temporary or non-clip files
                if f.name.endswith(".apk"):
                    continue
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


async def cleanup_storage_and_retention(retention_days: int = 30, max_storage_gb: float = 50.0) -> dict:
    """
    Two-tier retention and storage protection (v3.5.0):
      1. Purge unlocked clips older than retention_days.
      2. If total size exceeds max_storage_gb, purge oldest unlocked clips until within cap.
    Starred/locked clips and the APK file are NEVER removed.
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

    total_bytes = sum(f.stat().st_size for f in settings.CLIPS_DIR.rglob("*.mp4") if f.exists() and not f.name.endswith(".apk"))
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
    stats = await cleanup_storage_and_retention(settings.CLIP_RETENTION_DAYS, 50.0)
    return stats.get("removed_count", 0)


async def sweep_stuck_recordings(max_age_seconds: float = 40.0) -> int:
    """
    Sweeper that finalizes or cleans up recordings stuck in 'RECORDING' state older than max_age_seconds (v3.5.1).
    - If .mp4 file exists and size > 10 KB: updates status to READY, probes duration, ensures thumbnail.
    - If .mp4 file is missing, empty, or <= 10 KB: deletes the row from database.
    Returns the number of clips repaired or purged.
    """
    fixed_count = 0
    try:
        from app.database import get_db
        async with get_db(write=False) as db:
            cursor = await db.execute(
                "SELECT clip_id, video_path, created_at, snapshot_path, category, answered_by "
                "FROM clips WHERE status = 'RECORDING'"
            )
            rows = await cursor.fetchall()

        if not rows:
            return 0

        now_t = time.time()
        for row in rows:
            clip_id = row["clip_id"]
            created_at_str = row["created_at"]
            age_sec = 0.0
            try:
                dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                age_sec = (datetime.now(timezone.utc) - dt).total_seconds()
            except Exception:
                age_sec = max_age_seconds + 1.0

            if age_sec < max_age_seconds:
                continue

            # Found a stuck recording older than max_age_seconds
            vpath = row["video_path"]
            resolved_v = resolve_clip_path(vpath or clip_id, ext=".mp4")

            if resolved_v and resolved_v.exists() and resolved_v.stat().st_size > 10000:
                size_bytes = resolved_v.stat().st_size
                dur = await _probe_duration(str(resolved_v))
                if dur <= 0:
                    dur = 15.0
                thumb = await ensure_thumbnail(clip_id, str(resolved_v))
                await update_clip(
                    clip_id=clip_id,
                    status="READY",
                    duration=dur,
                    file_size=size_bytes,
                    video_path=str(resolved_v),
                    snapshot_path=thumb or row["snapshot_path"],
                )
                logger.info("🧹 [Sweeper] Repaired stuck recording %s -> READY (age=%.1fs, dur=%.1fs, size=%d)",
                            clip_id, age_sec, dur, size_bytes)
                fixed_count += 1
            else:
                await db_delete_clip(clip_id)
                logger.info("🧹 [Sweeper] Purged incomplete/empty stuck recording %s (age=%.1fs)", clip_id, age_sec)
                fixed_count += 1
    except Exception as e:
        logger.debug("Error running sweep_stuck_recordings: %s", e)
    return fixed_count


