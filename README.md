# Foxway Smart Doorbell (v2.9.0)

[![Version](https://img.shields.io/badge/version-2.9.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, Zero-UI live speler met milliseconde 0 snapshot-poster, onbeperkte opeenvolgende deurbeltriggers, stille uren (23:30 - 07:30 Europe/Amsterdam), anti-parked-car swap AI-bescherming, 2-fasen clip lifecycle (`RECORDING` -> `READY`), directe push-notificatie miniatuurfoto's, native go2rtc hardware-streaming (MSE + WebRTC) over LAN en 5G/Cloudflare WSS (0ms lag, 25 FPS), SQLite WAL database concurrency, en het snelle YOLOv8n Temporal Pixel-Diff Masking AI-detectie model (5 FPS, <45ms).

---

## ⚡ Kernfunctionaliteiten in v2.9.0

1. **Zero-UI Live Player & Geen Vertraging (`/api/v1/live/player`, `LiveScreen.kt`, `CallScreen.kt`):**
   - Volledig controle-vrije speler gebaseerd op `<zero-player>` en `/api/go2rtc/video-rtc.js`.
   - Toont op milliseconde 0 direct de meest recente snapshot (`/api/v1/live/instant.jpg`) als achtergrond/poster, zodat gebruikers nooit een zwart laadscherm zien.
   - Verbergt alle go2rtc badges ("MSE", status-overlays) via CSS en forceert `video.controls = false` via MutationObserver.
   - Bevat een 1500ms freeze watchdog die de verbinding direct herstelt bij stilstand (>2.5s).
   - In Android retourneert `WebChromeClient.getDefaultVideoPoster()` een 1x1 transparante bitmap (0% grijze play-knop).
   - Knop `"🔄 Sync Live"` roept `window.reconnectStream()` aan en herlaadt de speler.

2. **Onbeperkte Opeenvolgende Deurbel Triggers (`main.py`, `DoorbellConnectionService.kt`, `SettingsScreen.kt`):**
   - `POST /api/v1/webhook/shelly-ring?force=true` omzeilt alle debounces en reset actieve oproep-states direct.
   - Elk verzoek genereert direct een nieuwe unieke `call_id` en start een verse ring-notificatie.
   - Bij het ophangen (`handleHangup()`) in Android wordt de deduplicatie onmiddellijk gereset, waardoor opeenvolgend testen zonder pauzes mogelijk is.

3. **Stille Uren / Nachtmodus (23:30 - 07:30 Europe/Amsterdam):**
   - Bewegingsdetectie clips worden 's nachts stil opgenomen (`silent = True`, geen push/FCM geluid).
   - Deurbeloproepen (`CALL_RINGING`) rinkelen altijd en worden nooit onderdrukt.
   - Volledig dynamisch configureerbaar via `/admin` (Tab 2) en de Android app (`SettingsScreen.kt`).

4. **Anti-Car Swap AI-Filter (`ai_detector.py`):**
   - `parked_spots` pre-filtering: detecties die overlappen met een bekende geparkeerde plek (`IoU >= 0.55`) met `changed_pixel_ratio < 0.30` worden direct afgewezen.
   - Stap-verplaatsingscontrole: tussen twee 5 FPS frames (0.2s) mag een rijdend voertuig maximaal `0.12 * breedte` verplaatsen en moet opeenvolgend `IoU >= 0.15` overlap hebben. Dit sluit verspringen tussen nabijgelegen geparkeerde auto's (bijv. cx=0.42 naar cx=0.62) categorisch uit.

5. **ExoPlayer Dismiss & Schone Clip Sluiting (`ClipsScreen.kt`):**
   - Bij het aantikken van het kruisje (`X`) wordt ExoPlayer onmiddellijk gestopt en vrijgegeven (`stop(); release()`), en worden deeplink parameters direct gewist zodat afspeelbalken niet over de lijst blijven hangen.

6. **Cellular-Only WAN Lock (`ConnectionManager.kt` & `EndpointResolver.kt`):**
   - Wanneer de smartphone op mobiele data / 5G zit (zonder actieve Wi-Fi), wordt de route vergrendeld op WAN (`https://deurbel.foxway.nl`) en worden LAN health probes overgeslagen, wat ping-pong en timeouts voorkomt.

7. **Android Client App v2.9.0 (`versionCode = 20`):**
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
