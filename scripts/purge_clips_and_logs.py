#!/usr/bin/env python3
"""
Foxway Smart Doorbell — Purge All Recorded Clips and Saved Logs.
Safely cleans all recorded clips, thumbnails, snapshots, and log files,
while strictly preserving:
1. foxway-deurbel.apk in /mnt/opslag/deurbel/clips/
2. Paired devices and settings in /mnt/opslag/deurbel/foxway.db
"""

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

CLIPS_DIR = Path("/mnt/opslag/deurbel/clips")
SNAPSHOTS_DIR = Path("/mnt/opslag/deurbel/snapshots")
LOGS_DIR = Path("/mnt/opslag/deurbel/logs")
DB_PATH = Path("/mnt/opslag/deurbel/foxway.db")


def purge_database():
    print("🧹 Cleaning database tables...")
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    cursor = conn.cursor()

    tables = ["clips", "events", "call_logs", "ai_audit_logs", "crash_logs", "sdk_alarm_logs"]
    for tbl in tables:
        try:
            cursor.execute(f"DELETE FROM {tbl};")
            print(f"  ✅ Cleared table: {tbl}")
        except Exception as e:
            print(f"  ⚠️ Error clearing {tbl}: {e}")

    conn.commit()
    try:
        cursor.execute("VACUUM;")
        print("  ✅ Database vacuumed.")
    except Exception as e:
        print(f"  ⚠️ Error during vacuum: {e}")
    conn.close()


def purge_clips():
    print("🧹 Cleaning clips directory...")
    deleted_files = 0
    deleted_dirs = 0

    if CLIPS_DIR.exists():
        for item in list(CLIPS_DIR.iterdir()):
            if item.is_file():
                if item.name == "foxway-deurbel.apk":
                    print(f"  🛡️ Preserved: {item.name}")
                    continue
                try:
                    item.unlink()
                    deleted_files += 1
                except Exception as e:
                    print(f"  ⚠️ Failed to delete {item}: {e}")
            elif item.is_dir():
                # Subdirectory (e.g. YYYY-MM-DD)
                try:
                    shutil.rmtree(item)
                    deleted_dirs += 1
                except Exception as e:
                    print(f"  ⚠️ Failed to delete dir {item}: {e}")

    print(f"  ✅ Deleted {deleted_files} clip/thumbnail files and {deleted_dirs} date directories.")


def purge_snapshots():
    print("🧹 Cleaning snapshots directory...")
    deleted_files = 0
    if SNAPSHOTS_DIR.exists():
        for item in list(SNAPSHOTS_DIR.iterdir()):
            if item.is_file():
                try:
                    item.unlink()
                    deleted_files += 1
                except Exception as e:
                    print(f"  ⚠️ Failed to delete {item}: {e}")

    print(f"  ✅ Deleted {deleted_files} snapshot files.")


def purge_logs():
    print("🧹 Cleaning logs directory...")
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_log_name = f"{today_str}.jsonl"
    deleted_files = 0

    if LOGS_DIR.exists():
        for item in list(LOGS_DIR.iterdir()):
            if item.is_file():
                if item.name == today_log_name:
                    # Truncate today's log to 0 bytes so current open file handles can still write cleanly
                    try:
                        with open(item, "w") as f:
                            f.truncate(0)
                        print(f"  ✅ Truncated today's log to 0 bytes: {item.name}")
                    except Exception as e:
                        print(f"  ⚠️ Failed to truncate {item}: {e}")
                else:
                    try:
                        item.unlink()
                        deleted_files += 1
                    except Exception as e:
                        print(f"  ⚠️ Failed to delete log {item}: {e}")
            elif item.is_dir() and item.name == "hcnetsdk_native":
                for subitem in list(item.iterdir()):
                    if subitem.is_file():
                        try:
                            with open(subitem, "w") as f:
                                f.truncate(0)
                            print(f"  ✅ Truncated native SDK log: {subitem.name}")
                        except Exception as e:
                            print(f"  ⚠️ Failed to truncate {subitem}: {e}")

    print(f"  ✅ Deleted {deleted_files} archived log files.")


def main():
    print("======================================================================")
    print("🚀 FOXWAY DEURBEL PURGE SCRIPT")
    print("======================================================================")
    purge_database()
    purge_clips()
    purge_snapshots()
    purge_logs()
    print("======================================================================")
    print("✨ ALL CLIPS, SNAPSHOTS, AND LOGS PURGED SUCCESSFULLY!")
    print("======================================================================")


if __name__ == "__main__":
    main()

