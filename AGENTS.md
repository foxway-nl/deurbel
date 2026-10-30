# Regels & Instructies voor Ontwikkelaars en AI-Agents (AGENTS.md)

Welkom in het Foxway Smart Doorbell project. Lees deze regels zorgvuldig voordat je code wijzigt of commando's uitvoert.

---

## 🚨 HARDE ANTI-REGRESSIE REGELS

1. **NOOIT OUDE SPECS GEBRUIKEN:**
   - De codebase bevindt zich op versie **v3.2.0** (`versionCode = 27`).
   - Oude documenten over v1.0 / v2.0 / v2.3 / v2.4 / v2.5 / v2.6 / v2.8 / v2.9 / v3.0 / v3.1.2 / v3.1.4 mogen nooit leidend zijn.

2. **BESCHERMDE KERN-COMPONENTEN & HARDWARE-FEITEN (NIET AANRAKEN):**
   - **Hardware-Feiten EZVIZ DB1 Pro (192.168.2.31):** Poort 80 (HTTP / ISAPI / ONVIF) is **NIET BESCHIKBAAR**! Verbind **NOOIT** met poort 80 of `/ISAPI/...`. De enige open poorten zijn **Poort 554 (RTSP)** en **Poort 8000 (Hikvision C-SDK `libhcnetsdk.so`)**.
   - **Hardware Drukknop-Detectie (`backend/app/sdk_alarm_listener.py`):** `NET_DVR_SetupAlarmChan_V41` op Poort 8000 vangt hardware intercom/alarm events op met persisterende callback ref (`self._callback_ref`) en logt payload bytes hex[:64]. Triggers: (1) `COMM_UPLOAD_VIDEO_INTERCOM_EVENT` (0x1132) met byte 44 == 1 (Visitor Call); (2) `COMM_ALARM_VIDEO_INTERCOM` (0x1133) met byte 44 in (1, 14); (3) `COMM_ALARM_V30` (0x4000) of `0x4007` met `dwAlarmType != 3` (Motion `dwAlarmType == 3` wordt 100% genegeerd, geen DB writes of triggers). Onbekende commando's worden gelogd in het admin paneel.
   - **Akoestische Chime Permanent Verwijderd:** Elke vorm van microfoon-/FFT-gebaseerde chime-detectie (`ring_detector.py`, 728 Hz / 580 Hz) is definitief en permanent gesloopt. Deurbeltriggers verlopen **uitsluitend** via het Poort 8000 C-SDK alarmkanaal of de fysieke Shelly Plus Uni webhook (`POST /api/v1/webhook/shelly-ring?force=true`).
   - **Tweeweg-Audio (`backend/app/audio_bridge.py`):** De Hikvision Poort 8000 C-SDK (`libhcnetsdk.so`) met pre-warmed login sessies en 1000 Hz loopback FFT test werkt foutloos. Wijzig deze logica NIET.
   - **Synchrone Harde Audio-Kill & Geen Warm Stream Retention:** Tabs in `MainActivity.kt` worden uitsluitend voorwaardelijk gerenderd via `when (selectedTab)`. Geen verborgen offscreen WebViews via translationX. `LiveScreen` start standaard gedempt (`isSpeakerMuted = true`). Bij schermwissel, pauzeren (`onPause()`, `onStop()`), sluiten van de app (`onDestroy()`) of verlaten via `DisposableEffect` wordt direct synchroon `window.killStream()`, `onPause()`, `pauseTimers()`, in `onDispose` `loadUrl("about:blank")` + `destroy()`, en `LiveStreamController.hardKillAudio()` aangeroepen. De luidspreker-knop stuurt direct `window.setAudioMuted` en `AudioManager.isSpeakerphoneOn` aan.
   - **Vaste Onwrikbare 4:3 Aspect Ratio:** De EZVIZ DB1 heeft een native 4:3 sensor. In `LiveScreen.kt`, `CallScreen.kt` en `ClipsScreen.kt` is de videoweergave gewikkeld in een geforceerde 4:3 container (`aspectRatio(4f / 3f)`).
   - **Pinch-to-Zoom & Pan:** Zowel Live Kijken, Aanbelscherm als Clips bevatten een uniforme transparante Compose gebaren-overlay met `detectTransformGestures` (schaal 1x tot 5x, pan) en `detectTapGestures` dubbeltik-reset. Live / Call stuurt `window.setVideoZoom(scale, tx, ty)` naar de webview.
   - **Zero-UI Live Player & Native Hardware Streaming (go2rtc):** Live streaming verloopt via `/api/v1/live/player` (Zero-UI `<zero-player>` met `object-fit: fill !important`, GEEN instant.jpg achtergrond/poster/polling, live camera-audio met `window.setAudioMuted()`, buffer-lag herstel op 0.8s in `ontimeupdate`, `window.killStream()`, `window.setVideoZoom()` en herverbinding uitsluitend via `ws.onclose`/`ws.onerror`).
   - **Audio Retentie Live & Opnames:** Zowel live beeld als alle video-opnames behouden altijd het originele geluid van de camera. De RAM ring-buffer (`recorder.py`) gebruikt altijd `-c:a aac` (NOOIT `-an`) en concat gebruikt `-af aresample=async=1000` om audiodrift te voorkomen. In Android kan het geluid direct worden gedempt of aangezet via de speaker-knop.
   - **Onbeperkte Deurbel Triggers:** `/api/v1/webhook/shelly-ring?force=true` omzeilt alle debounces en actieve states; ophangen in Android reset direct telecom deduplicatie.
   - **Stille Uren (23:30 - 07:30 Europe/Amsterdam):** AI-beweging muteert pushnotificaties (`silent = True`, geen FCM/push geluid), terwijl deurbeloproepen (`CALL_RINGING`) altijd rinkelen.
   - **Anti-Parked-Car Swap AI (`backend/app/ai_detector.py`):** `parked_spots` pre-filtering (`IoU >= 0.55`, `ratio < 0.30` direct afgewezen) en strikte stap-verplaatsing validatie over $\ge 3$ frames (`0.02*w <= step_disp <= 0.12*w`, `IoU >= 0.15`) voorkomen het verspringen tussen stilstaande auto's.
   - **2-Fasen Clip Lifecycle (`RECORDING` -> `READY`):** Bij detectie $T_0$ wordt EERST de thumbnail synchroon weggeschreven naar `/mnt/opslag/deurbel/clips/thumb_<clip_id>.jpg` (>5 KB), waarna de clip-rij direct met `status = "RECORDING"` in de database wordt geplaatst vóór het push-event. Pas nadat de 20s Post-Roll en concat klaar zijn (`.mp4 > 100 KB`, `duration >= 5s`), wordt de clip bijgewerkt naar `status = "READY"`.
   - **Push Notificatie Miniatuurfoto's:** Notificaties bevatten direct `.setLargeIcon(bitmap)` én `.setStyle(BigPictureStyle().bigPicture(bitmap))` via `$activeBaseUrl/api/v1/clips/<clip_id>/thumbnail` met 500ms fallback retry.
   - **RAM Ring-Buffer & Sourcing:** `/dev/shm/deurbel_ring` schrijft chronologische 2-seconden Unix-timestamp segmenten (`seg_%s.ts`) met automatische gap-detectie en 20s pre-roll / 20s post-roll.
   - **Gecentraliseerde SQLite WAL (`backend/app/database.py`):** Alle database-operaties verlopen via `get_db()` met WAL mode, busy timeout 60s en write-lock bescherming.
   - **Netwerk & Routing:** Server draait lokaal op `http://192.168.2.1:8090` en extern via Cloudflare op `https://deurbel.foxway.nl`. Bij mobiele verbinding (4G/5G zónder WiFi) wordt route direct vergrendeld op WAN en wordt LAN probing overgeslagen om ping-pong te voorkomen.

3. **SYSTEEMBRONNEN & CGROUP REGELS:**
   - Draai nooit zware parallelle processen op de host.
   - Gebruik voor `ffmpeg` altijd `nice -n 15`.
   - Bouw Android altijd met `--max-workers=2`.

---

## 🛠️ STANDAARD COMMANDO'S

### Python Tests Uitvoeren
Python packages draaien in de Docker backend container. Draai tests altijd via:
```bash
docker exec foxway-doorbell-backend pytest -v /mnt/opslag/deurbel/backend/tests/
```

### Android APK Compileren
Java en de Android SDK zijn geïnstalleerd in de container `thyrlian/android-sdk:latest`. Bouw altijd via:
```bash
docker run --rm -v /mnt/opslag/deurbel:/mnt/opslag/deurbel -w /mnt/opslag/deurbel/android-app thyrlian/android-sdk:latest ./gradlew assembleRelease --max-workers=2
```
Kopieer na succes de APK naar `/mnt/opslag/deurbel/clips/foxway-deurbel.apk`.

### Git Workflow
Commits moeten duidelijke conventionele commitberichten hebben (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
Push altijd naar de `main` branch van `git@github.com:foxway-nl/deurbel.git`.
