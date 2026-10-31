# Regels & Instructies voor Ontwikkelaars en AI-Agents (AGENTS.md)

Welkom in het Foxway Smart Doorbell project. Lees deze regels zorgvuldig voordat je code wijzigt of commando's uitvoert.

---

## 🚨 HARDE ANTI-REGRESSIE REGELS

1. **NOOIT OUDE SPECS GEBRUIKEN:**
   - De codebase bevindt zich op versie **v3.4.0** (`versionCode = 30`).
   - Oude documenten over v1.0 / v2.0 / v2.3 / v2.4 / v2.5 / v2.6 / v2.8 / v2.9 / v3.0 / v3.1.2 / v3.1.4 / v3.2.0 / v3.3.0 / v3.3.1 mogen nooit leidend zijn.

2. **STRIKTE COMMANDO-TIMEOUT (NOOIT MEER LANG WACHTEN):**
   - Draai `pytest`, Gradle builds of Docker-taken **NOOIT** zonder harde timeout! Gebruik altijd `timeout 90s pytest ...` en `timeout 300s ./gradlew ...`.
   - Als een test of container langer dan 90 seconden hangt, kap je het proces direct af, los je de blokkade op en ga je door.
   - Test **incrementeel per aangepast onderdeel** (`pytest tests/test_specifiek.py`), NIET blind alles in één hangende sessie!

3. **ZERO-REGRESSION WET ("NIKS KAPOTMAKEN WAT AL WERKT"):**
   - De **AI-detectie van Mensen, Voertuigen én Dieren** werkt uitstekend! **RAAK DE AI-DETECTIE DREMPELS EN LOGICA IN `ai_detector.py` NIET AAN!**
   - De **Aanbel-detectie in de backend** werkt uitstekend! **MAAK DE AANBEL-TRIGGERS IN DE BACKEND NIET KAPOT!**
   - Wanneer je één bug oplost, controleer je direct of omliggende functionaliteit nog heel is.

