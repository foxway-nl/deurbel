#!/usr/bin/env python3
"""
Diagnostic runner for Foxway Smart Doorbell detector calibration.
Scans bel.wav and all stored clips in /mnt/opslag/deurbel/clips.
Measures:
  - Detections count, timestamp, score, gap, p_ding (pa), p_dong (pb), sd, so
  - Max peak dominance (dom) around 726.56 Hz and 562.5 Hz
  - Near misses (why + max dB per line)
"""
import glob
import json
import os
import sys
import numpy as np

# Ensure backend app in python path
sys.path.insert(0, "/mnt/opslag/deurbel/backend")
sys.path.insert(0, "/app")

from app.doorbell_detector import (
    load_audio, SR, NFFT, HOP, FRAME_S, BIN_HZ, _WIN, LINE_HZ, _B,
    I_DING, I_DONG, DING_SEC, DONG_SEC, Params, Detector, line_prominence
)

def analyze_clip(path, params=None):
    if params is None:
        params = Params()
        
    x = load_audio(path)
    total_duration = len(x) / SR
    
    # 1. FFT and line prominence
    n = (len(x) - NFFT) // HOP + 1
    if n <= 0:
        return {"error": "Clip too short"}
        
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    H = line_prominence(x[idx])
    
    dom_ding = H[:, 7]
    dom_dong = H[:, 8]
    
    max_dom_ding = float(dom_ding.max()) if len(dom_ding) else 0.0
    max_dom_dong = float(dom_dong.max()) if len(dom_dong) else 0.0

    # 2. Run standard detector with near_miss tracking
    det = Detector(params, threshold=0.30, cooldown=8.0, verbose=False, near_miss=True)
    events = []
    near_misses = []
    
    # Custom hook for near misses
    def custom_near(why, rows):
        mx = np.round(rows[:, :len(_B)].max(axis=0)).astype(int).tolist()
        near_misses.append({"why": why, "max_db": mx, "t": float(det.t_total)})
    det._near = custom_near
    
    # Feed audio in 0.1s steps
    for i in range(0, len(x), det.step_n):
        chunk = x[i:i + det.step_n]
        ev_list = det.feed(chunk)
        for t, s in ev_list:
            ev_data = dict(det.last_event) if det.last_event else {"t": t, "score": s}
            # Calculate sd and so at event time if available
            events.append(ev_data)
            
    # Calculate detailed line prominences at event times or overall max
    pa_max = float(H[:, I_DING].max()) if len(H) else 0.0
    pb_max = float(H[:, I_DONG].max()) if len(H) else 0.0
    
    return {
        "path": path,
        "duration": round(total_duration, 2),
        "events_count": len(events),
        "events": events,
        "max_best_score": round(float(det.best), 3),
        "pa_max": round(pa_max, 1),
        "pb_max": round(pb_max, 1),
        "max_dom_ding": round(max_dom_ding, 1),
        "max_dom_dong": round(max_dom_dong, 1),
        "near_misses": near_misses,
    }

if __name__ == "__main__":
    targets = [
        ("bel.wav", "/mnt/opslag/deurbel/backend/app/assets/bel.wav"),
        ("19-11 08:45 (GOED)", "/mnt/opslag/deurbel/clips/2026-11-19/8c365cad.mp4"),
        ("19-11 10:04 (VALS)", "/mnt/opslag/deurbel/clips/2026-11-19/a231e40b.mp4"),
        ("19-11 08:15 (VALS)", "/mnt/opslag/deurbel/clips/2026-11-19/1c345ac6.mp4"),
        ("18-11 23:28 (VALS 1809fbe6)", "/mnt/opslag/deurbel/clips/2026-11-18/1809fbe6.mp4"),
        ("18-11 23:28 (VALS d9a49c90)", "/mnt/opslag/deurbel/clips/2026-11-18/d9a49c90.mp4"),
        ("18-11 23:28 (VALS a29e0f87)", "/mnt/opslag/deurbel/clips/2026-11-19/a29e0f87.mp4"),
    ]
    
    # Also find all other clips
    all_clips = sorted(glob.glob("/mnt/opslag/deurbel/clips/**/*.mp4", recursive=True))
    
    results = []
    print("================================================================================")
    print("DIAGNOSE METINGEN DEURBEL DETECTOR (STAP 2)")
    print("================================================================================")
    
    # First the key test set
    for label, path in targets:
        if os.path.exists(path):
            r = analyze_clip(path)
            r["label"] = label
            results.append(r)
            ev_str = f"{r['events_count']} detecties"
            if r["events"]:
                ev = r["events"][0]
                ev_str += f" (t={ev.get('t', 0):.1f}s, score={ev.get('score', 0):.2f}, pa={ev.get('p_ding', 0):.1f}dB, pb={ev.get('p_dong', 0):.1f}dB, gap={ev.get('gap', 0):.2f}s)"
            near_str = ", ".join([f"{nm['why']} (max {nm['max_db'][:2]})" for nm in r["near_misses"]]) if r["near_misses"] else "geen"
            print(f"{label:28s} | Detecties: {ev_str:40s} | pa_max: {r['pa_max']:4.1f} dB | pb_max: {r['pb_max']:4.1f} dB | dom_ding: {r['max_dom_ding']:4.1f} dB | dom_dong: {r['max_dom_dong']:4.1f} dB | Bijna: {near_str}")

    # Now all other clips
    target_paths = {t[1] for t in targets}
    other_clips = [c for c in all_clips if c not in target_paths]
    print("\n--------------------------------------------------------------------------------")
    print(f"OVERIGE CLIPS IN CLIP-MAP ({len(other_clips)} clips):")
    print("--------------------------------------------------------------------------------")
    other_results = []
    for c in other_clips:
        fname = os.path.basename(c)
        r = analyze_clip(c)
        r["label"] = fname
        other_results.append(r)
        near_str = ", ".join([f"{nm['why']}" for nm in r["near_misses"]]) if r["near_misses"] else "-"
        print(f"{fname:20s} | Detecties: {r['events_count']} (score: {r['max_best_score']:.2f}) | pa_max: {r['pa_max']:4.1f} | pb_max: {r['pb_max']:4.1f} | dom_ding: {r['max_dom_ding']:4.1f} | dom_dong: {r['max_dom_dong']:4.1f} | Bijna: {near_str}")

    with open("/mnt/opslag/deurbel/logs/diagnose_step2_results.json", "w") as f:
        json.dump({"key_targets": results, "other_clips": other_results}, f, indent=2)
    print("\nVolledige diagnostiek weggeschreven naar /mnt/opslag/deurbel/logs/diagnose_step2_results.json")
