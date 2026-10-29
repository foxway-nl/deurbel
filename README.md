# Foxway Smart Doorbell (v2.8.0)

[![Version](https://img.shields.io/badge/version-2.8.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, 2-fasen clip lifecycle (`RECORDING` -> `READY`), directe push-notificatie miniatuurfoto's, native go2rtc hardware-streaming (MSE + WebRTC) over LAN en 5G/Cloudflare WSS (0ms lag, 25 FPS), SQLite WAL database concurrency, en het snelle YOLOv8n Temporal Pixel-Diff Masking AI-detectie model (5 FPS, <45ms).

---

## ⚡ Kernfunctionaliteiten in v2.8.0

1. **2-Fasen Clip Lifecycle (`RECORDING` -> `READY`) (`recorder.py`, `ai_detector.py`, `main.py`):**
   - Direct bij bewegingsdetectie $T_0$ wordt het geverifieerde frame synchroon weggeschreven naar `/mnt/opslag/deurbel/clips/thumb_<clip_id>.jpg` (>5 KB).
   - De clip-rij wordt direct in de SQLite database aangemaakt met `status = "RECORDING"`, zodat `GET /api/v1/clips/{clip_id}` direct 200 OK teruggeeft.
   - Pas zodra de 20s Post-Roll en de ffmpeg concat klaar zijn (`.mp4 > 100 KB`, `duration >= 5s`), wordt de clip bijgewerkt naar `status = "READY"`, `duration = echte_duur`, `file_size = echte_grootte`.

2. **Slimme Android Videospeler (`ClipsScreen.kt`):**
   - Geen zwart scherm of `00:00 / 00:00` meer bij het openen van een clip tijdens een lopende opname!
   - In `ClipPlayerDialog` toont de speler direct de live snapshot achtergrond met een status-overlay:
     `🔴 Opname wordt nog afgerond (20s na-opname)... Nog enkele seconden` + voortgangsindicator.
   - De speler pollt elke 2.0s op de achtergrond en start ExoPlayer automatisch en naadloos zodra de MP4 klaar is.
   - Bovenin toont de speler altijd de correcte datum en tijd (bijv. `Vandaag (29 okt) • 10:19:02`) in plaats van `"Eerdere opnames •"`.
   - In de opnamelijst toont een lopende opname de badge `🔴 Nu aan het opnemen...` in plaats van `00:00`.

3. **Gegarandeerde Push-Notificatie Miniatuurfoto's (`WebSocketForegroundService.kt`):**
   - Door de synchrone schrijfstap bestaat `/api/v1/clips/{clip_id}/thumbnail` al vóór de notificatie verzonden wordt.
   - De Android app downloadt de thumbnail via `$activeBaseUrl/api/v1/clips/${event.clipId}/thumbnail` (met 1 retry na 500ms) en zet zowel `.setLargeIcon(bitmap)` als `BigPictureStyle().bigPicture(bitmap)` op de melding.

4. **Native go2rtc Hardware Streaming (MSE + WebRTC):**
   - Transparante zero-buffer WebSocket proxy op `/api/go2rtc/ws` en `/api/v1/go2rtc/ws`.
   - Vloeiende 25 FPS video zonder CPU-hercodering via HTML5 `<video-stream>` player op `/api/v1/live/player`.

5. **Temporal Pixel-Diff AI Detector (`ai_detector.py`):**
   - Snelle YOLOv8n (<45ms) inferentie op 5 FPS (0.20s interval).
   - 1.2s temporal grayscale difference mask (`motion_mask`).
   - Bounding box pixel motion check: geparkeerde auto's (<4% motion) worden direct genegeerd als `STATIC_PARKED_CAR`. Cooldown 8s.

6. **Android Client App v2.8.0 (`versionCode = 19`):**
   - Gecompileerd met Gradle `--max-workers=2` in `thyrlian/android-sdk:latest`.

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
