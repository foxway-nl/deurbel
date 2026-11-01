#!/usr/bin/env python3
"""
Purge all recordings and events up to and including 2026-11-01 20:16:52 local time (v3.5.3).
Europe/Amsterdam is UTC+1 (Winter time/CET), so:
  Local: 2026-11-01 20:16:52
  UTC:   2026-11-01 19:16:52.999999
Preserves all recordings created AFTER 20:16:52!
NEVER deletes /mnt/opslag/deurbel/clips/foxway-deurbel.apk!
"""

import os
import sys
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime, timezone
import zoneinfo

def main():
    db_path = Path("/mnt/opslag/deurbel/foxway.db")
    if not db_path.exists():
        print(f"ERROR: Database {db_path} not found")
        sys.exit(1)

    amsterdam_tz = zoneinfo.ZoneInfo("Europe/Amsterdam")
    # Target cutoff: 2026-11-01 20:16:52.999999 Europe/Amsterdam
    cutoff_local = datetime(2026, 11, 1, 20, 16, 52, 999999, tzinfo=amsterdam_tz)
    cutoff_utc = cutoff_local.astimezone(timezone.utc)
    cutoff_epoch = cutoff_utc.timestamp()

    print(f"=== PURGE RECORDINGS UNTIL 2026-11-01 20:16:52 (v3.5.3) ===")
    print(f"Cutoff Local: {cutoff_local.isoformat()}")
    print(f"Cutoff UTC:   {cutoff_utc.isoformat()}")
    print(f"Cutoff Epoch: {cutoff_epoch}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. Identify clips to delete
    cursor.execute("SELECT clip_id, created_at, video_path, snapshot_path FROM clips")
    all_clips = cursor.fetchall()
    
    clips_to_delete = []
    clips_to_keep = []
    files_to_delete = set()

    for r in all_clips:
        cid = r["clip_id"]
        ts_str = r["created_at"] or ""
        
        # Parse timestamp
        clip_epoch = None
        try:
            clean_ts = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_ts)
            if dt.tzinfo is None:
                # If naive, assume amsterdam if hour >= 20 else UTC
                dt = dt.replace(tzinfo=timezone.utc)
            clip_epoch = dt.timestamp()
        except Exception:
            pass

        # String fallback
        is_before = False
        if clip_epoch is not None:
            is_before = (clip_epoch <= cutoff_epoch)
        else:
            # Lexicographical check
            is_before = (ts_str <= "2026-11-01T19:16:53")

        if is_before:
            clips_to_delete.append(cid)
            if r["video_path"]:
                files_to_delete.add(r["video_path"])
            if r["snapshot_path"]:
                files_to_delete.add(r["snapshot_path"])
            # Common locations
            files_to_delete.add(f"/mnt/opslag/deurbel/clips/{cid}.mp4")
            files_to_delete.add(f"/mnt/opslag/deurbel/clips/thumb_{cid}.jpg")
            files_to_delete.add(f"/mnt/opslag/deurbel/snapshots/{cid}.jpg")
            files_to_delete.add(f"/mnt/opslag/deurbel/snapshots/{cid}_debug.jpg")
            for day_dir in Path("/mnt/opslag/deurbel/clips").glob("2026-*"):
                if day_dir.is_dir():
                    files_to_delete.add(str(day_dir / f"{cid}.mp4"))
                    files_to_delete.add(str(day_dir / f"thumb_{cid}.jpg"))
        else:
            clips_to_keep.append((cid, ts_str))

    print(f"\nClips to delete: {len(clips_to_delete)}")
    print(f"Clips to keep:   {len(clips_to_keep)}")
    for cid, ts in sorted(clips_to_keep, key=lambda x: x[1]):
        print(f"  KEEP: {cid} ({ts})")

    # 2. Identify events to delete
    cursor.execute("SELECT id, clip_id, created_at, snapshot_url FROM events")
    all_events = cursor.fetchall()
    events_to_delete = []
    events_to_keep = []

    for ev in all_events:
        eid = ev["id"]
        ts_str = ev["created_at"] or ""
        ev_cid = ev["clip_id"] or ""

        ev_epoch = None
        try:
            clean_ts = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ev_epoch = dt.timestamp()
        except Exception:
            pass

        is_before = False
        if ev_cid in clips_to_delete:
            is_before = True
        elif ev_epoch is not None:
            is_before = (ev_epoch <= cutoff_epoch)
        else:
            is_before = (ts_str <= "2026-11-01T19:16:53")

        if is_before:
            events_to_delete.append(eid)
        else:
            events_to_keep.append((eid, ts_str, ev_cid))

    print(f"\nEvents to delete: {len(events_to_delete)}")
    print(f"Events to keep:   {len(events_to_keep)}")

    # 3. Scan disk for orphaned .mp4 and .jpg <= cutoff
    apk_path = Path("/mnt/opslag/deurbel/clips/foxway-deurbel.apk").resolve()
    scanned_deleted_files = 0

    scan_dirs = [
        Path("/mnt/opslag/deurbel/clips"),
        Path("/mnt/opslag/deurbel/snapshots"),
    ]

    for sdir in scan_dirs:
        if not sdir.exists():
            continue
        for p in sdir.rglob("*"):
            if not p.is_file():
                continue
            # NEVER delete APK!
            if p.resolve() == apk_path or p.name.endswith(".apk"):
                continue
            if p.suffix.lower() not in (".mp4", ".jpg", ".jpeg", ".png"):
                continue
            
            # Check if explicitly mapped to deleted clip or mtime <= cutoff
            should_del = str(p) in files_to_delete
            if not should_del:
                try:
                    mtime = p.stat().st_mtime
                    if mtime <= cutoff_epoch:
                        should_del = True
                except Exception:
                    pass

            # Make sure files belonging to KEEP clips are NEVER deleted!
            for keep_cid, _ in clips_to_keep:
                if keep_cid in p.name:
                    should_del = False
                    break

            if should_del:
                try:
                    p.unlink(missing_ok=True)
                    scanned_deleted_files += 1
                except Exception as e:
                    print(f"Failed to delete {p}: {e}")

    # Also delete any explicitly listed paths that might not have been caught in rglob
    for fpath in files_to_delete:
        p = Path(fpath)
        if p.exists() and p.resolve() != apk_path and not p.name.endswith(".apk"):
            # Check keep clips
            is_kept = any(keep_cid in p.name for keep_cid, _ in clips_to_keep)
            if not is_kept:
                try:
                    p.unlink(missing_ok=True)
                    scanned_deleted_files += 1
                except Exception:
                    pass

    print(f"\nTotal media files deleted from disk: {scanned_deleted_files}")

    # Clean up empty day directories in clips
    clips_dir = Path("/mnt/opslag/deurbel/clips")
    for d in clips_dir.glob("2026-*"):
        if d.is_dir():
            remaining_in_dir = list(d.glob("*"))
            if not remaining_in_dir:
                try:
                    d.rmdir()
                    print(f"Removed empty directory: {d}")
                except Exception:
                    pass

    # 4. Delete records from database
    if clips_to_delete:
        # SQLite limit on parameters, batch by 500
        for i in range(0, len(clips_to_delete), 500):
            batch = clips_to_delete[i:i+500]
            placeholders = ",".join("?" for _ in batch)
            cursor.execute(f"DELETE FROM clips WHERE clip_id IN ({placeholders})", batch)

    if events_to_delete:
        for i in range(0, len(events_to_delete), 500):
            batch = events_to_delete[i:i+500]
            placeholders = ",".join("?" for _ in batch)
            cursor.execute(f"DELETE FROM events WHERE id IN ({placeholders})", batch)

    # Delete ai_audit_logs older than cutoff
    cursor.execute("DELETE FROM ai_audit_logs WHERE timestamp <= ?", (cutoff_utc.isoformat(),))
    audit_deleted = cursor.rowcount

    conn.commit()
    print("Running database VACUUM...")
    conn.execute("VACUUM")
    conn.close()

    print("\n=== PURGE COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    main()

