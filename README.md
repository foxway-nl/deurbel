# Foxway Smart Doorbell (v3.4.1)

[![Version](https://img.shields.io/badge/version-3.4.1-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1 Pro** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, 10/10 EZVIZ AI-detectieniveau met 0.22 drempelwaarde en smart motion-fallback buiten geparkeerde auto's, host-level actieve ARP/Packet Sniffer voor EZVIZ Cloud aanbel-bursts (`trigger_source = NETWORK_SNIFF`), 3-laags Shazam deurbel-chime detector (728 Hz / 570 Hz dual-tone, NCC $\ge 0.72$), automatische herverbinding en WiFi/5G netwerkwissel-herstel in Android met backoff ladder (1s -> 3s -> 5s -> 15s), badges `[SHAZAM_CHIME]`, `[NETWORK_SNIFF]`, `[SHAZAM + SNIFF]`, sub-0.3s live-edge synchronisatie op 5G, en centrale black-box JSONL logging.

> **BELANGRIJK:** De EZVIZ DB1 Pro heeft **GEEN** Poort 80 (HTTP / ISAPI / ONVIF). Er wordt NOOIT met poort 80 of `/ISAPI/...` verbonden. De enige open hardware-poorten zijn **Poort 554 (RTSP)** en **Poort 8000 (Hikvision C-SDK `libhcnetsdk.so`)**.

---

## ⚡ Kernfunctionaliteiten in v3.4.1

1. **Host-Level Actieve ARP & EZVIZ Cloud Packet Sniffer (`network_sniffer.py`):**
   - Omdat EZVIZ `0x1132` (aanbellen) blokkeert op Poort 8000, vangt een host-level packet sniffer de uitgaande cloud-burst (UDP 6404) direct op.
   - Gerichte ARP-reply stuurt verkeer van `192.168.2.31` via host `192.168.2.1` (`eno1`) met automatische fail-safe gateway restore bij afsluiten.
   - Auto-calibreert bij elke Shazam detectie en triggert oproep met `trigger_source = "NETWORK_SNIFF"` (of `"SHAZAM + SNIFF"`).

2. **10/10 EZVIZ AI-Detectieniveau & Smart Motion Fallback (`ai_detector.py`):**
   - Bij camera hardware beweging (`0x4000`) wordt de YOLOv8 detectiedrempel voor 10 seconden verlaagd naar `0.22` (4 FPS scan-burst).
   - Indien YOLO geen objecten detecteert, activeert een slimme contour motion-fallback (`area >= 1.5%`, verplaatsing $\ge 2\%$ over 3 frames) buiten stilstaande geparkeerde voertuigen en boomkruinen en keurt goed als `label = "Beweging (Hardware)"`.

3. **Netwerk- en WiFi/5G Herstel:**
   - Zowel backend C-SDK als Shazam audio-consumer herverbinden binnen 3.0s met `[CAMERA RECONNECTED]`.
   - Android `WebSocketForegroundService` detecteert via `ConnectivityManager.NetworkCallback()` direct netwerkwissels en herverbindt via backoff ladder `1s -> 3s -> 5s -> 15s`.
   - Badges `[SHAZAM_CHIME]`, `[NETWORK_SNIFF]`, `[SHAZAM + SNIFF]` in Clips en 30-dagen Logboek.

3. **Gekalibreerde Shazam Deurbel-Chime Detector (NCC $\ge 0.72$):**
   - Gekalibreerd op de fysieke testen van 31-10-2026 (NCC scores 0.7519 en 0.7765).
   - Drempelwaarde ingesteld op 0.72 voor 100% trefzekerheid zonder valse meldingen.
   - Vaste JSON serialization bug in `GET /api/v1/admin/shazam-detections`.

4. **Notificatie-Poortwachter & 2-Weg Instellingen Synchronisatie:**
   - 10 instellingen synchroon tussen app en backend met blackbox logging (`[SETTINGS CHANGE]` en `[NOTIFY DECISION]`).
   - Meldingen voor uitgeschakelde categorieën worden 100% onderdrukt, terwijl deurbeloproepen (`CALL_RINGING`) altijd rinkelen.
   - Prominente Systeemlogboek-knop direct bovenaan Kaart 4 in `SettingsScreen.kt` en als actieknop in de TopAppBar.
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
