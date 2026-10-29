# Foxway Smart Doorbell System Specification (v2.9.0)

**Versie:** `v2.9.0` (`versionCode = 20`)  
**Datum:** 29 Oktober 2026  
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
   - **Zero-UI Live Player op `/api/v1/live/player`:** Eigen lichtgewicht speler gebaseerd op `<zero-player>` en `/api/go2rtc/video-rtc.js`. Toont direct op milliseconde 0 het laatste snapshot (`/api/v1/live/instant.jpg`) als achtergrond/poster, verwijdert alle controls via MutationObserver, verbergt go2rtc overlays via CSS en beschikt over een 1500ms freeze watchdog die bij stilstand direct herverbindt.
   - **Onbeperkt Opeenvolgend Deurbel Testen:** `POST /api/v1/webhook/shelly-ring?force=true` omzeilt alle debounces en reset eerdere gespreksstatussen direct om telkens een nieuwe oproep met unieke call_id uit te zenden.
   - **Stille Uren (23:30 - 07:30 Europe/Amsterdam):** AI bewegingsclips worden 's nachts stil opgenomen (`silent = True`, geen pushmelding/FCM), terwijl deurbeloproepen altijd met hoogste prioriteit doorkomen. Instelbaar via `/admin` en de Android app.
   - **Anti-Car Swap Filter (`app/ai_detector.py`):** `parked_spots` pre-filtering (`IoU >= 0.55` en `changed_pixel_ratio < 0.30` direct afgekeurd) en stap-verplaatsing validatie (`0.02*w <= step_disp <= 0.12*w` met `IoU >= 0.15` tussen opeenvolgende 5 FPS frames) voorkomen dat de detector tussen stilstaande auto's verspringt.
   - **2-Fasen Clip Lifecycle (`RECORDING` -> `READY`):** Bij detectie $T_0$ wordt direct synchroon de geverifieerde miniatuurfoto opgeslagen naar `/mnt/opslag/deurbel/clips/thumb_<clip_id>.jpg` (>5 KB). Meteen daarna wordt de clip-rij met `status = "RECORDING"` aangemaakt en pas daarna gaat de push-melding uit. Pas na afronding van de 20s Post-Roll en concat (`.mp4 > 100 KB`, `duration >= 5s`) wordt de status geüpdatet naar `READY`.
   - **Gecentraliseerde SQLite WAL Database (`app/database.py`):** WAL mode met `PRAGMA synchronous=NORMAL;`, `PRAGMA busy_timeout=60000;`, timeout=60.0s en async write lock serialisatie.
   - **Web Admin Paneel (`/admin`):** Dark-mode dashboard met 25 FPS live preview player, dynamische variabelen, stille uren configuratie, uitsluitingszones canvas en AI inspectie.
3. **Android Client App (`android-app`):**
   - Native Jetpack Compose, Kotlin coroutines, versie 2.9.0 (`versionCode = 20`).
   - `WebChromeClient.getDefaultVideoPoster()` retourneert een 1x1 transparante bitmap (0% grijze play-knop).
   - "🔄 Sync Live" knop herlaadt en roept direct `window.reconnectStream()` aan.
   - Cellular-only WAN lock in `ConnectionManager.kt` en `EndpointResolver.kt` voorkomt LAN ping-pong op mobiele data.
   - Sluiten van clips in `ClipsScreen.kt` stopt en released ExoPlayer direct en wist deeplink parameters.

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
