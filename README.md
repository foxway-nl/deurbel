# Foxway Smart Doorbell (v2.3.0)

[![Version](https://img.shields.io/badge/version-2.3.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, hardware echo-onderdrukking, 3-laags AI-verificatie met YOLOv8s, een web-based beheerportaal (`/admin`), en een native Android-applicatie met volscherm Telecom oproepen.

---

## ⚡ Kernfunctionaliteiten in v2.3.0

1. **Web-Based Beheerportaal (`/admin`):**
   - Veilige dark-mode beheeromgeving via `http://192.168.2.1:8090/admin` en `https://deurbel.foxway.nl/admin`.
   - Live 4:3 cameramonitor met frame-age, FPS, CPU/RAM/schijf monitoring.
   - Realtime configuratie van AI thresholds, verplaatsingspercentages en interactieve uitsluitingszones zonder herstart.
   - AI Verificatie Inspector met de laatste 50 goedgekeurde en geblokkeerde detecties inclusief debug snapshots.
   - Gecentraliseerde weergave van backend logs en Android crash rapporten.

2. **3-Laags AI Verificatie Gatekeeper (`ai_detector.py`):**
   - **Laag 1 (Volledige Context):** YOLOv8s analyseert het volledige $960 \times 720$ 4:3 frame. Grondvlak-filtering sluit bomen en dakranden uit ($y_2 \ge 0.25h$).
   - **Laag 2 (Trajectverificatie):** 3-frame trajectory verificatie over $1.2\text{s}$. Vereist minimaal $6\%$ verplaatsing ($\Delta d \ge 0.06$) voor personen/dieren en $12\%$ ($\Delta d \ge 0.12$) voor voertuigen. Filtert wiebelende takken, wind en geparkeerde auto's $100\%$ uit.
   - **Laag 3 (Directe Push):** Stuurt binnen $1.5\text{s}$ pushberichten via FCM en WebSocket. Opnames duren de tijd van de passage $+ 5\text{s}$ post-roll (max 25s).

3. **Zero-Lag Live Video Drain (`live_hub.py`):**
   - Achtergrond drain-worker leest direct via OS pipe `/dev/shm/deurbel_ring` en levert altijd het nieuwste frame met $<100\text{ms}$ vertraging.

4. **Android Stabiliteit & Crash Logger (`android-app`):**
   - Versie 2.3.0 (code 14).
   - Globale exception handlers sturen stacktraces naar `POST /api/v1/logs/client-crash` en bewaren `crash_last.json` lokaal.
   - `RGB_565` decodering halveert het geheugengebruik en voorkomt OutOfMemory crashes.
   - Thread-safe `AudioTrack` met mutex-bescherming tegen `IllegalStateException`.
   - `"🔄 Sync Live"` knop in live weergave voor directe resync zonder zwart beeld.

---

## 🏗️ Architectuur & Poorten

| Poort / Pad | Protocol | Omschrijving |
|---|---|---|
| `8090` | HTTP / WebSocket | FastAPI Backend (`/admin`, `/api/v1/*`, `/api/v1/ws/live`) |
| `8000` | TCP (Hikvision C-SDK) | EZVIZ DB1 Tweeweg-audio (`libhcnetsdk.so`) |
| `8554` / `1984` | RTSP / HTTP | go2rtc RTSP & WebRTC gateway |
| `/dev/shm/deurbel_ring` | RAM Buffer | Continu 2-seconden Pre-Roll segmenten buffer |
| `/mnt/opslag/deurbel/clips` | Opslag | MP4 videoclips en JPEG snapshots |

---

## 🚀 Snelle Start

### Backend (Docker)
```bash
docker compose up -d
docker logs -f foxway-doorbell-backend
```

### Pytest Verificatietests
```bash
docker exec foxway-doorbell-backend pytest -v backend/tests/
```

### Android APK Bouwen
```bash
docker run --rm -v /mnt/opslag/deurbel:/mnt/opslag/deurbel -w /mnt/opslag/deurbel/android-app thyrlian/android-sdk:latest ./gradlew assembleRelease --max-workers=2
```

---

## 🔐 Web Admin Toegang
- **URL:** `http://192.168.2.1:8090/admin` of `https://deurbel.foxway.nl/admin`
- **Gebruiker:** `admin`
- **Wachtwoord:** Ingesteld in SQLite database / `.env`
