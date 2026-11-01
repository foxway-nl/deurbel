# Foxway Smart Doorbell System Specification (v3.5.0)

**Versie:** `v3.5.0` (`versionCode = 32`)  
**Datum:** 1 November 2026  
**Productie-omgeving:** `foxway` (`192.168.2.1`), WAN: `https://deurbel.foxway.nl`  
**Camera:** EZVIZ DB1 Pro (`192.168.2.31`), native **4:3 aspect ratio** (`768x576` / `960x720`)

---

## 1. Systeemoverzicht & Architectuur
Het Foxway Deurbelsysteem is een end-to-end, privacy-vriendelijk en ultra-low-latency videodeurbelsysteem bestaande uit:
1. **EZVIZ DB1 Pro Camera (Alleen Poort 8000 C-SDK & Poort 554 RTSP):**
   - **BELANGRIJK:** Poort 80 (HTTP / ISAPI / ONVIF) is **NIET AANWEZIG** op de camera. Er wordt NOOIT verbonden met poort 80 of `/ISAPI/...`.
   - Native 4:3 beeldsensor met microfoon en luidspreker.
   - Tweeweg-audio gestuurd via `libhcnetsdk.so` C-SDK (`app/audio_bridge.py`) over TCP poort 8000.
   - **Dual Aanbel-Coördinator & Wederzijdse Foutcontrole (`backend/app/ring_coordinator.py`, `network_sniffer.py`, `ring_detector.py`):**
     - Omdat EZVIZ hardware intercom event `0x1132` op Poort 8000 blokkeert, is de drukknop detectie dubbel gewaarborgd:
       - (1) **Real-Time 3-Layer Shazam Deurbel-Chime Detector** (`ring_detector.py`, 728 Hz / 570 Hz dual-tone akoestische correlatie NCC $\ge 0.72$, 12s cooldown).
       - (2) **Host-Level Actieve ARP/EZVIZ Cloud Sniffer** (`network_sniffer.py`): negeert geïsoleerde 191B keepalives op TCP 8820, detecteert Patroon A (payload 195-400B) en Patroon B (>=3 pkts van 191B binnen 1000ms).
       - **Centrale Ring Coördinator (`ring_coordinator.py`):** Roept bij het allereerste signaal direct `call_sm.ring(...)` aan op ms 0 (zero latency). Een achtergrondvenster van 3.5s verifieert de komst van het andere signaal (`[RING VERIFIED]` bij gelijktijdige aflevering; `[RING MISMATCH ERROR]` bij ontbreken van één signaal).
     - C-SDK Poort 8000 vangt hardware bewegingsalarm `0x4000 dwAlarmType == 3` op en verzorgt tweeweg-audio.
2. **FastAPI & Python Media Server (`backend` in Docker):**
   - Draait op poort 8090 met `network_mode: host`.
   - **Dag-Mappen (`/clips/YYYY-MM-DD/`) & 50.0 GB Opslaglimiet (`recorder.py`, `main.py`):** Alle nieuwe video-opnames en miniaturen worden chronologisch opgeslagen in dag-mappen. Bij verwijderen worden lege dag-mappen opgeruimd. Standaard opslaglimiet verhoogd naar 50.0 GB (51200 MB).
   - **Server-Side Paginering & SQL-Filtering (`models.py`, `main.py`):** `GET /api/v1/clips?category=...&date=...&offset=...&limit=...` filtert direct in SQLite vóór `LIMIT` en `OFFSET`.
   - **Zero-UI Live Player op `/api/v1/live/player`:** Minimalistische speler gebaseerd op `<zero-player>` en `/api/go2rtc/video-rtc.js`. Instant 4:3 poster gerenderd op ms 0 via `<img id="instant-poster">`, direct verwijderd op eerste video-frame. Live-edge lag recovery op `<0.3s` via 250ms interval, `window.setAudioMuted()`, `window.killStream()`, `window.setVideoZoom()`, en stabiele WS-herverbinding uitsluitend bij `onclose`/`onerror`.
   - **Notificatie-Poortwachter & Stille Uren (23:30 - 07:30):** Wanneer een categorie `Melding` op UIT staat, zet de backend `silent = True` en `should_notify = False`, waardoor FCM en client-notificaties 100% worden onderdrukt. Deurbeloproepen (`CALL_RINGING`) rinkelen altijd en worden nooit onderdrukt.
   - **10/10 EZVIZ AI-Gevoeligheid & Smart Motion Fallback (`app/ai_detector.py`):** Dynamisch geheugen voor stilstaande auto's filtert geparkeerde auto's betrouwbaar uit. Contour motion fallback buiten geparkeerde auto's en boomkruinen bij hardware beweging.
   - **Web Admin Paneel (`/admin`):** Dark-mode dashboard met live preview player, Dual Aanbel-Detectie status en verificatietabel, werkende AI Verificatie-Inspector miniatuurfoto's (`/api/v1/admin/inspector-snapshot/{log_id}`), werkende "Test Push Melding" knop (`POST /api/v1/admin/test-push`), en foutenlogboek met 5s auto-refresh.
3. **Android Client App (`android-app`):**
   - Native Jetpack Compose, Kotlin coroutines, versie 3.5.0 (`versionCode = 32`).
   - **Arlo-Stijl Opnames UI (`ClipsScreen.kt`):** Klikbare miniatuurfoto met duidelijke Play-overlay en duur-badge fungeert als afspeelknop (geen losse "Afspelen" knop). Behoudt Downloaden, Ster (favoriet/vergrendelen) en Verwijderen knoppen. Technische badges vervangen door strakke `🔔 Aangebeld` badge. Oneindig scrollen met automatische paginering van de server.
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
