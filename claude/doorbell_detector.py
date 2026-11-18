#!/usr/bin/env python3
"""
Herkent de ding-dong van de EZVIZ DB1 Pro, ook met wind, regen, verkeer, kinderen.

Werking (korte uitleg):
  1. Audio -> spectrogram (STFT) in dB.
  2. "Tonale contrast": per frame het gemiddelde over frequentie (mediaan) aftrekken.
     Breedbandig lawaai (wind, regen, hagel, auto's) verdwijnt, smalle toonlijnen
     (ding ~727 Hz, dong ~562 Hz) blijven over.
  3. De referentie (jouw video) wordt automatisch een sjabloon van die toonlijnen in de tijd.
  4. Live: genormaliseerde kruiscorrelatie (NCC) tussen sjabloon en laatste audio.
     Score 0..1; boven drempel = aangebeld. Cooldown voorkomt dubbele meldingen.

Gebruik:
  python doorbell_detector.py --ref bel.mp4 --test opname.wav          # test bestand
  python doorbell_detector.py --ref bel.mp4 --rtsp rtsp://HOST:8554/deurbel --webhook http://HOST/hook
  python doorbell_detector.py --ref bel.mp4 --selftest                 # test met synthetisch lawaai

Vereist: python3, numpy, scipy, ffmpeg (in PATH).
"""
import argparse
import subprocess
import sys
import time
import urllib.request

import numpy as np
from scipy.ndimage import median_filter, maximum_filter1d, uniform_filter1d
from scipy.signal import stft

SR = 16000
NPERSEG = 1024          # 64 ms venster, 15.6 Hz per bin
HOP = 256               # 16 ms
FMIN, FMAX = 330, 3200  # laag (<330 Hz) negeren: wind, dreun, handling
MEDIAN_BINS = 33        # ~500 Hz breed, bepaalt wat als "achtergrond" geldt
CLIP_DB = 24.0          # contrast boven 24 dB niet extra laten wegen


# ---------------------------------------------------------------- features
def features(x):
    """audio (float, SR) -> tonaal-contrast matrix [frames, bins]"""
    f, _, Z = stft(x, SR, nperseg=NPERSEG, noverlap=NPERSEG - HOP, boundary=None, padded=False)
    P = uniform_filter1d(np.abs(Z) ** 2, 3, axis=1)      # 3 frames middelen: minder ruis-speckle
    db = 10 * np.log10(P + 1e-14)
    sel = (f >= FMIN) & (f <= FMAX)
    db = db[sel].T                                        # [frames, bins]
    bg = median_filter(db, size=(1, MEDIAN_BINS), mode="nearest")
    c = np.clip(db - bg, 0, CLIP_DB)
    return c


