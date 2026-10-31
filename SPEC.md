# Foxway Smart Doorbell System Specification (v3.4.1)

**Versie:** `v3.4.1` (`versionCode = 31`)  
**Datum:** 31 Oktober 2026  
**Productie-omgeving:** `foxway` (`192.168.2.1`), WAN: `https://deurbel.foxway.nl`  
**Camera:** EZVIZ DB1 Pro (`192.168.2.31`), native **4:3 aspect ratio** (`768x576` / `960x720`)

---

## 1. Systeemoverzicht & Architectuur
Het Foxway Deurbelsysteem is een end-to-end, privacy-vriendelijk en ultra-low-latency videodeurbelsysteem bestaande uit:
1. **EZVIZ DB1 Pro Camera (Alleen Poort 8000 C-SDK & Poort 554 RTSP):**
   - **BELANGRIJK:** Poort 80 (HTTP / ISAPI / ONVIF) is **NIET AANWEZIG** op de camera. Er wordt NOOIT verbonden met poort 80 of `/ISAPI/...`.
   - Native 4:3 beeldsensor met microfoon en luidspreker.
   - Tweeweg-audio gestuurd via `libhcnetsdk.so` C-SDK (`app/audio_bridge.py`) over TCP poort 8000.
   - **Hardware Drukknop-Detectie & Host-Level Cloud Sniffer (`backend/app/network_sniffer.py`):**
     - Omdat EZVIZ hardware intercom event `0x1132` op Poort 8000 blokkeert, is de drukknop detectie dubbel gewaarborgd:
       - **Shazam Chime Detector** (`ring_detector.py`, 728 Hz / 570 Hz dual-tone, NCC $\ge 0.72$).
       - **Host-Level Actieve ARP/Packet Sniffer** (`network_sniffer.py`): stuurt gerichte ARP reply naar `192.168.2.31` elke 2.0s via host `192.168.2.1` (`eno1`), vangt uitgaand WAN cloudverkeer op in 1000ms vensters en triggert `trigger_source = 'NETWORK_SNIFF'` (of `'SHAZAM + SNIFF'` bij gelijktijdige detectie).
       - Fail-safe atexit/signal handlers versturen 3x ARP restore packets (`00:f8:cc:d0:9e:00`) zodat internettoegang nooit verstoord raakt.
     - C-SDK Poort 8000 vangt hardware bewegingsalarm `0x4000 dwAlarmType == 3` op en verzorgt tweeweg-audio.
