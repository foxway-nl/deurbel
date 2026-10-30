# Foxway Smart Doorbell (v3.2.0)

[![Version](https://img.shields.io/badge/version-3.2.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1 Pro** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, synchrone hardware drukknop-detectie via Poort 8000 C-SDK (`NET_DVR_SetupAlarmChan_V41`), strikte eliminatie van spook-alarmen (`COMM_ALARM_V30` 0x4000 met motion `dwAlarmType == 3` 100% genegeerd, 0x1132 byte 44 == 1 en 0x1133 byte 44 in (1, 14) triggeren bel-actie, niet-motion 0x4000 triggert bel-actie), volledige uitfasering van akoestische chime-detectie (puur hardware-only), synchrone audio en stream hard-kill bij verlaten van schermen (geen verborgen background WebViews), standaard gedempt live geluid, pinch-to-zoom en pan gebaren (1x-5x), Zero-UI live speler zonder enige instant.jpg polling, onbeperkte opeenvolgende deurbeltriggers, stille uren (23:30 - 07:30 Europe/Amsterdam), anti-parked-car swap AI-bescherming, 2-fasen clip lifecycle (`RECORDING` -> `READY`), directe push-notificatie miniatuurfoto's, native go2rtc hardware-streaming (MSE + WebRTC) over LAN en 5G/Cloudflare WSS (0ms lag, 25 FPS), SQLite WAL database concurrency, en het snelle YOLOv8n Temporal Pixel-Diff Masking AI-detectie model (5 FPS, <45ms).

> **BELANGRIJK:** De EZVIZ DB1 Pro heeft **GEEN** Poort 80 (HTTP / ISAPI / ONVIF). Er wordt NOOIT met poort 80 of `/ISAPI/...` verbonden. De enige open hardware-poorten zijn **Poort 554 (RTSP)** en **Poort 8000 (Hikvision C-SDK `libhcnetsdk.so`)**.

---

## ⚡ Kernfunctionaliteiten in v3.2.0

1. **Poort 8000 C-SDK Hardware Drukknop-Detectie (`sdk_alarm_listener.py`):**
   - Registreert een C-callback (`NET_DVR_SetDVRMessageCallBack_V31`) met persistente referentie (`self._callback_ref` tegen GC) en opent het alarmkanaal (`NET_DVR_SetupAlarmChan_V41`) over Poort 8000.
   - Vangt hardware events op en filtert strikt:
     - `COMM_UPLOAD_VIDEO_INTERCOM_EVENT` (0x1132) met byte 44 == 1 (Visitor Call / Doorbell Button) triggert direct een deurbel-ring.
     - `COMM_ALARM_VIDEO_INTERCOM` (0x1133) met byte 44 in (1, 14) triggert direct een deurbel-ring.
     - `COMM_ALARM_V30` (0x4000) en `0x4007`: `dwAlarmType == 3` (Motion) wordt 100% genegeerd en niet opgeslagen; elk ander `dwAlarmType` (bijv. 0) triggert direct een ring.
   - Logt élke byte van binnenkomende commando's en eerste 64 bytes hex payload in real-time in logs en `/admin`.
   - Secundaire fysieke Shelly Plus Uni webhook (`/api/v1/webhook/shelly-ring?force=true`) blijft 100% actief.
   - Akoestische chime-detectie is volledig en definitief uit de codebase gesloopt: 100% hardware betrouwbaarheid.

2. **Synchrone Hard-Kill voor Audio & Streams, Geen Achtergrond Retention (`MainActivity.kt`, `LiveScreen.kt`, `CallScreen.kt`, `ClipsScreen.kt`):**
   - Geen verborgen offscreen WebViews meer via translationX in `MainActivity.kt`. Schermen worden uitsluitend voorwaardelijk gerenderd via `when (selectedTab)`.
   - `LiveScreen` start standaard gedempt (`isSpeakerMuted = true`), speaker toggle stuurt zowel `window.setAudioMuted` als `AudioManager.isSpeakerphoneOn` aan.
   - Bij verlaten van een scherm (`ON_PAUSE`, `ON_STOP`, `onDispose`) of ophangen worden streams en audio onmiddellijk synchroon hard beëindigd (`window.killStream()`, `onPause()`, `pauseTimers()`, in `onDispose` `loadUrl("about:blank")` + `destroy()`, en `hardKillAudio()`).

3. **Pinch-to-Zoom & Pan Gebaren-Overlay (Live, Call, Clips):**
   - Uniforme Compose gebaren-overlay met `detectTransformGestures` (schaal 1x tot 5x, pan) en `detectTapGestures` dubbeltik-reset over alle videoschermen.
   - Live & Call sturen synchroon `window.setVideoZoom(scale, tx, ty)` naar de Zero-UI webview. Clips stuurt de transformatie naar ExoPlayer Surface.

4. **Zero-UI Live Speler Zonder Instant.jpg Polling (`/api/v1/live/player`, `LiveStreamController.kt`):**
   - Volledig controle-vrije speler gebaseerd op `<zero-player>` en `/api/go2rtc/video-rtc.js`.
   - Bevat **0 referenties** naar `instant.jpg` (geen achtergrond, geen interval poller, geen fallback watchers).
   - `object-fit: fill !important`, automatische buffer-lag herstel op 0.8s via `ontimeupdate`, en herverbinding uitsluitend op `ws.onclose`/`onerror`.

4. **Onbeperkte Opeenvolgende Deurbel Triggers (`main.py`, `DoorbellConnectionService.kt`, `SettingsScreen.kt`):**
   - `POST /api/v1/webhook/shelly-ring?force=true` omzeilt alle debounces en reset actieve oproep-states direct.
   - Knop "Test Aanbel Trigger" in `SettingsScreen.kt` opent telkens direct het belscherm.
   - Bij het ophangen (`handleHangup()`) in Android wordt de telecom deduplicatie onmiddellijk gereset, waardoor opeenvolgend testen zonder pauzes mogelijk is.

5. **Stille Uren / Nachtmodus (23:30 - 07:30 Europe/Amsterdam):**
   - Bewegingsdetectie clips worden 's nachts stil opgenomen (`silent = True`, geen push/FCM geluid).
   - Deurbeloproepen (`CALL_RINGING`) rinkelen altijd en worden nooit onderdrukt.
   - Volledig dynamisch configureerbaar via `/admin` (Tab 2) en de Android app (`SettingsScreen.kt`).

6. **Anti-Car Swap AI-Filter (`ai_detector.py`):**
   - `parked_spots` pre-filtering: detecties die overlappen met een bekende geparkeerde plek (`IoU >= 0.55`) met `changed_pixel_ratio < 0.30` worden direct afgewezen.
   - Stap-verplaatsingscontrole: over $\ge 3$ opeenvolgende frames (0.2s per stap) moet een rijdend voertuig `0.02 * breedte <= step_dx <= 0.12 * breedte` verplaatsen met `IoU >= 0.15` overlap per tussenstap, tot een totale verplaatsing $\ge 14\%$ van het scherm. Dit sluit verspringen tussen nabijgelegen geparkeerde auto's (bijv. cx=0.42 naar cx=0.62) categorisch uit.

7. **ExoPlayer Dismiss & Schone Clip Sluiting (`ClipsScreen.kt`):**
   - Bij het aantikken van het kruisje (`X`) wordt ExoPlayer onmiddellijk gestopt en vrijgegeven (`stop(); release()`), en worden deeplink parameters direct gewist zodat afspeelbalken niet over de lijst blijven hangen.

8. **Cellular-Only WAN Lock (`ConnectionManager.kt` & `EndpointResolver.kt`):**
   - Wanneer de smartphone op mobiele data / 5G zit (zonder actieve Wi-Fi), wordt de route direct vergrendeld op WAN (`https://deurbel.foxway.nl`) en worden LAN health probes overgeslagen, wat ping-pong en timeouts voorkomt.

9. **Android Client App v3.0.0 (`versionCode = 21`):**
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