def load_audio(path):
    cmd = ["ffmpeg", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def build_template(path):
    x = load_audio(path)
    c = features(x)
    strength = c.sum(axis=1)
    mx = strength.max()
    idx = np.where(strength > 0.2 * mx)[0]
    start = max(idx[0] - 2, 0)
    end = min(idx[-1] + 2, len(c) - 1)
    return c[start:end + 1]


# ---------------------------------------------------------------- matching
def ncc_scores(c, T):
    """NCC van sjabloon T over feature-matrix c; geeft score per startframe."""
    n = T.shape[0]
    if len(c) < n:
        return np.array([])
    # +-1 bin tolerantie in live audio (kleine frequentieafwijking)
    cp = maximum_filter1d(c, size=3, axis=1)
    Tz = T - T.mean()
    Tn = np.linalg.norm(Tz)
    out = np.empty(len(c) - n + 1)
    for i in range(len(out)):
        w = cp[i:i + n]
        wz = w - w.mean()
        d = np.linalg.norm(wz) * Tn
        out[i] = 0.0 if d < 1e-9 else float((wz * Tz).sum() / d)
    return out


class Detector:
    def __init__(self, template, threshold=0.30, cooldown=8.0, buf_s=2.5, step_s=0.1):
        self.T = template
        self.thr = threshold
        self.cool = cooldown
        self.buf_n = int(buf_s * SR)
        self.step_n = int(step_s * SR)
        self.buf = np.zeros(0, dtype=np.float32)
        self.pending = 0
        self.last_fire = -1e9
        self.t_total = 0.0
        self.best = 0.0

    def feed(self, chunk):
        """chunk: float32 audio. Geeft lijst (tijd_s, score) van detecties."""
        events = []
        self.buf = np.concatenate([self.buf, chunk])[-self.buf_n:]
        self.pending += len(chunk)
        self.t_total += len(chunk) / SR
        while self.pending >= self.step_n:
            self.pending -= self.step_n
            if len(self.buf) < self.buf_n // 2:
                continue
            s = ncc_scores(features(self.buf), self.T)
            if s.size == 0:
                continue
            score = float(s.max())
            self.best = max(self.best, score)
            if score >= self.thr and (self.t_total - self.last_fire) > self.cool:
                self.last_fire = self.t_total
                events.append((self.t_total, score))
        return events


def scan_array(x, template, threshold=0.30, cooldown=8.0):
    d = Detector(template, threshold, cooldown)
    x = np.concatenate([np.zeros(SR * 2, np.float32), x.astype(np.float32), np.zeros(SR * 2, np.float32)])
    ev = []
    for i in range(0, len(x), d.step_n):
        ev += d.feed(x[i:i + d.step_n])
    return ev, d.best


# ---------------------------------------------------------------- live
def run_stream(url, det, webhook=None):
    cmd = ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url,
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    while True:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        print("luistert naar", url, flush=True)
        try:
            while True:
                raw = p.stdout.read(det.step_n * 4)
                if not raw:
                    break
                for t, s in det.feed(np.frombuffer(raw, dtype=np.float32)):
                    print(f"AANGEBELD score={s:.2f}", flush=True)
                    if webhook:
                        try:
                            urllib.request.urlopen(urllib.request.Request(webhook, data=b"doorbell", method="POST"), timeout=5)
                        except Exception as e:
                            print("webhook fout:", e, file=sys.stderr)
        finally:
            p.kill()
        print("stream weg, opnieuw proberen over 5s", file=sys.stderr)
        time.sleep(5)


# ---------------------------------------------------------------- selftest
def selftest(ref_path, thr):
    rng = np.random.default_rng(1)
    T = build_template(ref_path)
    ref = load_audio(ref_path)
    n = SR * 20
    t = np.arange(n) / SR

    def white(): return rng.standard_normal(n)
    def pink():
        X = np.fft.rfft(rng.standard_normal(n)); f = np.arange(len(X)); f[0] = 1
        return np.fft.irfft(X / np.sqrt(f), n)
    def wind():
        X = np.fft.rfft(rng.standard_normal(n)); f = np.fft.rfftfreq(n, 1 / SR)
        g = 1 / (1 + (f / 250) ** 3) * (1 + 0.8 * np.sin(2 * np.pi * 0.3 * t).mean())
        w = np.fft.irfft(X * g, n); return w * (1 + 0.6 * np.sin(2 * np.pi * 0.2 * t))
    def rain():
        r = np.diff(rng.standard_normal(n), prepend=0)
        r += (rng.random(n) > 0.9995) * rng.standard_normal(n) * 30
        return r
    def hail():
        h = np.zeros(n)
        for p in rng.integers(0, n - 800, 300):
            k = np.arange(800); h[p:p + 800] += np.exp(-k / 120) * np.sin(2 * np.pi * rng.uniform(1500, 4000) * k / SR) * rng.uniform(1, 5)
        return h + 0.2 * white()
    def car():
        X = np.fft.rfft(rng.standard_normal(n)); f = np.fft.rfftfreq(n, 1 / SR)
        g = 1 / (1 + (f / 400) ** 2)
        e = np.exp(-((t - 10) / 2.5) ** 2)
        return np.fft.irfft(X * g, n) * e * 2 + 0.3 * np.sin(2 * np.pi * 90 * t) * e
    def kids():
        y = np.zeros(n)
        for s in rng.uniform(0, 18, 12):
            L = int(rng.uniform(0.5, 1.5) * SR); k = np.arange(L) / SR
            f0 = rng.uniform(500, 900) + rng.uniform(-300, 300) * k
            ph = 2 * np.pi * np.cumsum(f0) / SR
            v = sum(np.sin(h * ph) / h for h in range(1, 8)) * np.hanning(L)
            y[int(s * SR):int(s * SR) + L] += v[:max(0, min(L, n - int(s * SR)))] if False else v[:n - int(s * SR)]
        return y

    bgs = dict(wit=white, roze=pink, wind=wind, regen=rain, hagel=hail, auto=car, kinderen=kids)
    ok = True
    for name, fn in bgs.items():
        bg = fn(); bg = bg / np.std(bg)
        # 1) alleen achtergrond: mag NIET afgaan
        ev, best = scan_array(bg.astype(np.float32) * 0.1, T, thr)
        fp = len(ev)
        # 2) bel erin op 5 en 14 s, op verschillende signaal/ruis-niveaus
        res = []
        for snr_db in (10, 0, -6):
            sig = np.zeros(n); sig[5 * SR:5 * SR + len(ref)] += ref; sig[14 * SR:14 * SR + len(ref)] += ref
            # ruis relatief aan RMS van de bel zelf
            rms_ref = np.sqrt(np.mean(ref ** 2))
            mix = sig + bg * rms_ref / (10 ** (snr_db / 20))
            ev, _ = scan_array(mix.astype(np.float32), T, thr)
            res.append((snr_db, len(ev)))
        good = all(c == 2 for _, c in res) if name not in () else True
        print(f"{name:9s} vals alarm: {fp} (max score {best:.2f}) | bel (snr dB -> detecties): {res}")
    print("klaar. verwacht: vals alarm 0, detecties 2 per bel-test (hoge ruis kan missen)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, help="video/audio met de ding-dong")
    ap.add_argument("--test", help="audio/video bestand om te scannen")
    ap.add_argument("--rtsp", help="rtsp url (bijv. go2rtc) om live te luisteren")
    ap.add_argument("--webhook", help="URL die een POST krijgt bij aanbellen")
    ap.add_argument("--threshold", type=float, default=0.30)
    ap.add_argument("--cooldown", type=float, default=8.0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest(a.ref, a.threshold)
    T = build_template(a.ref)
    print(f"sjabloon: {T.shape[0] * HOP / SR:.2f}s")
    if a.test:
        x = load_audio(a.test)
        ev, best = scan_array(x, T, a.threshold, a.cooldown)
        print(f"beste score {best:.2f}")
        for t, s in ev:
            print(f"  bel bij ~{t:.1f}s (score {s:.2f})")
        if not ev:
            print("  geen bel gevonden")
    if a.rtsp:
        run_stream(a.rtsp, Detector(T, a.threshold, a.cooldown), a.webhook)


if __name__ == "__main__":
    main()
