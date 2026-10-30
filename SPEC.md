# Foxway Smart Doorbell System Specification (v3.2.0)

**Versie:** `v3.2.0` (`versionCode = 27`)  
**Datum:** 30 Oktober 2026  
**Productie-omgeving:** `foxway` (`192.168.2.1`), WAN: `https://deurbel.foxway.nl`  
**Camera:** EZVIZ DB1 Pro (`192.168.2.31`), native **4:3 aspect ratio** (`768x576` / `960x720`)

---

## 1. Systeemoverzicht & Architectuur
Het Foxway Deurbelsysteem is een end-to-end, privacy-vriendelijk en ultra-low-latency videodeurbelsysteem bestaande uit:
1. **EZVIZ DB1 Pro Camera (Alleen Poort 8000 C-SDK & Poort 554 RTSP):**
   - **BELANGRIJK:** Poort 80 (HTTP / ISAPI / ONVIF) is **NIET AANWEZIG** op de camera. Er wordt NOOIT verbonden met poort 80 of `/ISAPI/...`.
   - Native 4:3 beeldsensor met microfoon en luidspreker.
   - Tweeweg-audio gestuurd via `libhcnetsdk.so` C-SDK (`app/audio_bridge.py`) over TCP poort 8000.
   - **Hardware Drukknop-Detectie (`backend/app/sdk_alarm_listener.py`):** `NET_DVR_SetupAlarmChan_V41` op poort 8000 vangt hardware intercom- en alarm-events synchroon op met persistente callback-referentie (`self._callback_ref`).
     - `COMM_UPLOAD_VIDEO_INTERCOM_EVENT` (0x1132) met byte 44 == 1 triggert direct een deurbel-oproep (Visitor Call).
     - `COMM_ALARM_VIDEO_INTERCOM` (0x1133) met byte 44 in (1, 14) triggert direct een deurbel-oproep.
     - `COMM_ALARM_V30` (0x4000) of `COMM_ALARM_V40` (0x4007) met `dwAlarmType == 3` (Motion) wordt 100% genegeerd en niet weggeschreven; elk ander `dwAlarmType` triggert een legitieme oproep.
     - Alle onbekende commando's worden met hun eerste 64 hex payload bytes gelogd in het admin paneel.
2. **FastAPI & Python Media Server (`backend` in Docker):**
   - Draait op poort 8090 met `network_mode: host`.
   - **Zero-UI Live Player op `/api/v1/live/player`:** Minimalistische speler gebaseerd op `<zero-player>` en `/api/go2rtc/video-rtc.js`. Geen instant.jpg polling of poster, CSS `object-fit: fill !important`, buffer-lag recovery op 0.8s via `ontimeupdate`, `window.setAudioMuted()`, `window.killStream()`, `window.setVideoZoom()`, en stabiele WS-herverbinding uitsluitend bij `onclose`/`onerror`.
   - **Onbeperkt Opeenvolgend Deurbel Testen:** `POST /api/v1/webhook/shelly-ring?force=true` omzeilt alle debounces en reset eerdere gespreksstatussen direct om telkens een nieuwe oproep met unieke call_id uit te zenden.
   - **Stille Uren (23:30 - 07:30 Europe/Amsterdam):** AI bewegingsclips worden 's nachts stil opgenomen (`silent = True`, geen pushmelding/FCM), terwijl deurbeloproepen altijd met hoogste prioriteit doorkomen. Instelbaar via `/admin` en de Android app.
   - **Anti-Car Swap Filter (`app/ai_detector.py`):** `parked_spots` pre-filtering (`IoU >= 0.55` en `changed_pixel_ratio < 0.30` direct afgekeurd) en strikte stap-verplaatsing validatie over $\ge 3$ opeenvolgende frames (`0.02*w <= step_disp <= 0.12*w` met `IoU >= 0.15` en totale verplaatsing $\ge 14\%$) voorkomen dat de detector tussen stilstaande auto's verspringt.
   - **Geen Akoestische Chime (Hardware-Only):** Akoestische / FFT-gebaseerde chime-detectie (`ring_detector.py`) is definitief en volledig verwijderd. Alleen hardware triggers (Poort 8000 C-SDK en Shelly Plus Uni webhook) zijn actief.
   - **2-Fasen Clip Lifecycle (`RECORDING` -> `READY`):** Bij detectie $T_0$ wordt direct synchroon de geverifieerde miniatuurfoto opgeslagen naar `/mnt/opslag/deurbel/clips/thumb_<clip_id>.jpg` (>5 KB). Meteen daarna wordt de clip-rij met `status = "RECORDING"` aangemaakt en pas daarna gaat de push-melding uit. Pas na afronding van de 20s Post-Roll en concat (`.mp4 > 100 KB`, `duration >= 5s`) wordt de status geüpdatet naar `READY`.
   - **Gecentraliseerde SQLite WAL Database (`app/database.py`):** WAL mode met `PRAGMA synchronous=NORMAL;`, `PRAGMA busy_timeout=60000;`, timeout=60.0s en async write lock serialisatie.
   - **Web Admin Paneel (`/admin`):** Dark-mode dashboard met 25 FPS live preview player, dynamische variabelen, stille uren configuratie, uitsluitingszones canvas, real-time Poort 8000 SDK hardware events viewer en AI inspectie.
3. **Android Client App (`android-app`):**
   - Native Jetpack Compose, Kotlin coroutines, versie 3.2.0 (`versionCode = 27`).
   - **Strikte 4:3 Aspect Ratio Container:** `LiveScreen.kt`, `CallScreen.kt` en `ClipsScreen.kt` hanteren een harde, onwrikbare `.fillMaxWidth().aspectRatio(4f / 3f).clipToBounds()` container zonder vervormende rekking of uitrek-toggles.
   - **Pinch-to-Zoom & Pan:** Transparante Compose gebaren-overlay met `detectTransformGestures` (schaal 1x tot 5x, pan) en `detectTapGestures` dubbeltik-reset over alle videoschermen (Live, Call, Clips).
   - **Synchrone Hard-Kill voor Audio & Streams:** Geen offscreen verborgen WebViews in `MainActivity.kt`. Bij schermwissels, minimaliseren (`onStop()`, `onPause()`, `onDestroy()`) of verlaten via `onDispose` stopt alle audio en video onmiddellijk via `window.killStream()`, `onPause()`, `pauseTimers()`, `loadUrl("about:blank")` + `destroy()` en `hardKillAudio()`.
   - `LiveScreen` start standaard gedempt (`isSpeakerMuted = true`), speaker toggle stuurt direct `window.setAudioMuted` en `AudioManager.isSpeakerphoneOn` aan.
   - Cellular-only WAN lock in `ConnectionManager.kt` en `EndpointResolver.kt` voorkomt LAN ping-pong op mobiele data (bij geen Wi-Fi direct en permanent WAN).

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
