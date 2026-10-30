#!/usr/bin/env python3
"""
Foxway Smart Doorbell — Black-Box Log Triage & Diagnostics Tool.

Parses today's /mnt/opslag/deurbel/logs/YYYY-MM-DD.jsonl and native C-SDK logs:
- Summarizes errors and warnings.
- Reports all C-SDK Port 8000 packets.
- Reports all Shazam chime cross-correlation scores.
- Reports all AI detections and notification suppressions.
"""

from __future__ import annotations

import collections
import datetime
import glob
import json
import os
import sys

LOG_DIR = "/mnt/opslag/deurbel/logs"
NATIVE_LOG_DIR = "/mnt/opslag/deurbel/logs/hcnetsdk_native"


def run_triage():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    jsonl_path = os.path.join(LOG_DIR, f"{today}.jsonl")

    print("=" * 70)
    print(f"🔍 FOXWAY SMART DOORBELL BLACK-BOX TRIAGE REPORT — {today}")
    print("=" * 70)

    if not os.path.exists(jsonl_path):
        print(f"⚠️  No JSONL log file found at {jsonl_path} for today.")
    else:
        print(f"📁 Parsing log file: {jsonl_path} ({os.path.getsize(jsonl_path):,} bytes)")

    categories = collections.Counter()
    levels = collections.Counter()
    errors = []
    warnings = []
    sdk_commands = collections.Counter()
    sdk_actions = collections.Counter()
    shazam_events = []
    ai_events = []
    client_dumps = []

    if os.path.exists(jsonl_path):
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception as e:
                    print(f"Error parsing line {line_no}: {e}")
                    continue

                cat = entry.get("category", "UNKNOWN")
                lvl = entry.get("level", "INFO")
                data = entry.get("data", {})
                ts = entry.get("timestamp", "")

                categories[cat] += 1
                levels[lvl] += 1

                if lvl == "ERROR":
                    errors.append((ts, cat, entry.get("event"), data))
                elif lvl == "WARNING":
                    warnings.append((ts, cat, entry.get("event"), data))

                if cat == "C-SDK":
                    cmd_hex = data.get("command_hex", "unknown")
                    cmd_name = data.get("command_name", "")
                    action = data.get("action_taken", "")
                    sdk_commands[f"{cmd_hex} ({cmd_name})"] += 1
                    sdk_actions[action] += 1

                elif cat == "SHAZAM":
                    ncc = data.get("ncc", 0.0)
                    matched = data.get("matched", False)
                    shazam_events.append((ts, ncc, matched, data.get("details", "")))

                elif cat == "AI":
                    ai_events.append((ts, data.get("category"), data.get("label"), data.get("silent"), data.get("action")))

                elif cat == "CLIENT_DUMP":
                    client_dumps.append((ts, data))

    print("\n📊 1. LOG VOLUME BY CATEGORY:")
    for cat, count in categories.most_common():
        print(f"   - {cat:<15}: {count:>5} events")

    print("\n🚨 2. ERRORS & WARNINGS SUMMARY:")
    print(f"   - Total Errors  : {len(errors)}")
    print(f"   - Total Warnings: {len(warnings)}")
    for ts, cat, ev, d in errors[:5]:
        print(f"     [ERROR] {ts} [{cat}] {ev}: {d}")
    for ts, cat, ev, d in warnings[:5]:
        print(f"     [WARN ] {ts} [{cat}] {ev}: {d}")

    print("\n🔔 3. HIKVISION POORT 8000 C-SDK PACKETS:")
    if not sdk_commands:
        print("   - No C-SDK packets recorded yet today.")
    else:
        for cmd, cnt in sdk_commands.most_common():
            print(f"   - {cmd:<45}: {cnt:>5}x")
        print("   Actions taken:")
        for act, cnt in sdk_actions.most_common():
            print(f"     * {act:<35}: {cnt:>5}x")

    print("\n🎵 4. SHAZAM CHIME CROSS-CORRELATION SCORES:")
    if not shazam_events:
        print("   - No high-NCC chime evaluations logged yet (threshold >= 0.45).")
    else:
        print(f"   - Total evaluations logged: {len(shazam_events)}")
        max_ncc = max(ev[1] for ev in shazam_events)
        print(f"   - Peak NCC today          : {max_ncc:.4f}")
        for ts, ncc, matched, det in shazam_events[-5:]:
            status = "🚨 MATCHED RING" if matched else "Evaluating"
            print(f"     [{ts}] NCC={ncc:.4f} ({status}) | {det}")

    print("\n🤖 5. AI DETECTIONS & NOTIFICATION SUPPRESSION:")
    if not ai_events:
        print("   - No AI events recorded.")
    else:
        cat_counts = collections.Counter(ev[1] for ev in ai_events)
        silent_counts = collections.Counter(ev[3] for ev in ai_events)
        print(f"   - Total Detections: {len(ai_events)} (Categories: {dict(cat_counts)})")
        print(f"   - Notifications   : Silent/Suppressed={silent_counts[True]}, Notified={silent_counts[False]}")

    print("\n📱 6. ANDROID CLIENT STATUS DUMPS:")
    print(f"   - Total client dumps received: {len(client_dumps)}")
    for ts, d in client_dumps[-3:]:
        print(f"     [{ts}] IP={d.get('ip')} Device={d.get('device')} Screen={d.get('screen_state')}")

    print("\n📜 7. NATIVE C-SDK FILE LOGS (/mnt/opslag/deurbel/logs/hcnetsdk_native):")
    native_files = glob.glob(os.path.join(NATIVE_LOG_DIR, "*.log"))
    if not native_files:
        print("   - No native C-SDK log files found.")
    else:
        for nf in native_files:
            sz = os.path.getsize(nf)
            print(f"   - {os.path.basename(nf)}: {sz:,} bytes")
            # print last 3 lines
            try:
                with open(nf, "r", encoding="utf-8", errors="ignore") as f:
                    tail = collections.deque(f, maxlen=3)
                    for tl in tail:
                        print(f"       {tl.strip()}")
            except Exception:
                pass

    print("=" * 70)


if __name__ == "__main__":
    run_triage()

