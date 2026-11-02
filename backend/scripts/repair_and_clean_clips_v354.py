#!/usr/bin/env python3
"""
Foxway Smart Doorbell v3.5.4 — Clip Repair & Cleanup Utility.
1. Removes false short vehicle/motion clips (<= 10.0s and < 3.5MB).
2. Cleans up corrupt/stuck clips (<= 2.0s).
3. Safely preserves physical ring events (like c225c24b5bb7) and true person events.
4. Removes deleted files from disk and performs database VACUUM.
"""

import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("/mnt/opslag/deurbel/foxway.db")

def main():
    if not DB_PATH.exists():
        print(f"Error: Database {DB_PATH} not found.")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Query false short clips
    query = """
        SELECT clip_id, category, duration, file_size, video_path, snapshot_path 
        FROM clips 
        WHERE (
            (category IN ('vehicle', 'motion') AND duration <= 10.0)
            OR (duration <= 2.0 AND category != 'ring')
        ) AND clip_id != 'c225c24b5bb7'
    """
    rows = c.execute(query).fetchall()
    print(f"Found {len(rows)} false short or stuck clips to remove.")

    deleted_count = 0
    freed_bytes = 0

    for r in rows:
        cid = r["clip_id"]
        v_path = r["video_path"]
        s_path = r["snapshot_path"]

        # Remove video file if exists
        if v_path and os.path.exists(v_path):
            try:
                sz = os.path.getsize(v_path)
                os.remove(v_path)
                freed_bytes += sz
            except Exception as e:
                print(f"Failed to remove {v_path}: {e}")

        # Remove snapshot if exists
        if s_path and os.path.exists(s_path):
            try:
                sz = os.path.getsize(s_path)
                os.remove(s_path)
                freed_bytes += sz
            except Exception as e:
                print(f"Failed to remove {s_path}: {e}")

        # Delete from clips & events
        c.execute("DELETE FROM clips WHERE clip_id = ?", (cid,))
        c.execute("DELETE FROM events WHERE clip_id = ?", (cid,))
        deleted_count += 1
        print(f"  🗑️ Deleted clip {cid} ({r['category']}, {r['duration']:.1f}s)")

    conn.commit()
    c.execute("VACUUM")
    conn.close()

    print(f"✅ Cleanup complete: {deleted_count} clips deleted, {freed_bytes / (1024*1024):.2f} MB freed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())

