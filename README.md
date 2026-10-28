# Foxway Smart Doorbell (v2.5.0)

[![Version](https://img.shields.io/badge/version-2.5.0-emerald.svg)](https://github.com/foxway-nl/deurbel)
[![Platform](https://img.shields.io/badge/platform-Android%20%7C%20FastAPI%20%7C%20Docker-blue.svg)](https://github.com/foxway-nl/deurbel)
[![Aspect Ratio](https://img.shields.io/badge/aspect_ratio-4%3A3_native-orange.svg)](https://github.com/foxway-nl/deurbel)

Productie-oplossing voor de **EZVIZ DB1** deurbel met native 4:3 beeldsensor, realtime tweeweg-audio over Hikvision Poort 8000 C-SDK, SQLite WAL database concurrency, freeze-watchdog met camera-klok pixel-motion verificatie, chronologische Unix-timestamp opnames (`seg_%s.ts`) met gap-detectie (anti-time-jump), Arlo-stijl 3-laags AI-verificatie met voertuig bounding box verplaatsingsregel en persoon-prioriteit, een web-based beheerportaal (`/admin`), en een geoptimaliseerde Android-app met 4 overzichtelijke instellingen-kaarten en betrouwbare Telecom oproepen.

---

## ⚡ Kernfunctionaliteiten in v2.5.0

1. **Anti-Freeze Live Watchdog & Echte Sync Live (`live_hub.py`):**
   - Automatische pixel-motion verificatie op de camera-klok over 3 seconden. Bij stilstand (`pixel_diff < 0.15`) herstart de pipeline zelfherstellend.
   - Drukken op "Sync Live" herstart go2rtc en FFmpeg direct en levert pas status terug zodra een geverifieerd bewegend frame binnenkomt.
   - Directe RTSP fallback bij eventuele go2rtc complicaties.

2. **Chronologische Unix-Timestamp Opnames & Anti-Time-Jump (`recorder.py`):**
   - RAM ring-buffer slaat chronologische bestanden op (`seg_%s.ts`) met automatische achtergrondopruiming.
   - Gap-detectie (>4.0s) prunet oude segmenten vóór een gap, wat terugspringen in de tijd definitief elimineert.
   - Single-recording lock en strikte validatie (omvang > 100 KB en duur $\ge 5.0$s) vóór database-opslag, waardoor $00:00$ spook-clips worden uitgesloten.

3. **Voertuig-Verplaatsingsregel & Persoon-Prioriteit (`ai_detector.py`):**
   - Vereist $\Delta x_{\text{left}} \ge 0.12$ én $\Delta x_{\text{right}} \ge 0.12$ voor rijdende voertuigen; geparkeerde voertuigen ($\Delta x < 0.03$) worden direct genegeerd.
   - Wanneer een persoon in beeld is of recent (<20s) is gezien, krijgt deze absolute prioriteit en worden voertuigtriggers onderdrukt.
   - Schone snapshot voor app en debug snapshot met bounding boxen voor Web Admin.

4. **Telecom & Oproepscherm Verbeteringen (`CallScreen.kt`, `CallActivity.kt`, `DoorbellConnectionService.kt`):**
   - Oproep-deduplicatie met 10s cooldown en foreground activiteitscheck.
   - Grote 72.dp opneem/weiger actieknoppen met directe visuele feedback en muting op de UI-thread.

5. **Strakke 4-Kaarten Instellingen (`SettingsScreen.kt`):**
   - Kaart 1: Profiel & Gekoppelde Apparaten (met realtime online status).
   - Kaart 2: AI-Detectie & Meldingen (compacte gecombineerde toggles).
   - Kaart 3: Opslag & Bewaarduur (voortgangsbalk & retentieknoppen).
   - Kaart 4: Systeem & Diagnose (hardware test, webhook test, directe link naar `/admin`, ontkoppelen).

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
