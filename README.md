# Foxway Smart Doorbell (v3.3.0)

[![Version](https://img.shields.io/badge/version-3.3.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1 Pro** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, hardware drukknop-detectie via Poort 8000 C-SDK met dual ingest (`byDeployType=0` & `1`), passieve TCP listen op Poort 7200, 100% raw packet sniffer en byte-diffing, een 3-laags Shazam akoestische deurbel-chime detector getraind op echte deurbel-opnames (728 Hz / 570 Hz dual-tone), geëlimineerd 4s zwart scherm & sub-0.3s live-edge synchronisatie op 5G, strikte notificatie-poortwachter (onderdrukt pushnotificaties wanneer een categorie UIT staat via SharedPreferences synchronisatie), directe 2-standen luidsprekerknop (Rood/Groen), centrale black-box JSONL logging met Android status-dump reporter (`POST /api/v1/logs/client`) en diagnostics triage script (`scripts/triage.py`).

> **BELANGRIJK:** De EZVIZ DB1 Pro heeft **GEEN** Poort 80 (HTTP / ISAPI / ONVIF). Er wordt NOOIT met poort 80 of `/ISAPI/...` verbonden. De enige open hardware-poorten zijn **Poort 554 (RTSP)** en **Poort 8000 (Hikvision C-SDK `libhcnetsdk.so`)**.

---

## ⚡ Kernfunctionaliteiten in v3.3.0

1. **Poort 8000 C-SDK Dual Ingest & Full Packet Sniffer (`sdk_alarm_listener.py`):**
   - Exacte 28-byte `NET_DVR_SETUPALARM_PARAM` structuur met dual alarmkanaal ingest: `byDeployType = 0` (Client/Intercom modus) en `byDeployType = 1` (Server modus).
   - Passieve TCP Listen-Mode via `NET_DVR_StartListen_V30` op Poort 7200 voor ongevraagde camera-oproepen.
   - Native C-SDK file logger geactiveerd via `NET_DVR_SetLogToFile` naar `/mnt/opslag/deurbel/logs/hcnetsdk_native/`.
   - 100% Raw buffer packet sniffer: vangt 100% van de payload op, voert byte-diffing uit op bewegingspakketten (`0x4000`), en logt alle events naar SQLite `sdk_alarm_logs` en dagelijkse black-box JSONL.
   - Triggers: `0x1132` (byte 44 in 1, 0), `0x1133` (byte 44 in 1, 14), `0x4000` (`dwAlarmType == 0`), en ISAPI ring events (`0x5002` / `0x6009`).

2. **Real-Time 3-Layer Shazam Deurbel-Chime Detector (`ring_detector.py`):**
   - Getraind op authentieke audio-opnames van de fysieke deurbel van vandaag.
   - Dual-tone akoestische signatuur: Toon 1 = ~728 Hz (250ms), Toon 2 = ~570 Hz (250ms) over een 0.85s tijdvenster.
   - 3-Laags filter: Laag 1 spectrale whitening, Laag 2 kwarts-klok toonstabiliteit (+-15 Hz, >=12 dB prominentie), Laag 3 2D Normalized Cross-Correlation (NCC >= 0.82) met 12s cooldown.
   - Vingerafdruk opgeslagen in `backend/app/assets/chime_fingerprint.npz`. NCC >= 0.45 evaluaties worden gelogd in daily JSONL.

3. **5G Live-Edge Streaming & Instant Poster (`main.py`, `LiveScreen.kt`):**
   - Instant 4:3 poster direct gerenderd op ms 0 via `<img id="instant-poster">`, permanent verwijderd zodra het eerste video-frame rendert.
   - Live-edge lag synchronisatie (`syncToLiveEdge`) forceert `<0.3s` vertraging op metadata, data, canplay, playing en via een 250ms timer (`end - currentTime > 0.4s -> currentTime = end - 0.12s`).
   - MSE buffer trimt segmenten ouder dan 2 seconden om geheugen en bufferophoping te voorkomen.

4. **Notificatie-Poortwachter & Directe Mute-Knop (`ai_detector.py`, `WebSocketForegroundService.kt`, `LiveScreen.kt`):**
   - Wanneer een categorie `Melding` op UIT staat, zet de backend `silent = True` en `should_notify = False`, waardoor FCM wordt onderdrukt.
   - Android slaat instellingen direct op in `SharedPreferences("foxway_settings")` en `WebSocketForegroundService` verifieert `item.silent` en lokale categorie-instellingen vóór het tonen van notificaties.
   - Directe 2-standen luidsprekerknop in `LiveScreen.kt`: Rood (Gedempt) of Groen (Geluid Aan), zonder grijze tussenstand.
   - Aangescherpt geparkeerde-auto filter: vereist $\ge 4$ opeenvolgende frames in dezelfde richting met $\Delta x \ge 18\%$ en `changed_pixel_ratio >= 0.35`.

5. **Centrale Black-Box Logging & Diagnostics Triage (`logger.py`, `scripts/triage.py`):**
   - Gestructureerde dagelijkse logging naar `/mnt/opslag/deurbel/logs/YYYY-MM-DD.jsonl`.
   - Ingebouwde knop `"📤 Rapporteer Fout / Status-Dump"` in de Android app stuurt toestelgegevens, schermstatus en app-logs direct door naar `POST /api/v1/logs/client`.
   - Diagnostisch triage-script `/mnt/opslag/deurbel/scripts/triage.py` geeft direct inzicht in alle C-SDK packets, Shazam scores, AI-detecties en client-status dumps.
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
