#!/usr/bin/env python3
"""
EZVIZ DB1 Pro ding-dong detector, versie 2 (regel-gebaseerd, veel strenger dan v1).

Waarom v2?  v1 vergeleek het hele spectrum met een sjabloon. Stemmen en kindergegil kunnen
dan toevallig genoeg lijken. v2 kijkt alleen naar wat de bel ECHT uniek maakt: een aantal
zuivere, exact vaste toonlijnen in een vaste volgorde:

  DING  (~0,3 s):  726,6 Hz  + 398,4 / 1968,8 / 2500 Hz        (562,5 Hz is dan afwezig)
  DONG  (~0,7 s):  562,5 Hz  + nagalm 726,6 Hz + 1687,5 / 1523,4 Hz
  Tussen begin ding en begin dong: 0,15 - 0,6 s (app-sample 0,22 s, echte opnames 0,33-0,38 s).

Een melding komt pas als ALLES klopt: ding-lijn lang genoeg (maar niet eindeloos), daarna dong-lijn
lang genoeg, de nevenlijnen er bij, en de tijdsafstand juist. Stem, gegil, wind, regen, hagel,
verkeer, sirenes of een constante piep halen dat niet. Het is een zuivere digitale sample, dus de
lijnen liggen op de exacte frequentie (+-1 bin = +-8 Hz tolerantie).

Compatibel met v1: build_template(), Detector(template, threshold, cooldown), det.feed(chunk) ->
[(tijd_s, score)], det.step_n, det.best, scan_array(), load_audio() en run_stream().

Gebruik:
  python doorbell_detector.py --test opname.mp4                   # bestand scannen
  python doorbell_detector.py --rtsp rtsp://HOST:8554/deurbel --webhook http://HOST/hook --clip-dir clips
  python doorbell_detector.py --selftest

Vereist: python3, numpy, ffmpeg (in PATH). Geen scipy nodig.
"""
import argparse
import subprocess
import sys
import time
import urllib.request
import wave
from dataclasses import dataclass

import numpy as np

SR = 16000
NFFT = 2048                 # 128 ms venster, 7,8 Hz per bin
HOP = 512                   # 32 ms per frame
FRAME_S = HOP / SR
BIN_HZ = SR / NFFT

# index: 0 ding-hoofd, 1 dong-hoofd, 2-4 ding-nevenlijnen, 5-6 dong-nevenlijnen
LINE_HZ = [726.56, 562.5, 398.4, 1968.8, 2500.0, 1687.5, 1523.4]
I_DING, I_DONG = 0, 1
DING_SEC = [2, 3, 4]
DONG_SEC = [5, 6]
I_DOM_DING, I_DOM_DONG = 7, 8


@dataclass
class Params:
    ta: float = 22.0        # ding-lijn minimaal zoveel dB boven de omgeving
    tb_absent: float = 10.0  # tijdens ding mag 562,5 Hz hooguit zoveel dB boven de omgeving zijn
    tb: float = 15.0        # dong-lijn minimaal
    tb_ring: float = 12.0   # nagalm 726,6 Hz tijdens dong minimaal
    ts: float = 10.0        # nevenlijn telt als aanwezig vanaf zoveel dB
    sec_frames: int = 3     # nevenlijn moet zoveel frames aanwezig zijn
    min_sec_ding: int = 1   # aantal nevenlijnen (van 3) in het ding
    min_sec_dong: int = 1   # aantal nevenlijnen (van 2) in de dong
    n_a_min: int = 5        # ding minstens 5 frames (0,16 s)
    n_a_max: int = 18       # ding hoogstens 18 frames (0,58 s): langer = constante toon
    n_b: int = 8            # dong minstens 8 frames (0,26 s) -> dan meteen melden
    hole: int = 3           # maximaal zoveel frames tussen ding en dong
    gap_min: float = 0.15   # s tussen begin ding en begin dong
    gap_max: float = 0.60
    min_dom_db: float = 9.0  # piek-dominantie (t.o.v. directe buren b+-2) minimaal zoveel dB


