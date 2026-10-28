# Foxway Smart Doorbell (v2.4.0)

[![Version](https://img.shields.io/badge/version-2.4.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, SQLite WAL database concurrency, 20s pre-roll / 20s post-roll clipopname, robuuste zero-lag live stream met HTTP snapshot fallback, 3-laags AI-verificatie met YOLOv8s, een web-based beheerportaal (`/admin`), en een native Android-applicatie met volscherm Telecom oproepen.

---

## ⚡ Kernfunctionaliteiten in v2.4.0

1. **Hersteld & Robuust Live Beeld (`live_hub.py` & Android):**
   - Schone, low-latency FFmpeg pipeline naar go2rtc via TCP RTSP.
   - Live frames worden in RAM bewaard (`latest_jpeg`).
   - `ai_detector.py` en `/admin` lezen direct uit deze centrale RAM buffer (0 extra RTSP sessies, 0 connectieproblemen).
   - Android `LiveStreamController` bevat automatische HTTP snapshot polling (150ms) bij proxy vertragingen en naadloze videoweergave in zowel `LiveScreen.kt` als `CallScreen.kt`.

2. **Gecentraliseerde SQLite WAL Architectuur (`database.py`):**
   - Oplossing voor `database is locked` via `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;` en `PRAGMA busy_timeout=60000;`.
   - Serialisatie van parallelle writes via asyncio event-loop locks en threading re-entrant locks.

3. **20s Pre-Roll & 20s Post-Roll Opname (`recorder.py`):**
   - RAM ring-buffer met 25 segmenten van 2 seconden (50 seconden capaciteit in `/dev/shm/deurbel_ring`).
   - Pre-roll segmenten worden strikt op timestamp gefilterd binnen $[T_0 - 20s, T_0]$ (exact 10 segmenten = 20s).
   - Volledig dynamisch configureerbaar via `/admin`: `pre_roll_seconds`, `post_roll_seconds`, `max_active_motion_seconds`.
   - Thumbnail generatie op $T_0$ direct uit de live RAM cache voorkomt zwarte previews.

4. **Web-Based Beheerportaal (`/admin`):**
   - Dark-mode beheeromgeving via `http://192.168.2.1:8090/admin` en `https://deurbel.foxway.nl/admin`.
   - Live 4:3 cameramonitor met frame-age, FPS, CPU/RAM/schijf monitoring.
   - Realtime configuratie van AI thresholds, pre/post roll en uitsluitingszones zonder herstart.
   - AI Verificatie Inspector met de laatste 50 goedgekeurde en geblokkeerde detecties inclusief debug snapshots.
   - Gecentraliseerde weergave van backend logs en Android crash rapporten.

5. **Android App (`android-app`):**
   - Versie 2.4.0 (code 15).
   - Volledig 4:3 weergave met RGB_565 decoding en AudioTrack thread-safety.
   - Telecom framework integratie met ConnectionService voor volscherm belscherm.

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
