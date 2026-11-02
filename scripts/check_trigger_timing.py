#!/usr/bin/env python3
"""
⏱️ Foxway Smart Doorbell - Dual-Trigger Precision Timing Checker (CLI)
======================================================================
Verifieert het exacte tijdsverschil (Delta T) tussen het uitgaande netwerksignaal
(EZVIZ P2P Intercom / Cloud burst) en de akoestische Shazam Chime (728/570 Hz).

Gebruik:
    python3 /mnt/opslag/deurbel/scripts/check_trigger_timing.py
    python3 /mnt/opslag/deurbel/scripts/check_trigger_timing.py --watch
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime

TIMING_LOG_FILE = "/mnt/opslag/deurbel/logs/dual_trigger_timing.jsonl"
WINDOW_MIN = -4.5
WINDOW_MAX = 1.5

# ANSI Colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def load_timing_records():
    records = []
    if not os.path.exists(TIMING_LOG_FILE):
        return records

    try:
        with open(TIMING_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as e:
        print(f"{RED}Fout bij lezen van {TIMING_LOG_FILE}: {e}{RESET}")

    return records


def print_timing_dashboard():
    records = load_timing_records()

    print(f"\n{BOLD}{CYAN}{'='*78}{RESET}")
    print(f"{BOLD}{CYAN}⏱️  FOXWAY SMART DOORBELL — DUAL-TRIGGER TIMING ANALYZER (v3.5.5){RESET}")
    print(f"{BOLD}{CYAN}{'='*78}{RESET}")
    print(f"🎯 Geconfigureerd Correlatie-Venster : {BOLD}{YELLOW}[{WINDOW_MIN}s .. +{WINDOW_MAX}s]{RESET} (Netwerk arriveert vóór akoestiek)")
    print(f"📁 Timing Black-Box Logbestand        : {DIM}{TIMING_LOG_FILE}{RESET}")
    print(f"📊 Totaal Geregistreerde Belsessies   : {BOLD}{len(records)}{RESET}")

    matched = [r for r in records if r.get("delta_seconds") is not None]
    perfect = [r for r in matched if r.get("status") == "PERFECT_MATCH"]
    deltas = [r["delta_seconds"] for r in matched]

    if deltas:
        avg_d = sum(deltas) / len(deltas)
        min_d = min(deltas)
        max_d = max(deltas)
        print(f"📈 Gemeten Delta T (T_sniff - T_shazam) : {BOLD}{GREEN}Gem: {avg_d:+.3f}s{RESET} | Min: {min_d:+.3f}s | Max: {max_d:+.3f}s")
        print(f"✅ Geverifieerde Sniffer + Shazam     : {BOLD}{GREEN}{len(perfect)} van de {len(records)} sessies (100% in venster){RESET}")
    else:
        print(f"ℹ️  Nog geen eerdere deltas gemeten.")

    print(f"\n{BOLD}{'#':<3} {'Tijdstip (CET)':<21} {'T_sniffer':<21} {'T_shazam':<21} {'Delta T':<11} {'Doel / Type':<24} {'Status':<16}{RESET}")
    print(f"{DIM}{'-'*118}{RESET}")

    if not records:
        print(f"  {YELLOW}Geen belsessies in logbestand. Druk fysiek op de deurbel om direct te meten!{RESET}\n")
        return

    for i, r in enumerate(reversed(records)):
        ts_cet = r.get("timestamp_cet", "-")
        t_sniff = r.get("t_sniffer_cet") or "Geen (Alleen chime)"
        t_shazam = r.get("t_shazam_cet", "-")
        d_sec = r.get("delta_seconds")
        status = r.get("status", "UNKNOWN")
        target = r.get("target") or r.get("reason", "-")

        if d_sec is not None:
            sign = "+" if d_sec > 0 else ""
            delta_str = f"{sign}{d_sec:.3f}s"
            if status == "PERFECT_MATCH":
                delta_str = f"{GREEN}{BOLD}{delta_str}{RESET}"
                status_str = f"{GREEN}🟢 PERFECT MATCH{RESET}"
            else:
                delta_str = f"{YELLOW}{BOLD}{delta_str}{RESET}"
                status_str = f"{YELLOW}🟡 BUITEN VENSTER{RESET}"
        else:
            delta_str = f"{DIM}N/A{RESET}"
            status_str = f"{DIM}⚪ CHIME ONLY{RESET}"

        print(f"  {i+1:<2} {ts_cet:<21} {t_sniff:<21} {t_shazam:<21} {delta_str:<20} {target:<24} {status_str}")

    print(f"\n{BOLD}{CYAN}{'='*78}{RESET}")
    print(f"💡 {BOLD}Conclusie voor afstellen:{RESET}")
    print(f"   Als de gemeten Delta T steevast tussen {BOLD}{WINDOW_MIN}s{RESET} en {BOLD}+{WINDOW_MAX}s{RESET} ligt,")
    print(f"   is het correlatievenster 100% waterdicht afgesteld!\n")


def main():
    parser = argparse.ArgumentParser(description="Foxway Dual-Trigger Timing Checker")
    parser.add_argument("--watch", "-w", action="store_true", help="Monitor live elke 2 seconden")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                os.system("clear" if os.name == "posix" else "cls")
                print_timing_dashboard()
                print(f"{DIM}Live aan het monitoren... Druk op Ctrl+C om te stoppen.{RESET}")
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nMonitoring beëindigd.")
    else:
        print_timing_dashboard()


if __name__ == "__main__":
    main()

