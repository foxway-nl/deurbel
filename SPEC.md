# Foxway Smart Doorbell System Specification (v2.4.0)

**Versie:** `v2.4.0` (`versionCode = 15`)  
**Datum:** 28 Oktober 2026  
**Productie-omgeving:** `foxway` (`192.168.2.1`), WAN: `https://deurbel.foxway.nl`  
**Camera:** EZVIZ DB1 (`192.168.2.31`), native **4:3 aspect ratio** (`768x576` / `960x720`)

---

## 1. Systeemoverzicht & Architectuur
Het Foxway Deurbelsysteem is een end-to-end, privacy-vriendelijk en ultra-low-latency videodeurbelsysteem bestaande uit:
1. **EZVIZ DB1 Camera (Poort 8000 C-SDK & RTSP):**
   - Native 4:3 beeldsensor met microfoon en luidspreker.
   - Tweeweg-audio gestuurd via `libhcnetsdk.so` C-SDK (`app/audio_bridge.py`) over TCP poort 8000.
2. **FastAPI & Python Media Server (`backend` in Docker):**
   - Draait op poort 8090 met `network_mode: host`.
   - **Gecentraliseerde SQLite WAL Database (`app/database.py`):** WAL mode met `PRAGMA synchronous=NORMAL;`, `PRAGMA busy_timeout=60000;`, timeout=60.0s en async write lock serialisatie voorkomt `database is locked` definitief.
   - **Zero-lag Live Drain & RAM Sourcing (`app/live_hub.py`):** Dedicated achtergrond-drain leest via FFmpeg van lokale go2rtc stream en bewaart het actuele JPEG frame in RAM (`<100ms` latentie). `ai_detector.py` en `/admin` halen hun frames direct uit deze centrale RAM buffer zonder extra RTSP sessies.
   - **3-Laags AI Gatekeeper (`VerificationGatekeeper`):** YOLOv8s op volledig 4:3 frame, grondvlak-filtering ($y_2 \ge 0.25h$), interactieve uitsluitingszones, 3-frame trajectcontrole ($\Delta d \ge 6\%$ voor persoon/dier, $\ge 12\%$ voor voertuig).
   - **Direct Push Coupling & 20s/20s Opname:** Stuurt FCM en WebSocket push notificaties binnen 1.5s (stub-event op $T_0 + 1.0s$). Pre-roll clips bevatten exact 20 seconden vóór $T_0$ en 20 seconden post-roll na beweging.
   - **Web Admin Paneel (`/admin`):** Dark-mode dashboard met sessie-authenticatie, live videomonitor, dynamische variabelen (`pre_roll_seconds`, `post_roll_seconds`, `max_active_motion_seconds`), uitsluitingszones canvas, AI inspectietabel en Android crash logs.
3. **Android Client App (`android-app`):**
   - Native Jetpack Compose, Kotlin coroutines, versie 2.4.0 (`versionCode = 15`).
   - Telecom framework integratie met `ConnectionService` voor volscherm belscherm (`TelecomManager`).
   - `RGB_565` bitmap decodering en `AudioTrack` mutex-bescherming tegen OOM en threading crashes.
   - `LiveStreamController` met automatische 150ms HTTP instant snapshot fallback (`/api/v1/live/instant.jpg`) bij proxy/WebSocket vertraging, en naadloze videoweergave in zowel `LiveScreen.kt` als `CallScreen.kt`.
   - Automatische crash logger naar `crash_last.json` en `POST /api/v1/logs/client-crash`.

---

## 2. Harde Systeemeisen & Beschermde Componenten
- **Geen Regressie:** Eerdere functionaliteit van v2.2.0 en v2.3.0 (Telecom belscherm, C-SDK tweeweg-audio, mDNS discovery, Admin UI) blijft 100% gegarandeerd behouden.
- **Tweeweg-Audio (`backend/app/audio_bridge.py`):** De C-SDK `libhcnetsdk.so` met pre-warmed login sessies en 1000 Hz loopback FFT test is beschermd.
- **4:3 Aspect Ratio:** Alle weergaven in Android (`LiveScreen.kt`, `CallScreen.kt`, `ClipsScreen.kt`) en Web Admin (`/admin`) zijn strikt 4:3 (`768x576`).
- **RAM Ring-Buffer:** `/dev/shm/deurbel_ring` draait met `-segment_wrap 25` (50 seconden buffer in RAM) voor robuuste 20-seconden Pre-Roll.
- **Systeembronnen:** FFmpeg draait altijd met `nice -n 15`. Gradle builds met `--max-workers=2`.

---

## 3. Database Concurrency & Stabiliteit (`database.py`)
- **WAL Modus:** `PRAGMA journal_mode=WAL;` staat parallelle lezers toe zonder schrijvers te blokkeren.
- **Busy Timeout:** 60.000 ms (`PRAGMA busy_timeout=60000;`) voorkomt `OperationalError('database is locked')`.
- **Async Write Lock:** Schrijfacties in `database.py` worden geserialiseerd via een event-loop-specifieke `asyncio.Lock` en `threading.RLock()`.

---

## 4. Pre-Roll, Post-Roll & Opnamelengte (`recorder.py`)
- **Standaardwaarden:**
  - `pre_roll_seconds = 20`
  - `post_roll_seconds = 20`
  - `max_active_motion_seconds = 30`
- **Segmentselectie:** `recorder.py` filtert `.ts`-segmenten in `/dev/shm/deurbel_ring` strikt op basis van bestandswijzigingstijd ($mtime$) tussen $[T_0 - \text{preroll\_seconds}, T_0]$, wat exact resulteert in 10 segmenten van 2 seconden.
- **Thumbnail Generatie:** Snapshot wordt direct op $T_0$ uit de centrale RAM buffer gecachet, of als fallback via `ffmpeg -ss <preroll_seconds>` gegenereerd, waardoor zwarte thumbnails tot het verleden behoren.

---

## 5. Web Admin Dashboard (`/admin`)
- **Login:** `/admin/login` beveiligd met cookie-gebaseerde sessie (`doorbell_admin_session`).
- **Tab 1: Live Systeem & Stream Monitor:** Live 4:3 weergave met actuele frame-age, FPS, CPU/RAM status en resync-knoppen.
- **Tab 2: Dynamische Variabelen & Uitsluitingszones:** Realtime instellen van AI thresholds, `pre_roll_seconds`, `post_roll_seconds`, `max_active_motion_seconds` en uitsluitingszones canvas.
- **Tab 3: AI Verificatie Inspector:** Laatste 50 evaluaties met bounding boxes, delta-displacement en auditaanleiding.
- **Tab 4: Foutenlogboek & Crash Reports:** Server ring-logger en Android crash logs.
