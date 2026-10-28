# Foxway Smart Doorbell System Specification (v2.6.0)

**Versie:** `v2.6.0` (`versionCode = 17`)  
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
   - **Native Hardware Streaming (go2rtc MSE + WebRTC):** Live streaming verloopt via go2rtc MSE + WebRTC (`/api/v1/live/player`, proxy `/api/go2rtc/ws`). 0% CPU serverbelasting tijdens live kijken, vlekkeloze weergave op 25 FPS zonder haperende secondenteller.
   - **Gecentraliseerde SQLite WAL Database (`app/database.py`):** WAL mode met `PRAGMA synchronous=NORMAL;`, `PRAGMA busy_timeout=60000;`, timeout=60.0s en async write lock serialisatie.
   - **Temporal Pixel-Diff AI Detector (`app/ai_detector.py`):** YOLOv8n (<45ms) op 5 FPS met 1.2s temporal grayscale difference mask. Bounding box pixel-diff check: geparkeerde auto's (<4% motion) worden direct als `STATIC_PARKED_CAR` genegeerd. Rijdende auto's worden geverifieerd via Manier 1 (nieuw in beeld, IoU < 0.50, 2 frames, ratio >= 0.15) of Manier 2 (inparkerend/wegrijdend, ratio >= 0.25, centroid shift >= 5%). Personen/dieren vereisen 2 opeenvolgende frames (0.4s) met ratio >= 0.08 en verplaatsing >= 3%. Cooldown is standaard 8s.
   - **Chronologische Unix-Timestamp Opnames & Anti-Time-Jump (`app/recorder.py`):** `/dev/shm/deurbel_ring` slaat segmenten op als `seg_%s.ts` met gap-detectie (>4.0s). 20s pre-roll en 20s post-roll.
   - **Web Admin Paneel (`/admin`):** Dark-mode dashboard met 25 FPS live preview player, dynamische variabelen, uitsluitingszones canvas en AI inspectie.
3. **Android Client App (`android-app`):**
   - Native Jetpack Compose, Kotlin coroutines, versie 2.6.0 (`versionCode = 17`).
   - Hardware-versnelde live streaming via `AndroidView` met native go2rtc speler (MSE + WebRTC).
   - Telecom framework integratie met `ConnectionService` voor volscherm belscherm met 10s deduplicatie.

---

## 2. Harde Systeemeisen & Beschermde Componenten
- **Geen Regressie:** Eerdere functionaliteit van v2.2.0, v2.3.0 en v2.4.0 (Telecom belscherm, C-SDK tweeweg-audio, mDNS discovery, Admin UI) blijft 100% gegarandeerd behouden.
- **Tweeweg-Audio (`backend/app/audio_bridge.py`):** De C-SDK `libhcnetsdk.so` met pre-warmed login sessies en 1000 Hz loopback FFT test is beschermd.
- **4:3 Aspect Ratio:** Alle weergaven in Android (`LiveScreen.kt`, `CallScreen.kt`, `ClipsScreen.kt`) en Web Admin (`/admin`) zijn strikt 4:3 (`768x576`).
- **RAM Ring-Buffer:** `/dev/shm/deurbel_ring` draait met chronologische Unix-timestamp bestandsnamen (`seg_%s.ts`) met 45s achtergrondopruiming.
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