def build_template(path=None, verbose=True):
    """Compatibel met v1. De lijnen zijn vast (uit jouw opnames bepaald). Met een referentiebestand
    controleren we alleen of de bel daarin herkend wordt."""
    p = Params()
    if path:
        try:
            ev, _ = scan_array(load_audio(path), p, 0.0, 8.0)
            if verbose:
                print("referentie " + ("OK: bel herkend" if ev else
                      "WAARSCHUWING: bel niet herkend in referentie (dong te kort afgeknipt of ander geluid?)"), flush=True)
        except Exception as e:
            print("referentiecontrole overgeslagen:", e, file=sys.stderr)
    return p


def load_audio(path):
    cmd = ["ffmpeg", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


# ---------------------------------------------------------------- spectrum -> lijnprominentie
_WIN = np.hanning(NFFT).astype(np.float32)
_B = [int(round(f / BIN_HZ)) for f in LINE_HZ]


def line_prominence(frames):
    """frames [n, NFFT] -> [n, 9]:
    0..6: dB van elke lijn boven de mediaan van de omgeving (+-300 Hz).
    7: piek-dominantie ding (726,56 Hz t.o.v. directe buren b+-2).
    8: piek-dominantie dong (562,5 Hz t.o.v. directe buren b+-2).
    """
    db = 10 * np.log10(np.abs(np.fft.rfft(frames * _WIN, axis=1)) ** 2 + 1e-12)
    out = np.empty((len(frames), len(_B) + 2), np.float32)
    for k, b in enumerate(_B):
        pk = db[:, b - 1:b + 2].max(axis=1)
        bg = np.median(np.concatenate([db[:, b - 48:b - 5], db[:, b + 6:b + 49]], axis=1), axis=1)
        out[:, k] = pk - bg

    b_ding, b_dong = _B[I_DING], _B[I_DONG]
    pk_ding = db[:, b_ding - 1:b_ding + 2].max(axis=1)
    nbr_ding = np.maximum(db[:, b_ding - 2], db[:, b_ding + 2])
    out[:, I_DOM_DING] = pk_ding - nbr_ding

    pk_dong = db[:, b_dong - 1:b_dong + 2].max(axis=1)
    nbr_dong = np.maximum(db[:, b_dong - 2], db[:, b_dong + 2])
    out[:, I_DOM_DONG] = pk_dong - nbr_dong

    return out


def _fill1(m):
    m = m.copy()
    m[1:-1] |= m[:-2] & m[2:]            # gaten van 1 frame dichten
    return m


def _runs(m):
    if not m.any():
        return []
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    s, e = np.where(d == 1)[0], np.where(d == -1)[0] - 1
    return list(zip(s.tolist(), e.tolist()))


# ---------------------------------------------------------------- detector
class Detector:
    def __init__(self, template=None, threshold=0.30, cooldown=8.0, clip_dir=None, verbose=True,
                 clip_s=8.0, near_miss=True, **_ignored):
        self.p = template if isinstance(template, Params) else Params()
        self.thr = threshold
        self.cool = cooldown
        self.clip_dir = clip_dir
        self.verbose = verbose
        self.near_miss = near_miss
        self.step_n = int(0.1 * SR)             # v1-compatibel: lees in blokken van 0,1 s
        self.buf = np.zeros(0, np.float32)      # nog niet verwerkte samples
        self.ring = np.zeros(0, np.float32)     # laatste clip_s seconden (voor opslaan)
        self.ring_n = int(clip_s * SR)
        self.hist = np.zeros((0, len(_B) + 2), np.float32)
        self.hist_max = 100
        self.n_frames = 0                       # totaal verwerkte frames
        self.t_total = 0.0
        self.last_fire = -1e9
        self.last_near = -1e9
        self.reported_a = set()
        self.best = 0.0
        self.last_event = None

    # -- invoer
    def feed(self, chunk):
        chunk = np.asarray(chunk, np.float32)
        events = []
        self.buf = np.concatenate([self.buf, chunk])
        self.ring = np.concatenate([self.ring, chunk])[-self.ring_n:]
        self.t_total += len(chunk) / SR
        if len(self.buf) >= NFFT:
            n = (len(self.buf) - NFFT) // HOP + 1
            idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
            rows = line_prominence(self.buf[idx])
            self.buf = self.buf[n * HOP:]
            for r in rows:
                self.n_frames += 1
                self.hist = np.vstack([self.hist, r[None, :]])[-self.hist_max:]
                ev = self._evaluate()
                if ev:
                    events.append(ev)
        return events

    # -- regels
    def _evaluate(self):
        p, H = self.p, self.hist
        n = len(H)
        if n < p.n_a_min + p.n_b:
            return None
        pa, pb = H[:, I_DING], H[:, I_DONG]
        dom_a, dom_b = H[:, I_DOM_DING], H[:, I_DOM_DONG]
        A = _fill1((pa >= p.ta) & (pb < p.tb_absent) & (dom_a >= p.min_dom_db))
        B = _fill1((pb >= p.tb) & (pa >= p.tb_ring) & (dom_b >= p.min_dom_db))
        t_now = self.t_total

        a_runs = _runs(A)
        b_runs = _runs(B)
        # voortgangsscore voor logging (best): ding gezien = 0.4, ding+dong = tot 1.0
        if a_runs:
            a0, a1 = a_runs[-1]
            qa = min(1.0, float(pa[a0:a1 + 1].mean()) / 40) * min(1.0, (a1 - a0 + 1) / p.n_a_min)
            self.best = max(self.best, 0.4 * qa)

        # near-miss: ding zonder dong
        if self.near_miss:
            for a0, a1 in a_runs:
                abs_end = self.n_frames - (n - 1 - a1)
                if (a1 - a0 + 1) >= p.n_a_min and (n - 1 - a1) > 16 and abs_end not in self.reported_a:
                    self.reported_a.add(abs_end)
                    followed = any(b0 > a1 and (b1 - b0 + 1) >= 3 for b0, b1 in b_runs)
                    if not followed and t_now - self.last_near > 20:
                        self.last_near = t_now
                        self._near("ding gezien, geen dong", H[a0:a1 + 1])
            if len(self.reported_a) > 200:
                self.reported_a = set(sorted(self.reported_a)[-50:])

        if not b_runs or b_runs[-1][1] != n - 1:
            return None
        b0, b1 = b_runs[-1]
        lb = b1 - b0 + 1
        if lb != p.n_b:                           # precies 1x evalueren: op het moment dat de dong lang genoeg is
            return None
        cand = [r for r in a_runs if r[1] < b0 and (b0 - r[1] - 1) <= p.hole]
        why = None
        if not cand:
            why = "dong zonder ding ervoor"
        else:
            a0, a1 = cand[-1]
            la = a1 - a0 + 1
            gap = (b0 - a0) * FRAME_S
            if la < p.n_a_min or la > p.n_a_max:
                why = f"ding-lengte {la * FRAME_S:.2f}s"
            elif not (p.gap_min <= gap <= p.gap_max):
                why = f"afstand ding-dong {gap:.2f}s"
            else:
                sd = sum(int((H[max(0, a0 - 2):a1 + 4, k] >= p.ts).sum() >= p.sec_frames) for k in DING_SEC)
                so = sum(int((H[max(0, b0 - 2):b1 + 1, k] >= p.ts).sum() >= p.sec_frames) for k in DONG_SEC)
                if sd < p.min_sec_ding:
                    why = f"nevenlijnen ding {sd}/{len(DING_SEC)}"
                elif so < p.min_sec_dong:
                    why = f"nevenlijnen dong {so}/{len(DONG_SEC)}"
        if why:
            if self.near_miss and t_now - self.last_near > 20:
                self.last_near = t_now
                self._near(why, H[max(b0 - 20, 0):b1 + 1])
            return None

        score = float(min(1.0, min(pa[a0:a1 + 1].mean() / 40, pb[b0:b1 + 1].mean() / 30)))
        self.best = max(self.best, score)
        if score < self.thr or (t_now - self.last_fire) <= self.cool:
            return None
        self.last_fire = t_now
        ding_t = t_now - (n - 1 - a0) * FRAME_S - NFFT / SR / 2
        dong_t = t_now - (n - 1 - b0) * FRAME_S - NFFT / SR / 2
        self.last_event = dict(t=t_now, ding_t=ding_t, dong_t=dong_t, score=score,
                               p_ding=float(pa[a0:a1 + 1].mean()), p_dong=float(pb[b0:b1 + 1].mean()),
                               gap=(b0 - a0) * FRAME_S, delay_after_ding=t_now - ding_t)
        if self.verbose:
            e = self.last_event
            print(f"[bel] DETECTIE audio-tijd {t_now:.1f}s  ding {e['ding_t']:.2f}s dong {e['dong_t']:.2f}s "
                  f"(vertraging detector na begin ding {e['delay_after_ding']:.2f}s)  score {score:.2f}  "
                  f"P ding/dong {e['p_ding']:.0f}/{e['p_dong']:.0f} dB  gap {e['gap']:.2f}s", flush=True)
        self._save_clip(f"bel_{score:.2f}")
        return (t_now, score)

    # -- diagnostiek
    def _near(self, why, rows):
        if self.verbose:
            mx = np.round(rows[:, :len(_B)].max(axis=0)).astype(int).tolist()
            print(f"[bel] bijna-melding: {why}  (max dB per lijn {mx})", flush=True)
        self._save_clip("bijna")

    def _save_clip(self, tag):
        if not self.clip_dir:
            return
        import os
        os.makedirs(self.clip_dir, exist_ok=True)
        fn = os.path.join(self.clip_dir, time.strftime("%Y%m%d_%H%M%S_") + tag + ".wav")
        with wave.open(fn, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(self.ring, -1, 1) * 32767).astype(np.int16).tobytes())
        if self.verbose:
            print("[bel] clip opgeslagen:", fn, flush=True)


