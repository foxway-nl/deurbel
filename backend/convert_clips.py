#!/usr/bin/env python3
"""
Convert existing MP4 clips in /mnt/opslag/deurbel/clips/ to H.264 (yuv420p, 720p) + AAC + faststart.
Ensures zero black screen recordings and reduces file size to 1-2 MB.
"""

import os
import subprocess
import sys
from pathlib import Path

CLIPS_DIR = Path("/mnt/opslag/deurbel/clips")

def main():
    if not CLIPS_DIR.exists():
        print(f"Directory {CLIPS_DIR} does not exist.")
        return

    clips = sorted([f for f in CLIPS_DIR.glob("*.mp4") if not f.name.endswith("_tmp.mp4")])
    print(f"Found {len(clips)} MP4 files to inspect...")

    converted = 0
    skipped = 0
    removed = 0

    for idx, clip in enumerate(clips, 1):
        if clip.stat().st_size == 0:
            print(f"[{idx}/{len(clips)}] Removing 0-byte file: {clip.name}")
            try:
                clip.unlink()
                removed += 1
            except Exception:
                pass
            continue

        # Check existing video format via ffprobe
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt,height",
            "-of", "csv=p=0",
            str(clip)
        ]
        try:
            res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            output = res.stdout.strip()
            # If already h264, yuv420p and height <= 720, skip
            parts = output.split(",")
            if len(parts) >= 3:
                codec, pix_fmt, height = parts[0], parts[1], parts[2]
                if codec == "h264" and pix_fmt == "yuv420p" and height.isdigit() and int(height) <= 720:
                    skipped += 1
                    continue
        except Exception:
            pass

        tmp_clip = clip.parent / f"{clip.stem}_conv_tmp.mp4"
        print(f"[{idx}/{len(clips)}] Converting {clip.name} ({clip.stat().st_size // (1024*1024)} MB)...")

        cmd = [
            "nice", "-n", "15",
            "ffmpeg", "-y",
            "-i", str(clip),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-vf", "scale=-2:720",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "64k",
            "-movflags", "+faststart",
            str(tmp_clip)
        ]

        try:
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if p.returncode == 0 and tmp_clip.exists() and tmp_clip.stat().st_size > 1000:
                tmp_clip.replace(clip)
                converted += 1
                print(f"   Done -> {clip.stat().st_size // 1024} KB")
            else:
                if tmp_clip.exists():
                    tmp_clip.unlink()
                print(f"   Failed to convert {clip.name}")
        except Exception as e:
            if tmp_clip.exists():
                tmp_clip.unlink()
            print(f"   Error converting {clip.name}: {e}")

    # Also clean up any lingering _tmp.mp4 files
    for tmp in CLIPS_DIR.glob("*_tmp.mp4"):
        try:
            tmp.unlink()
        except Exception:
            pass

    print(f"\nFinished: converted {converted}, skipped {skipped}, removed {removed} empty files.")

if __name__ == "__main__":
    main()
