#!/usr/bin/env python3
"""
Foxway Smart Doorbell — v3.5.2 Fast Clip & Thumbnail Repair Script
Single-pass directory index to repair duration and missing thumbnails in seconds.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path("/mnt/opslag/deurbel")
CLIPS_DIR = BASE_DIR / "clips"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
DB_PATH = BASE_DIR / "foxway.db"


def probe_duration(file_path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def extract_thumbnail_from_mp4(mp4_path: Path, output_thumb: Path, seek_sec: float = 3.0) -> bool:
    output_thumb.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "nice", "-n", "15",
        "ffmpeg", "-y",
        "-ss", str(seek_sec),
        "-i", str(mp4_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_thumb)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
        return output_thumb.exists() and output_thumb.stat().st_size > 1000
    except Exception:
        try:
            cmd[3] = "0"
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
            return output_thumb.exists() and output_thumb.stat().st_size > 1000
        except Exception:
            return False


def main():
    print("Building file index...", flush=True)
    mp4_map = {}
    jpg_map = {}

    for root, _, files in os.walk(CLIPS_DIR):
        for f in files:
            p = Path(root) / f
            if f.endswith(".mp4"):
                mp4_map[p.stem] = p
            elif f.endswith(".jpg"):
                jpg_map[p.name] = p

    print(f"Indexed {len(mp4_map)} MP4s and {len(jpg_map)} JPGs in clips dir.", flush=True)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    cursor.execute("SELECT clip_id, created_at, duration, file_size FROM clips WHERE duration <= 0.0 OR duration IS NULL")
    zero_clips = cursor.fetchall()
    print(f"Found {len(zero_clips)} clips with duration <= 0.0 in DB.", flush=True)

    repaired_duration = 0
    repaired_thumbs = 0

    for clip_id, created_at, dur, fsize in zero_clips:
        mp4_path = mp4_map.get(clip_id)
        if not mp4_path:
            for stem, path in mp4_map.items():
                if clip_id in stem:
                    mp4_path = path
                    break

        if mp4_path and mp4_path.exists():
            actual_dur = probe_duration(mp4_path)
            actual_size = mp4_path.stat().st_size

            if actual_dur > 0:
                cursor.execute(
                    "UPDATE clips SET duration = ?, file_size = ?, status = 'READY' WHERE clip_id = ?",
                    (round(actual_dur, 3), actual_size, clip_id)
                )
                repaired_duration += 1
                print(f"  ✓ Repaired duration for {clip_id}: {actual_dur:.1f}s ({actual_size} bytes)", flush=True)

    conn.commit()

    # Thumbnail check
    cursor.execute("SELECT clip_id, created_at FROM clips WHERE status = 'READY'")
    all_ready = cursor.fetchall()
    print(f"Checking thumbnails for {len(all_ready)} READY clips...", flush=True)

    for clip_id, created_at in all_ready:
        thumb_name1 = f"thumb_{clip_id}.jpg"
        thumb_name2 = f"{clip_id}.jpg"

        p1 = jpg_map.get(thumb_name1)
        p2 = jpg_map.get(thumb_name2)

        has_valid = (p1 and p1.stat().st_size > 2000) or (p2 and p2.stat().st_size > 2000)

        if not has_valid:
            mp4_path = mp4_map.get(clip_id)
            if mp4_path and mp4_path.exists():
                target_thumb = mp4_path.parent / thumb_name1
                root_thumb = CLIPS_DIR / thumb_name1
                if extract_thumbnail_from_mp4(mp4_path, target_thumb):
                    try:
                        shutil.copy2(target_thumb, root_thumb)
                    except Exception:
                        pass
                    repaired_thumbs += 1
                    print(f"  🖼️ Extracted thumbnail for {clip_id} from {mp4_path.name}", flush=True)

    conn.close()
    print(f"Done! Repaired {repaired_duration} clip durations and {repaired_thumbs} thumbnails.", flush=True)


if __name__ == "__main__":
    main()