4. **BESCHERMDE KERN-COMPONENTEN & HARDWARE-FEITEN (NIET AANRAKEN):**
   - **Hardware-Feiten EZVIZ DB1 Pro (192.168.2.31):** Poort 80 (HTTP / ISAPI / ONVIF) is **NIET BESCHIKBAAR**! Verbind **NOOIT** met poort 80 of `/ISAPI/...`. De enige open poorten zijn **Poort 554 (RTSP)** en **Poort 8000 (Hikvision C-SDK `libhcnetsdk.so`)**.
   - **Hardware Drukknop-Detectie & Dual Ingest (`backend/app/sdk_alarm_listener.py`):** `NET_DVR_SetupAlarmChan_V41` op Poort 8000 vangt hardware intercom/alarm events op met persisterende callback ref en exacte 28-byte `NET_DVR_SETUPALARM_PARAM` struct in dual-mode (`byDeployType=0` Intercom mode en `byDeployType=1` Server mode) plus passieve TCP listen op Poort 7200 (`NET_DVR_StartListen_V30`). Triggers: (1) `COMM_UPLOAD_VIDEO_INTERCOM_EVENT` (0x1132) met byte 44 in (1, 0); (2) `COMM_ALARM_VIDEO_INTERCOM` (0x1133) met byte 44 in (1, 14); (3) `COMM_ALARM_V30` (0x4000) met `dwAlarmType == 0` (Motion `dwAlarmType == 3` triggert High-Speed AI Scan-Burst 4 FPS / 0.25s via `ai_detector.notify_hardware_motion()`); (4) `COMM_ISAPI_ALARM` (0x5002 / 0x6009) met 'ring' / 'call'. Alle pakketten worden gelogd in SQLite `sdk_alarm_logs` en native file logging `logs/hcnetsdk_native/`.
   - **Passieve Deurbel Netwerk & SDK Analyser (`backend/app/sdk_alarm_listener.py`):** Bij deurbelactiviteit (bijv. `SHAZAM_CHIME` of bellen) inspecteert de listener passief `/proc/net/tcp` naar sockets van/naar `192.168.2.31` en bevraagt `NET_DVR_GetDVRWorkState_V30`. Schrijft gestructureerde diagnostiek weg naar dagelijkse zwarte-doos JSONL onder `DEURBEL_STATUS_MONITOR`.
   - **Real-Time 3-Layer Shazam Deurbel-Chime Detector (`backend/app/ring_detector.py`):** Getraind op echte deurbel-opnames van de fysieke deurbel (728 Hz / 570 Hz dual-tone). Laag 1: spectrale whitening; Laag 2: kwarts-klok toonstabiliteit binnen +-15 Hz; Laag 3: 2D Normalized Cross-Correlation (NCC $\ge 0.82$, 12s cooldown). Logt alle evaluaties $\ge 0.45$ NCC in daily black-box JSONL.
   - **Tweeweg-Audio (`backend/app/audio_bridge.py`):** De Hikvision Poort 8000 C-SDK (`libhcnetsdk.so`) met pre-warmed login sessies en 1000 Hz loopback FFT test werkt foutloos. Wijzig deze logica NIET.
   - **Synchrone Harde Audio-Kill & Geen Warm Stream Retention:** Tabs in `MainActivity.kt` worden uitsluitend voorwaardelijk gerenderd via `when (selectedTab)`. Geen verborgen offscreen WebViews via translationX. `LiveScreen` start standaard gedempt (`isSpeakerMuted = true`). Bij schermwissel, pauzeren (`onPause()`, `onStop()`), sluiten van de app (`onDestroy()`) of verlaten via `DisposableEffect` wordt direct synchroon `window.killStream()`, `onPause()`, `pauseTimers()`, in `onDispose` `loadUrl("about:blank")` + `destroy()`, en `LiveStreamController.hardKillAudio()` aangeroepen. De luidspreker-knop in `LiveScreen.kt` is een directe 2-standen schakelaar (Rood = gedempt, Groen = geluid aan, GEEN grijze tussenstand).
   - **Vaste Onwrikbare 4:3 Aspect Ratio:** De EZVIZ DB1 heeft een native 4:3 sensor. In `LiveScreen.kt`, `CallScreen.kt` en `ClipsScreen.kt` is de videoweergave gewikkeld in een geforceerde 4:3 container (`aspectRatio(4f / 3f)`).
   - **Pinch-to-Zoom & Pan:** Zowel Live Kijken, Aanbelscherm als Clips bevatten een uniforme transparante Compose gebaren-overlay met `detectTransformGestures` (schaal 1x tot 5x, pan) en `detectTapGestures` dubbeltik-reset. Live / Call stuurt `window.setVideoZoom(scale, tx, ty)` naar de webview.
   - **Zero-UI Live Player & Native Hardware Streaming (go2rtc):** Live streaming verloopt via `/api/v1/live/player` (Zero-UI `<zero-player>` met `object-fit: fill !important`, instant-poster `<img id="instant-poster">` op ms 0 direct verwijderd zodra eerste video-frame arriveert, live-edge lag synchronisatie `<0.3s` via 250ms interval, `window.setAudioMuted()`, `window.killStream()`, `window.setVideoZoom()` en herverbinding uitsluitend via `ws.onclose`/`ws.onerror`).
   - **Audio Retentie Live & Opnames:** Zowel live beeld als alle video-opnames behouden altijd het originele geluid van de camera. De RAM ring-buffer (`recorder.py`) gebruikt altijd `-c:a aac` (NOOIT `-an`) en concat gebruikt `-af aresample=async=1000` om audiodrift te voorkomen. In Android kan het geluid direct worden gedempt of aangezet via de speaker-knop.
   - **Melding-Poortwachter & Stille Uren:** Als categorie `Melding` UIT staat of tijdens stille uren (23:30 - 07:30 Europe/Amsterdam), wordt `silent = True` gezet. Zowel server-side (FCM) als client-side (`WebSocketForegroundService` via `SharedPreferences`) onderdrukken notificaties 100%. Deurbeloproepen (`CALL_RINGING`) rinkelen ALTIJD.
   - **100% EZVIZ AI-Detectie & Stationary Parked-Car Geheugen (`backend/app/ai_detector.py`):** Dynamisch 15-seconden geheugen voor geparkeerde voertuigen (`IoU >= 0.72`, centroid shift `< 0.035*w`) filtert stilstaande auto's betrouwbaar uit zonder false positives. Rijdende auto's (`delta_d >= 0.045*w` over 2 frames / 0.5s) en wegrijdende auto's (`delta_d >= 0.05*w`) worden direct goedgekeurd. Voetgangers (`conf >= 0.35`, displacement $\ge 1.5\%$ of `conf >= 0.50`) en dieren (`conf >= 0.32`) triggeren direct. Per-categorie cooldown (5s zelfde categorie, 0s verschillende categorieën) voorkomt dat gebeurtenissen elkaar blokkeren. Camera hardware beweging (`0x4000 dwAlarmType == 3`) activeert 4 FPS High-Speed Scan-Burst.
   - **2-Fasen Clip Lifecycle (`RECORDING` -> `READY`):** Bij detectie $T_0$ wordt EERST de thumbnail synchroon weggeschreven naar `/mnt/opslag/deurbel/clips/thumb_<clip_id>.jpg` (>5 KB), waarna de clip-rij direct met `status = "RECORDING"` in de database wordt geplaatst vóór het push-event. Pas nadat de 20s Post-Roll en concat klaar zijn (`.mp4 > 100 KB`, `duration >= 5s`), wordt de clip bijgewerkt naar `status = "READY"`.
   - **Push Notificatie Miniatuurfoto's:** Notificaties bevatten direct `.setLargeIcon(bitmap)` én `.setStyle(BigPictureStyle().bigPicture(bitmap))` via `$activeBaseUrl/api/v1/clips/<clip_id>/thumbnail` met 500ms fallback retry.
   - **RAM Ring-Buffer & Sourcing:** `/dev/shm/deurbel_ring` schrijft chronologische 2-seconden Unix-timestamp segmenten (`seg_%s.ts`) met automatische gap-detectie en 20s pre-roll / 20s post-roll.
   - **Gecentraliseerde SQLite WAL (`backend/app/database.py`):** Alle database-operaties verlopen via `get_db()` met WAL mode, busy timeout 60s en write-lock bescherming.
   - **Centrale Black-Box Logging & Android Triage:** Alle componenten schrijven naar chronologische dagelijkse JSONL `/mnt/opslag/deurbel/logs/YYYY-MM-DD.jsonl`. Android bevat een `"📤 Rapporteer Fout / Status-Dump"` knop via `POST /api/v1/logs/client`. Triage verloopt via `/mnt/opslag/deurbel/scripts/triage.py`.

3. **SYSTEEMBRONNEN & CGROUP REGELS:**
   - Draai nooit zware parallelle processen op de host.
   - Gebruik voor `ffmpeg` altijd `nice -n 15`.
   - Bouw Android altijd met `--max-workers=2`.

---

## 🛠️ STANDAARD COMMANDO'S

### Python Tests Uitvoeren
Python packages draaien in de Docker backend container. Draai tests altijd met timeout via:
```bash
docker exec foxway-doorbell-backend timeout 90s pytest -v /mnt/opslag/deurbel/backend/tests/
```

### Android APK Compileren
Java en de Android SDK zijn geïnstalleerd in de container `thyrlian/android-sdk:latest`. Bouw altijd met timeout via:
```bash
docker run --rm -v /mnt/opslag/deurbel:/mnt/opslag/deurbel -w /mnt/opslag/deurbel/android-app thyrlian/android-sdk:latest timeout 300s ./gradlew assembleRelease --max-workers=2
```
Kopieer na succes de APK naar `/mnt/opslag/deurbel/clips/foxway-deurbel.apk`.

### Git Workflow
Commits moeten duidelijke conventionele commitberichten hebben (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
Push altijd naar de `main` branch van `git@github.com:foxway-nl/deurbel.git`.
