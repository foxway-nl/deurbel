#!/usr/bin/env python3
"""
Zuinige deurdetectie: mensen > dieren > bewegende voertuigen.

Twee trappen, zodat de CPU bijna altijd rust:
  1. Bewegingspoort (goedkoop, 320x180, MOG2): alleen als er in de kijkzone een echte
     beweging is (blob groot genoeg, regen/ruis wordt weggefilterd) gaat trap 2 aan.
  2. YOLO (ONNX, imgsz 320, 2 threads, max ~3 fps) + tracker:
       - persoon:  >=2 treffers
       - dier:     >=3 treffers (kat/hond; vogels uit tenzij --birds)
       - voertuig: >=3 treffers EN daadwerkelijk verplaatst (geparkeerde auto's tellen niet)
     Takken, regen, schaduwen, lampen zijn geen YOLO-klassen en geven dus nooit een melding.

Gebruik:
  python door_watch.py --rtsp rtsp://HOST:8554/deur_sub --save-frame frame.jpg     # raster-foto voor zones
  python door_watch.py --rtsp rtsp://HOST:8554/deur_sub --model yolo11n.onnx --zones zones.json --webhook http://HOST/hook

Model exporteren (eenmalig, op de Mac): yolo export model=yolo11n.pt format=onnx imgsz=320 simplify=True
Vereist: python3, numpy, opencv-python(-headless), onnxruntime, ffmpeg.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import cv2
import numpy as np

# COCO-klassen -> (label, categorie)
CLASS_MAP = {0: ("person", "person"), 1: ("bicycle", "vehicle"), 2: ("car", "vehicle"),
             3: ("motorcycle", "vehicle"), 5: ("bus", "vehicle"), 7: ("truck", "vehicle"),
             15: ("cat", "animal"), 16: ("dog", "animal")}
BIRD_MAP = {14: ("bird", "animal")}
PRIORITY = {"person": 0, "animal": 1, "vehicle": 2}
CONF = {"person": 0.45, "animal": 0.40, "vehicle": 0.45}
MIN_HITS = {"person": 2, "animal": 3, "vehicle": 3}
MATCH_DIST = {"person": 0.12, "animal": 0.15, "vehicle": 0.30}   # fractie van framebreedte
VEHICLE_MOVE = 0.06                                              # min. verplaatsing (framebreedte)
COOLDOWN = {"person": 30.0, "animal": 60.0, "vehicle": 60.0}


# ------------------------------------------------------------ zones
def poly_mask(shape, polys):
    h, w = shape
    m = np.zeros((h, w), np.uint8)
    for p in polys:
        pts = (np.array(p, np.float32) * [w, h]).astype(np.int32)
        cv2.fillPoly(m, [pts], 255)
    return m


def load_zones(path, shape):
    full = np.full(shape, 255, np.uint8)
    if not path:
        return full
    z = json.load(open(path))
    m = poly_mask(shape, z["watch"]) if z.get("watch") else full
    if z.get("ignore"):
        m[poly_mask(shape, z["ignore"]) > 0] = 0
    return m


# ------------------------------------------------------------ trap 1: beweging
class MotionGate:
    def __init__(self, mask_small, min_blob_frac=0.004, global_frac=0.45):
        self.mask = mask_small
        self.area = max(int((mask_small > 0).sum()), 1)
        self.min_blob = max(int(min_blob_frac * mask_small.size), 6)
        self.global_frac = global_frac
        self.bg = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=36, detectShadows=False)
        self.k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.streak = 0

    def update(self, small_bgr):
        g = cv2.GaussianBlur(cv2.cvtColor(small_bgr, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        fg = self.bg.apply(g)
        fg = cv2.bitwise_and(fg, self.mask)
        if (fg > 0).sum() > self.global_frac * self.area:     # lichtwissel / IR-schakeling
            self.streak = 0
            return False
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.k)     # regenspikkels weg
        fg = cv2.dilate(fg, self.k, iterations=2)
        n, _, st, _ = cv2.connectedComponentsWithStats((fg > 0).astype(np.uint8), 8)
        moving = n > 1 and st[1:, cv2.CC_STAT_AREA].max() >= self.min_blob
        self.streak = self.streak + 1 if moving else 0
        return self.streak >= 2                               # 2 frames na elkaar


# ------------------------------------------------------------ trap 2: YOLO
def decode(out, ids, conf_thr, iou_thr, r, dw, dh):
    """YOLOv8/YOLO11 ONNX-uitvoer [1, 4+nc, N] -> lijst (cls_index, conf, (x1,y1,x2,y2)) in framecoordinaten."""
    p = out[0]
    if p.shape[0] < p.shape[1]:
        p = p.T
    sc = p[:, 4:][:, ids]
    loc = sc.argmax(1)
    conf = sc[np.arange(len(sc)), loc]
    keep = conf >= conf_thr
    p, conf, loc = p[keep], conf[keep], loc[keep]
    if len(p) == 0:
        return []
    cx, cy, w, h = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
    x1, y1 = (cx - w / 2 - dw) / r, (cy - h / 2 - dh) / r
    x2, y2 = (cx + w / 2 - dw) / r, (cy + h / 2 - dh) / r
    off = loc * 4096.0                                        # NMS per klasse
    nb = np.stack([x1 + off, y1, x2 - x1, y2 - y1], 1).tolist()
    idx = np.array(cv2.dnn.NMSBoxes(nb, conf.tolist(), conf_thr, iou_thr)).reshape(-1)
    return [(int(ids[loc[i]]), float(conf[i]), (float(x1[i]), float(y1[i]), float(x2[i]), float(y2[i]))) for i in idx]


class YoloOnnx:
    def __init__(self, path, imgsz=320, threads=2, birds=False, conf=0.30, iou=0.5):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.s = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        inp = self.s.get_inputs()[0]
        self.name = inp.name
        shp = inp.shape
        self.size = int(shp[2]) if isinstance(shp[2], int) else imgsz
        self.map = dict(CLASS_MAP)
        if birds:
            self.map.update(BIRD_MAP)
        self.ids = np.array(sorted(self.map))
        self.conf, self.iou = conf, iou

    def detect(self, bgr):
        h, w = bgr.shape[:2]
        s = self.size
        r = min(s / h, s / w)
        nw, nh = int(round(w * r)), int(round(h * r))
        dw, dh = (s - nw) / 2, (s - nh) / 2
        img = np.full((s, s, 3), 114, np.uint8)
        img[int(dh):int(dh) + nh, int(dw):int(dw) + nw] = cv2.resize(bgr, (nw, nh))
        blob = cv2.dnn.blobFromImage(img, 1 / 255.0, (s, s), swapRB=True)
        out = self.s.run(None, {self.name: blob})[0]
        res = []
        for cid, c, (x1, y1, x2, y2) in decode(out, self.ids, self.conf, self.iou, r, int(dw), int(dh)):
            lab, cat = self.map[cid]
            res.append(dict(label=lab, cat=cat, conf=c, box=(max(x1, 0), max(y1, 0), min(x2, w), min(y2, h))))
        return res


# ------------------------------------------------------------ tracker
class Track:
    def __init__(self, tid, d, t):
        self.id, self.cat, self.label = tid, d["cat"], d["label"]
        self.first = self.last = center(d["box"])
        self.max_disp = 0.0
        self.hits, self.miss, self.alerted = 1, 0, False
        self.conf = d["conf"]
        self.box = d["box"]
        self.t = t


def center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


class Tracker:
    def __init__(self, width, max_miss=5):
        self.W, self.max_miss, self.tracks, self.n = width, max_miss, [], 0

    def clear(self):
        self.tracks = []

    def update(self, dets, t):
        used = set()
        for tr in self.tracks:
            best, bd = None, MATCH_DIST[tr.cat] * self.W
            for i, d in enumerate(dets):
                if i in used or d["cat"] != tr.cat:
                    continue
                c = center(d["box"])
                dist = np.hypot(c[0] - tr.last[0], c[1] - tr.last[1])
                if dist < bd:
                    best, bd = i, dist
            if best is None:
                tr.miss += 1
                continue
            used.add(best)
            d = dets[best]
            tr.last = center(d["box"])
            tr.max_disp = max(tr.max_disp, np.hypot(tr.last[0] - tr.first[0], tr.last[1] - tr.first[1]))
            tr.hits += 1
            tr.miss = 0
            tr.conf, tr.box, tr.label, tr.t = d["conf"], d["box"], d["label"], t
        for i, d in enumerate(dets):
            if i not in used:
                self.n += 1
                self.tracks.append(Track(self.n, d, t))
        self.tracks = [tr for tr in self.tracks if tr.miss <= self.max_miss]

    def confirmed(self):
        out = []
        for tr in self.tracks:
            if tr.alerted or tr.miss > 0 or tr.hits < MIN_HITS[tr.cat]:
                continue
            if tr.cat == "vehicle" and tr.max_disp < VEHICLE_MOVE * self.W:
                continue                                       # geparkeerd
            out.append(tr)
        return sorted(out, key=lambda x: PRIORITY[x.cat])


# ------------------------------------------------------------ meldingen
def send_alert(tr, frame, args, t):
    snap = None
    if args.snap_dir:
        os.makedirs(args.snap_dir, exist_ok=True)
        img = frame.copy()
        x1, y1, x2, y2 = map(int, tr.box)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(img, f"{tr.label} {tr.conf:.2f}", (x1, max(y1 - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        snap = os.path.join(args.snap_dir, time.strftime("%Y%m%d_%H%M%S") + f"_{tr.cat}.jpg")
        cv2.imwrite(snap, img)
    msg = dict(type=tr.cat, label=tr.label, conf=round(tr.conf, 2), snapshot=snap, t=round(t, 1))
    print("MELDING " + json.dumps(msg), flush=True)
    if args.webhook:
        def post():
            try:
                req = urllib.request.Request(args.webhook, data=json.dumps(msg).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print("webhook fout:", e, file=sys.stderr)
        threading.Thread(target=post, daemon=True).start()
    return msg


# ------------------------------------------------------------ hoofdlus
def run(frames, detector, args, W, H, fps, zone_full=None, alerts_out=None):
    SW, SH = 320, int(round(320 * H / W))
    zone_small = cv2.resize(zone_full, (SW, SH), interpolation=cv2.INTER_NEAREST) if zone_full is not None \
        else np.full((SH, SW), 255, np.uint8)
    gate = MotionGate(zone_small, args.min_blob)
    tracker = Tracker(W)
    last_alert = {}
    last_motion, awake_since = -1e9, None
    n = yolo_n = wake_n = 0
    yolo_ms = 0.0
    t_stat = 0.0
    for idx, frame in enumerate(frames):
        t = idx / fps
        n += 1
        small = cv2.resize(frame, (SW, SH), interpolation=cv2.INTER_AREA)
        if gate.update(small):
            last_motion = t
        awake = (t - last_motion) <= args.hold
        if not awake:
            tracker.clear()
        else:
            wake_n += 1
            t0 = time.time()
            dets = []
            for d in detector.detect(frame):
                if d["conf"] < CONF[d["cat"]]:
                    continue
                fx, fy = (d["box"][0] + d["box"][2]) / 2, d["box"][3]          # voetpunt
                if zone_full is not None and zone_full[min(int(fy), H - 1), min(int(fx), W - 1)] == 0:
                    continue
                dets.append(d)
            yolo_ms += (time.time() - t0) * 1000
            yolo_n += 1
            tracker.update(dets, t)
            for tr in tracker.confirmed():
                tr.alerted = True
                if t - last_alert.get(tr.cat, -1e9) < COOLDOWN[tr.cat]:
                    continue
                last_alert[tr.cat] = t
                m = send_alert(tr, frame, args, t)
                if alerts_out is not None:
                    alerts_out.append(m)
        if t - t_stat >= 60:
            t_stat = t
            print(f"[stats] frames={n} wakker={wake_n} ({100 * wake_n / max(n, 1):.1f}%) "
                  f"yolo_gem={yolo_ms / max(yolo_n, 1):.0f}ms", flush=True)
            n = wake_n = yolo_n = 0
            yolo_ms = 0.0


def ffmpeg_frames(src, W, H, fps):
    cmd = ["ffmpeg", "-loglevel", "error"]
    if src.startswith("rtsp"):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-i", src, "-an", "-vf", f"fps={fps},scale={W}:{H}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    size = W * H * 3
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        while True:
            raw = p.stdout.read(size)
            if len(raw) < size:
                return
            yield np.frombuffer(raw, np.uint8).reshape(H, W, 3)
    finally:
        p.kill()


def save_frame(src, W, H, out):
    for f in ffmpeg_frames(src, W, H, 1):
        img = f.copy()
        for i in range(1, 10):
            x, y = int(W * i / 10), int(H * i / 10)
            cv2.line(img, (x, 0), (x, H), (0, 255, 255), 1)
            cv2.line(img, (0, y), (W, y), (0, 255, 255), 1)
            cv2.putText(img, f"{i / 10:.1f}", (x + 2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
            cv2.putText(img, f"{i / 10:.1f}", (2, y - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        cv2.imwrite(out, img)
        print("opgeslagen:", out, "(raster = genormaliseerde coordinaten 0..1 voor zones.json)")
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtsp", required=True, help="RTSP-url (liefst substream) of videobestand")
    ap.add_argument("--model", help="YOLO ONNX-bestand (yolov8n/yolo11n, imgsz 320)")
    ap.add_argument("--zones", help="zones.json met watch/ignore polygonen (0..1)")
    ap.add_argument("--webhook")
    ap.add_argument("--snap-dir", default="snapshots")
    ap.add_argument("--fps", type=float, default=3)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=360)
    ap.add_argument("--imgsz", type=int, default=320)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--hold", type=float, default=8.0, help="seconden YOLO blijft aan na laatste beweging")
    ap.add_argument("--min-blob", type=float, default=0.004, help="min. beweging-oppervlak (fractie van beeld)")
    ap.add_argument("--birds", action="store_true", help="ook vogels melden")
    ap.add_argument("--save-frame", help="sla 1 frame met raster op en stop")
    a = ap.parse_args()

    if a.save_frame:
        return save_frame(a.rtsp, a.width, a.height, a.save_frame)
    if not a.model:
        sys.exit("--model is verplicht")
    det = YoloOnnx(a.model, a.imgsz, a.threads, a.birds)
    zone = load_zones(a.zones, (a.height, a.width))
    while True:
        run(ffmpeg_frames(a.rtsp, a.width, a.height, a.fps), det, a, a.width, a.height, a.fps, zone)
        if not a.rtsp.startswith("rtsp"):
            break
        print("stream weg, opnieuw proberen over 5s", file=sys.stderr)
        time.sleep(5)


if __name__ == "__main__":
    main()