2. **FastAPI & Python Media Server (`backend` in Docker):**
   - Draait op poort 8090 met `network_mode: host`.
   - **Real-Time 3-Layer Shazam Deurbel-Chime Detector (`ring_detector.py`):** Gekalibreerd op echte fysieke deurbelopnames (728 Hz / 570 Hz dual-tone). Laag 1: spectrale whitening, Laag 2: kwarts-klok toonstabiliteit binnen +-15 Hz, Laag 3: 2D Normalized Cross-Correlation (NCC $\ge 0.72$, 12s cooldown). Logt alle evaluaties $\ge 0.45$ NCC in daily JSONL en triggert oproep met `trigger_source = 'SHAZAM_CHIME'`.
   - **Zero-UI Live Player op `/api/v1/live/player`:** Minimalistische speler gebaseerd op `<zero-player>` en `/api/go2rtc/video-rtc.js`. Instant 4:3 poster gerenderd op ms 0 via `<img id="instant-poster">`, direct verwijderd op eerste video-frame. Live-edge lag recovery op `<0.3s` via 250ms interval, `window.setAudioMuted()`, `window.killStream()`, `window.setVideoZoom()`, en stabiele WS-herverbinding uitsluitend bij `onclose`/`onerror`.
   - **Notificatie-Poortwachter & Stille Uren (23:30 - 07:30):** Wanneer een categorie `Melding` op UIT staat, zet de backend `silent = True` en `should_notify = False`, waardoor FCM en client-notificaties 100% worden onderdrukt. Deurbeloproepen (`CALL_RINGING`) rinkelen altijd en worden nooit onderdrukt.
   - **10/10 EZVIZ AI-Gevoeligheid & Smart Motion Fallback (`app/ai_detector.py`):**
     - Dynamisch geheugen voor stilstaande auto's filtert geparkeerde auto's betrouwbaar uit.
     - Bij camera hardware beweging (`0x4000`) wordt de YOLOv8 detectiedrempel voor 10s verlaagd naar `0.22` en 4 FPS scan-burst geactiveerd.
     - Indien YOLO geen objecten detecteert, activeert slimme contour motion fallback (`area >= 1.5%`, verplaatsing $\ge 2\%$ over 3 frames) buiten stilstaande auto's en boomkruinen en keurt goed als `label = "Beweging (Hardware)"`.
   - **Netwerk- en WiFi/5G Herstel:** Zowel backend C-SDK als Shazam herverbinden binnen 3.0s met `[CAMERA RECONNECTED]`. Android `WebSocketForegroundService` detecteert via `ConnectivityManager.NetworkCallback()` direct netwerkwissels en herverbindt via backoff ladder `1s -> 3s -> 5s -> 15s`.
   - **Web Admin Paneel (`/admin`):** Dark-mode dashboard met live preview player, real-time Poort 8000 SDK packet sniffer tabel, native C-SDK log viewer modal, Shazam Chime live monitor en Live EZVIZ Cloud-Sniffer tabel.
3. **Android Client App (`android-app`):**
   - Native Jetpack Compose, Kotlin coroutines, versie 3.4.1 (`versionCode = 31`). Badges `[SHAZAM_CHIME]`, `[NETWORK_SNIFF]`, `[SHAZAM + SNIFF]` in Clips en 30-dagen Logboek.
   - **R8 Minification Uitgeschakeld:** `isMinifyEnabled = false` en `isShrinkResources = false` waarborgen foutloze JSON/Gson deserialisatie van data-classes (`DoorbellEvent`, `CallRingingPayload`, instellingen DTOs).
   - **Verbeterde Foreground Service Compatibiliteit:** Special-use foreground service types worden uitsluitend op Android 14+ (`API 34`) meegegeven, waardoor crashes op Android 10-13 zijn verholpen.
   - **Real-Time Belscherm Activatie:** Directe activering van inkomende oproepen zonder foutieve klokskew-suppressie.
   - **Directe 2-Standen Luidsprekerknop:** In `LiveScreen.kt` direct Rood (Gedempt) of Groen (Geluid Aan), zonder grijze `SurfaceGlass` tussenstand.
   - **Prominente Systeemlogboek Knop:** Direct bovenaan Kaart 4 in `SettingsScreen.kt` en als actieknop in de TopAppBar.
   - **SharedPreferences Notificatie Synchronisatie:** Slaat categorie-schakelaars lokaal op en onderdrukt notificaties in `WebSocketForegroundService.kt` wanneer een categorie UIT staat of het event `silent = true` heeft.
   - **Fout / Status-Dump Reporter:** `"📤 Rapporteer Fout / Status-Dump"` knop in `SettingsScreen.kt` verstuurt toestelspecificaties, schermstatus en de laatste 40 app-logs naar `POST /api/v1/logs/client`.
   - **Strikte 4:3 Aspect Ratio Container:** `LiveScreen.kt`, `CallScreen.kt` en `ClipsScreen.kt` hanteren een harde, onwrikbare `.fillMaxWidth().aspectRatio(4f / 3f)` container.
   - **Synchrone Hard-Kill voor Audio & Streams:** Bij schermwissels, minimaliseren of verlaten stopt alle audio en video onmiddellijk via `window.killStream()`, `onPause()`, `pauseTimers()`, `loadUrl("about:blank")` + `destroy()` en `hardKillAudio()`.
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
