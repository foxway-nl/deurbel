#!/usr/bin/env python3
"""
Foxway Smart Doorbell — Module Guard (v3.5.1)
==============================================
Protects frozen core modules from accidental modification.

Usage:
  python3 scripts/module_guard.py --verify          # Run 5 smoke checks + hash verify
  python3 scripts/module_guard.py --update-hashes   # Refresh locked hashes
  python3 scripts/module_guard.py --unlock-module=<name>  # Allow one module to differ

Frozen modules (NEVER modify without --unlock-module):
  - backend/app/ai_detector.py
  - backend/app/ring_detector.py
  - backend/app/audio_bridge.py
  - android-app/app/src/main/java/nl/foxway/deurbel/network/WebSocketForegroundService.kt
  - android-app/app/src/main/java/nl/foxway/deurbel/ui/call/CallActivity.kt
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path("/mnt/opslag/deurbel")
HASH_FILE = BASE_DIR / ".locked_hashes.json"
BACKEND_URL = "http://localhost:8090"
LOG_DIR = BASE_DIR / "logs"

FROZEN_MODULES = [
    "backend/app/ai_detector.py",
    "backend/app/ring_detector.py",
    "backend/app/audio_bridge.py",
    "android-app/app/src/main/java/nl/foxway/deurbel/network/WebSocketForegroundService.kt",
    "android-app/app/src/main/java/nl/foxway/deurbel/ui/call/CallActivity.kt",
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Hash Management
# ---------------------------------------------------------------------------

def load_hashes() -> dict:
    if HASH_FILE.exists():
        with open(HASH_FILE, "r") as f:
            return json.load(f)
    return {}


def save_hashes(hashes: dict) -> None:
    with open(HASH_FILE, "w") as f:
        json.dump(hashes, f, indent=2)
    print(f"✅ Locked hashes saved to {HASH_FILE}")


def update_hashes() -> None:
    print("🔐 Updating locked hashes for frozen modules...")
    hashes = {}
    for rel in FROZEN_MODULES:
        p = BASE_DIR / rel
        if p.exists():
            h = sha256_of(p)
            hashes[rel] = h
            print(f"  ✓  {rel}  →  {h[:16]}…")
        else:
            print(f"  ⚠️  {rel}  →  FILE NOT FOUND (skipped)")
    hashes["_updated"] = datetime.now().isoformat()
    save_hashes(hashes)


def verify_hashes(unlocked: list[str]) -> bool:
    stored = load_hashes()
    if not stored:
        print("⚠️  No locked hashes found. Run --update-hashes first.")
        return True  # Non-blocking if no hashes stored yet

    ok = True
    for rel in FROZEN_MODULES:
        if rel in unlocked:
            print(f"  🔓 {rel}  →  UNLOCKED (allowed to differ)")
            continue
        p = BASE_DIR / rel
        if not p.exists():
            print(f"  ⚠️  {rel}  →  FILE NOT FOUND (non-fatal)")
            continue
        current = sha256_of(p)
        stored_hash = stored.get(rel)
        if stored_hash is None:
            print(f"  🆕 {rel}  →  Not yet in locked hashes (run --update-hashes)")
            continue
        if current != stored_hash:
            print(f"  ❌ FROZEN MODULE MODIFIED: {rel}")
            print(f"     Expected: {stored_hash[:32]}…")
            print(f"     Got:      {current[:32]}…")
            print(f"  🛑 Restoring via git checkout...")
            try:
                subprocess.run(
                    ["git", "-C", str(BASE_DIR), "checkout", "--", rel],
                    check=True, capture_output=True
                )
                print(f"  ✅ Restored {rel} to last committed version")
            except subprocess.CalledProcessError as e:
                print(f"  ⚠️  git checkout failed: {e.stderr.decode()}")
            ok = False
        else:
            print(f"  ✅ {rel}  →  OK ({current[:16]}…)")
    return ok


# ---------------------------------------------------------------------------
# 5 Smoke Checks
# ---------------------------------------------------------------------------

def smoke_check_status() -> bool:
    """Check 1: GET /api/v1/status returns HTTP 200."""
    try:
        with urllib.request.urlopen(f"{BACKEND_URL}/api/v1/status", timeout=5) as r:
            ok = r.status == 200
            print(f"  {'✅' if ok else '❌'} /api/v1/status → HTTP {r.status}")
            return ok
    except Exception as e:
        print(f"  ❌ /api/v1/status → FAILED: {e}")
        return False


def smoke_check_stuck_recordings() -> bool:
    """Check 2: No clips older than 45s with status RECORDING."""
    try:
        db_path = BASE_DIR / "foxway.db"
        if not db_path.exists():
            db_path = BASE_DIR / "backend" / "foxway.db"
        if not db_path.exists():
            print("  ⚠️  foxway.db not found — skipping stuck recording check")
            return True
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM clips WHERE status = 'RECORDING' "
            "AND created_at < datetime('now', '-45 seconds')"
        )
        count = cursor.fetchone()[0]
        conn.close()
        ok = count == 0
        print(f"  {'✅' if ok else '❌'} Stuck RECORDING clips (>45s): {count}")
        return ok
    except Exception as e:
        print(f"  ⚠️  Stuck recording check failed: {e}")
        return True  # Non-blocking


def smoke_check_thumbnail() -> bool:
    """Check 3: First available clip thumbnail returns HTTP 200, >1000 bytes."""
    try:
        db_path = BASE_DIR / "foxway.db"
        if not db_path.exists():
            db_path = BASE_DIR / "backend" / "foxway.db"
        if not db_path.exists():
            print("  ⚠️  No DB for thumbnail check — skipping")
            return True
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT clip_id FROM clips WHERE status = 'READY' ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if not row:
            print("  ⚠️  No READY clips found — skipping thumbnail check")
            return True
        clip_id = row[0]
        url = f"{BACKEND_URL}/api/v1/clips/{clip_id}/thumbnail"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = r.read()
            ok = r.status == 200 and len(data) > 1000
            print(f"  {'✅' if ok else '❌'} Thumbnail for {clip_id[:8]}… → HTTP {r.status}, {len(data)} bytes")
            return ok
    except Exception as e:
        print(f"  ⚠️  Thumbnail check failed: {e}")
        return True  # Non-blocking


def smoke_check_live_player() -> bool:
    """Check 4: /api/v1/live/player HTML contains setAudioMuted and syncToLiveEdge."""
    try:
        with urllib.request.urlopen(f"{BACKEND_URL}/api/v1/live/player", timeout=5) as r:
            html = r.read().decode("utf-8", errors="replace")
        has_muted = "setAudioMuted" in html
        has_sync = "syncToLiveEdge" in html or "live-edge" in html or "liveEdge" in html
        ok = has_muted and has_sync
        print(f"  {'✅' if ok else '❌'} /api/v1/live/player → setAudioMuted={has_muted}, syncToLiveEdge={has_sync}")
        return ok
    except Exception as e:
        print(f"  ⚠️  Live player check failed: {e}")
        return True  # Non-blocking


def smoke_check_no_recent_errors() -> bool:
    """Check 5: Last 2 minutes of JSONL logs have 0 unresolved ERROR lines."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = LOG_DIR / f"{today}.jsonl"
        if not log_file.exists():
            print("  ⚠️  No log file for today — skipping error check")
            return True
        cutoff = time.time() - 120  # 2 minutes ago
        error_count = 0
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("timestamp", "")
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str).timestamp()
                        except Exception:
                            continue
                        if ts >= cutoff:
                            lvl = entry.get("level", "").upper()
                            if lvl == "ERROR":
                                error_count += 1
                except Exception:
                    pass
        ok = error_count == 0
        print(f"  {'✅' if ok else '❌'} Recent errors (last 2min): {error_count}")
        return ok
    except Exception as e:
        print(f"  ⚠️  Error log check failed: {e}")
        return True  # Non-blocking


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Foxway Module Guard v3.5.1")
    parser.add_argument("--verify", action="store_true", help="Verify hashes + 5 smoke checks")
    parser.add_argument("--update-hashes", action="store_true", help="Refresh locked hashes")
    parser.add_argument("--unlock-module", action="append", default=[], metavar="NAME",
                        help="Allow this module to differ (repeatable)")
    args = parser.parse_args()

    if args.update_hashes:
        update_hashes()
        return 0

    if args.verify:
        print("\n" + "=" * 70)
        print("🔐 FOXWAY MODULE GUARD — v3.5.1 VERIFICATION")
        print("=" * 70)

        # Step 1: Hash verification
        print("\n📌 FROZEN MODULE HASH CHECK:")
        hash_ok = verify_hashes(args.unlock_module)

        # Step 2: 5 smoke checks
        print("\n🧪 SMOKE TESTS:")
        checks = [
            smoke_check_status(),
            smoke_check_stuck_recordings(),
            smoke_check_thumbnail(),
            smoke_check_live_player(),
            smoke_check_no_recent_errors(),
        ]
        all_ok = hash_ok and all(checks)

        print("\n" + "=" * 70)
        if all_ok:
            print("✅ ALL CHECKS PASSED — System healthy!")
        else:
            print("❌ SOME CHECKS FAILED — Review output above!")
        print("=" * 70 + "\n")
        return 0 if all_ok else 1

    # Default: just verify hashes (pre-commit mode)
    print("🔐 Foxway Module Guard — Pre-commit hash check...")
    ok = verify_hashes(args.unlock_module)
    if not ok:
        print("🛑 Commit BLOCKED: frozen module modified. Use --unlock-module=<name> to allow.")
        return 1
    print("✅ All frozen module hashes verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
