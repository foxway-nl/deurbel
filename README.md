# Foxway Smart Doorbell (v2.6.0)

[![Version](https://img.shields.io/badge/version-2.6.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, native go2rtc hardware-streaming (MSE + WebRTC) over LAN en 5G/Cloudflare WSS (0ms lag, 25 FPS), SQLite WAL database concurrency, en het snelle YOLOv8n Temporal Pixel-Diff Masking AI-detectie model (5 FPS, <45ms) dat rijdende auto's en personen betrouwbaar detecteert en geparkeerde auto's direct negeert.

---

## ⚡ Kernfunctionaliteiten in v2.6.0

1. **Native go2rtc Hardware Streaming (MSE + WebRTC) (`main.py`, `LiveScreen.kt`, `CallScreen.kt`, `/admin`):**
   - Transparante zero-buffer WebSocket proxy op `/api/go2rtc/ws` en `/api/v1/go2rtc/ws`.
   - Vloeiende 25 FPS video zonder CPU-hercodering via HTML5 `<video-stream>` player op `/api/v1/live/player`.
   - 0% CPU serverbelasting tijdens live kijken; `live_hub.py` draait geen continue image2pipe transcodering meer wanneer geen WebSocket clients luisteren.

2. **Temporal Pixel-Diff AI Detector (`ai_detector.py`):**
   - Snelle YOLOv8n (<45ms) inferentie op 5 FPS (0.20s interval).
   - 1.2s temporal grayscale difference mask (`motion_mask`).
   - Bounding box pixel motion check: geparkeerde auto's (<4% motion) worden direct genegeerd als `STATIC_PARKED_CAR`.
   - Rijdende voertuigen worden direct geverifieerd via Manier 1 (nieuw in beeld, IoU < 0.50, 2 frames, ratio >= 0.15) of Manier 2 (inparkerend/wegrijdend, ratio >= 0.25, centroid shift >= 5%).
   - Personen en dieren worden binnen 2 frames (0.4s) geverifieerd (ratio >= 0.08, verplaatsing >= 3%).
   - Event cooldown verlaagd naar 8 seconden.

3. **Chronologische Unix-Timestamp Opnames & Anti-Time-Jump (`recorder.py`):**
   - RAM ring-buffer slaat chronologische segmenten op (`seg_%s.ts`) met 20s pre-roll en 20s post-roll.
   - Gap-detectie (>4.0s) prunet oude segmenten vóór een gap, wat terugspringen in de tijd definitief uitsluit.

4. **Android Client App v2.6.0 (`versionCode = 17`):**
   - Hardware-versnelde live stream in `LiveScreen.kt` en `CallScreen.kt` via native WebView gekoppeld aan go2rtc MSE/WebRTC.
   - Telecom integratie met full-screen belnotificaties en tweeweg-audio talkback via C-SDK Poort 8000.

---

## 🏗️ Architectuur & Poorten

| Poort / Pad | Protocol | Omschrijving |
|---|---|---|
| `8090` | HTTP / WebSocket | FastAPI Backend (`/admin`, `/api/v1/*`, `/api/v1/ws/live`, `/api/v1/live/instant.jpg`) |
| `8000` | TCP (Hikvision C-SDK) | EZVIZ DB1 Tweeweg-audio (`libhcnetsdk.so`) |
| `8554` / `1984` | RTSP / HTTP | go2rtc RTSP & WebRTC gateway |
| `/dev/shm/deurbel_ring` | RAM Buffer | Continu 2-seconden Pre-Roll segmenten buffer (25 wrap) |
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
docker exec foxway-doorbell-backend pytest -v /mnt/opslag/deurbel/backend/tests/
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