def scan_array(x, template=None, threshold=0.30, cooldown=8.0, verbose=False):
    d = Detector(template, threshold, cooldown, verbose=verbose, near_miss=False)
    x = np.concatenate([np.zeros(SR, np.float32), np.asarray(x, np.float32), np.zeros(SR, np.float32)])
    ev = []
    for i in range(0, len(x), d.step_n):
        ev += [(t - 1.0, s) for t, s in d.feed(x[i:i + d.step_n])]
    return ev, d.best


# ---------------------------------------------------------------- live
def run_stream(url, det, webhook=None):
    cmd = ["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
           "-probesize", "32768", "-analyzeduration", "0", "-rtsp_transport", "tcp", "-i", url,
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    while True:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        print("luistert naar", url, flush=True)
        t0, got, last_stat = time.time(), 0, time.time()
        try:
            while True:
                raw = p.stdout.read(det.step_n * 4)
                if not raw:
                    break
                got += len(raw) // 4
                for t, s in det.feed(np.frombuffer(raw, dtype=np.float32)):
                    print(f"AANGEBELD {time.strftime('%H:%M:%S')} score={s:.2f}", flush=True)
                    if webhook:
                        try:
                            urllib.request.urlopen(urllib.request.Request(webhook, data=b"doorbell", method="POST"), timeout=5)
                        except Exception as e:
                            print("webhook fout:", e, file=sys.stderr)
                if time.time() - last_stat > 60:
                    lag = (time.time() - t0) - got / SR
                    print(f"[stats] luistert nog; achterstand verwerking {lag:+.1f}s (moet ~constant blijven); "
                          f"hoogste deelscore {det.best:.2f}", flush=True)
                    det.best, last_stat = 0.0, time.time()
        finally:
            p.kill()
        print("stream weg, opnieuw proberen over 5s", file=sys.stderr)
        time.sleep(5)


# ---------------------------------------------------------------- selftest
def _noise(kind, n, rng):
    t = np.arange(n) / SR
    if kind == "wit":
        return rng.standard_normal(n)
    if kind == "wind":
        X = np.fft.rfft(rng.standard_normal(n)); f = np.fft.rfftfreq(n, 1 / SR)
        return np.fft.irfft(X / (1 + (f / 250) ** 3), n) * (1 + 0.6 * np.sin(2 * np.pi * 0.2 * t))
    if kind == "regen":
        r = np.diff(rng.standard_normal(n), prepend=0)
        return r + (rng.random(n) > 0.9995) * rng.standard_normal(n) * 30
    if kind == "hagel":
        h = np.zeros(n)
        for p in rng.integers(0, n - 800, 300):
            k = np.arange(800); h[p:p + 800] += np.exp(-k / 120) * np.sin(2 * np.pi * rng.uniform(1500, 4000) * k / SR) * rng.uniform(1, 5)
        return h + 0.2 * rng.standard_normal(n)
    if kind == "auto":
        X = np.fft.rfft(rng.standard_normal(n)); f = np.fft.rfftfreq(n, 1 / SR)
        e = np.exp(-((t - n / SR / 2) / 2.5) ** 2)
        return np.fft.irfft(X / (1 + (f / 400) ** 2), n) * e * 2 + 0.3 * np.sin(2 * np.pi * 90 * t) * e
    if kind == "gegil":                       # harmonisch, glijdend, hard (kind), formant-achtig
        y = np.zeros(n)
        for s in rng.uniform(0, n / SR - 2, 14):
            L = int(rng.uniform(0.6, 1.6) * SR); k = np.arange(L) / SR
            f0 = rng.uniform(450, 1000) + rng.uniform(-400, 400) * k + 25 * np.sin(2 * np.pi * 6 * k)
            ph = 2 * np.pi * np.cumsum(f0) / SR
            v = sum(np.sin(h * ph) / h ** 0.7 for h in range(1, 10)) * np.hanning(L)
            i0 = int(s * SR); y[i0:i0 + L] += v[:n - i0]
        return y
    raise ValueError(kind)


def selftest(ref_path):
    rng = np.random.default_rng(7)
    ref = load_audio(ref_path)
    n = SR * 30
    rms_ref = float(np.sqrt(np.mean(ref ** 2)))
    ok_all = True
    for kind in ("wit", "wind", "regen", "hagel", "auto", "gegil"):
        bg = _noise(kind, n, rng); bg = bg / np.std(bg)
        fp = []
        for lvl in (0.05, 0.3, 1.0):                    # achtergrond alleen, op 3 volumes
            ev, _ = scan_array((bg * lvl).astype(np.float32))
            fp.append(len(ev))
        res = []
        for rel_db in (10, 0, -6):                      # bel op 3 niveaus t.o.v. achtergrond
            sig = np.zeros(n); sig[8 * SR:8 * SR + len(ref)] += ref; sig[20 * SR:20 * SR + len(ref)] += ref
            mix = sig + bg * rms_ref / (10 ** (rel_db / 20))
            ev, _ = scan_array(mix.astype(np.float32))
            res.append((rel_db, len(ev)))
        print(f"{kind:7s} valse alarmen (3 volumes): {fp} | bel 2x erin (bel {res[0][0]}/{res[1][0]}/{res[2][0]} dB boven ruis): {[c for _, c in res]}")
    print("verwacht: valse alarmen 0; bel-detecties 2 (alleen bij zeer harde ruis kan er een gemist worden)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", help="(optioneel) referentie-opname; wordt alleen gecontroleerd")
    ap.add_argument("--test", help="audio/video bestand om te scannen")
    ap.add_argument("--rtsp", help="rtsp url (bijv. go2rtc) om live te luisteren")
    ap.add_argument("--webhook", help="URL die een POST krijgt bij aanbellen")
    ap.add_argument("--threshold", type=float, default=0.30)
    ap.add_argument("--cooldown", type=float, default=8.0)
    ap.add_argument("--min-dom", type=float, default=None, help="minimale piek-dominantie in dB")
    ap.add_argument("--clip-dir", help="sla bij elke melding/bijna-melding 8 s audio op (voor afstellen)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest(a.ref or a.test)
    T = build_template(a.ref)
    if a.min_dom is not None:
        T.min_dom_db = a.min_dom
    if a.test:
        x = np.concatenate([np.zeros(SR, np.float32), load_audio(a.test), np.zeros(SR, np.float32)])
        d = Detector(T, a.threshold, a.cooldown, clip_dir=a.clip_dir, near_miss=True)
        ev = []
        for i in range(0, len(x), d.step_n):
            ev += d.feed(x[i:i + d.step_n])
        print(f"gevonden: {len(ev)}  hoogste deelscore {d.best:.2f}")
        for t, s in ev:
            print(f"  bel bij {t:.1f}s (score {s:.2f})")
    if a.rtsp:
        run_stream(a.rtsp, Detector(T, a.threshold, a.cooldown, clip_dir=a.clip_dir), a.webhook)


if __name__ == "__main__":
    main()