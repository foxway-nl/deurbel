# Foxway Deurbel — Technische Handleiding (TECHNICAL_GUIDE.md)

Deze technische handleiding biedt een diepgaande beschrijving van de interne werking van de FastAPI backend en de Android Jetpack Compose applicatie van het Foxway Deurbel ecosysteem.

---

## 1. Backend Architectuur & Modules

De backend is gebouwd met **Python 3.11 en FastAPI**, geoptimaliseerd voor asynchrone I/O en lage CPU-footprint. De applicatie draait in een Docker-container (`foxway-doorbell-backend`) met `network_mode: host` voor directe socket- en netwerkcommunicatie met de hardware.

### 1.1 Overzicht van de modules (`/app/`)

| Bestand | Verantwoordelijkheid |
| :--- | :--- |
| `main.py` | FastAPI server, API-routes, WebSocket multiplexing, go2rtc media proxies, lifecycle hooks, security dependency. |
| `models.py` | Pydantic v2 schemas voor request/response validatie, SQLite datamodellen en enums. |
| `recorder.py` | Opname-engine voor oproepen en handmatige opnames, low-priority achtergrondcompressie (`nice -n 19`), storage monitoring en dual-tier retentieopruiming. |
| `ai_detector.py` | Tweetraps AI-detectie: OpenCV frame differencing op lage resolutie substream + YOLOv8n ONNX runtime classificatie. |
| `audio_bridge.py` | Port 8000 HCNetSDK integratie via `ctypes`, live audio transcoding naar AAC-LC 16kHz ADTS voor de deurbel speaker. |

---

### 1.2 Module Details

#### A. `main.py` — Routering & Media Proxies
- **Stream Proxies voor 5G/WAN:**
  - `GET /api/v1/stream.mp4`: Proxy voor gefragmenteerde MP4 (`fMP4`) video- en audiostreams vanaf go2rtc (`http://127.0.0.1:1984/api/stream.mp4`). Ondersteunt `src=deurbel` (HD) en `src=deurbel_sub` (SD).
  - `GET /api/v1/frame.jpeg`: Haalt een enkel JPEG keyframe op met caching-headers (`no-cache`) voor ultra-snelle snapshots en preview-tegels.
  - `@app.websocket("/api/ws")` & `@app.websocket("/api/v1/webrtc/ws")`: Volledig asynchrone WebSocket reverse proxy die WebRTC SDP-onderhandelingen doorstuurt tussen mobiele clients op WAN en de lokale go2rtc daemon zonder directe poortblootstelling.
- **Microfoon Streaming Endpoint:**
  - `@app.websocket("/api/v1/audio/stream")`: Ontvangt binaire PCM audiostreams (16kHz 16-bit mono) vanaf de Android-microfoon, transcodeert deze on-the-fly via een persistent FFmpeg-proces naar AAC-LC ADTS en stuurt ze naar de Hikvision SDK.
- **Deurbel Gezondheidscontrole:**
  - Achtergrondtaak controleert elke 30 seconden de TCP-verbinding naar `192.168.2.31:8000` en `192.168.2.31:554`.
  - Als de camera > 120 seconden onbereikbaar is, wordt een `DOORBELL_STATUS`-bericht met `online=false` via WebSocket gebroadcast.

#### B. `recorder.py` — Opnames, Compressie & Retentie
- **Handmatige Opname:**
  - `start_manual_recording(user_name)`: Start een asynchroon FFmpeg proces dat de live RTSP-stream (`rtsp://127.0.0.1:8554/deurbel`) zonder hercodering (`-c copy`) direct opslaat als MP4.
  - `stop_manual_recording()`: Beëindigt de opname op een nette manier (`SIGINT`), genereert een thumbnail en dient een achtergrondtaak in voor compressie.
- **Low-Priority Achtergrondcompressie:**
  - Wordt uitgevoerd met `nice -n 19` om CPU-pieken te voorkomen.
  - Instellingen: `-vcodec libx264 -crf 28 -preset veryfast -acodec aac -b:a 48k -movflags +faststart`.
  - Reduceert bestanden met **70% tot 85%** met behoud van gezichts- en nummerplaatherkenning.
- **Dual-Tier Retentie & Lock/Star (⭐):**
  - **Tier 1 (Tijdsretentie):** Verwijdert niet-vergrendelde clips ouder dan het geconfigureerde aantal dagen (`retention_days`).
  - **Tier 2 (Capaciteitsbescherming):** Als de totale grootte van `/mnt/opslag/deurbel/clips` de limiet (`max_storage_gb`, standaard 10 GB) nadert, worden automatisch de oudste niet-vergrendelde clips opgeruimd tot de opslag onder 85% van het maximum daalt.
  - Clips met `is_locked = 1` worden door beide tiers genegeerd en blijven altijd bewaard.

#### C. `ai_detector.py` — Tweetraps AI Engine
- **Stage 1: OpenCV Bewegingsdetector (<1% CPU):**
  - Leest de substream (768x432 H.264 @ 10fps) met interval sampling (elke 5-10 frames).
  - Past een softwarematig privacymasker toe (`ignore_top_percent`, standaard 30%) om voorbijrijdend verkeer op de straat te negeren.
  - Berekent het absolute verschil tussen opeenvolgende frames met drempelwaarde filtering.
- **Stage 2: YOLOv8n ONNX Classificatie:**
  - Alleen geactiveerd bij daadwerkelijke beweging in de actieve zone.
  - Voert inferentie uit op een 640x640 letterbox frame via ONNXRuntime CPUExecutionProvider.
  - Gemiddelde inferentietijd op Ryzen 5 2400GE: **38 ms**.
  - Mapt COCO-klassen naar categorieën:
    - Mensen: `person (0)`
    - Voertuigen: `bicycle (1)`, `car (2)`, `motorcycle (3)`, `bus (5)`, `truck (7)`
    - Dieren: `bird (14)`, `cat (15)`, `dog (16)`, `horse (17)`, `sheep (18)`, `cow (19)`, `elephant (20)`, `bear (21)`, `zebra (22)`, `giraffe (23)`
    - Overige beweging: fallback als geen van bovenstaande klassen een betrouwbaarheid > 40% heeft.

---

## 2. Android App Architectuur & Schermen

De Android-applicatie is geschreven in **Kotlin** met **Jetpack Compose** en volgt de moderne Android MVVM-architectuur.

### 2.1 Dynamische Endpoint Resolutie (`EndpointResolver.kt`)
- `detectServer(context)`: Voert een snelle HTTP probe uit naar `http://192.168.2.1:8090/api/v1/status`.
- Time-out is ingesteld op **1500 ms**.
- Bepaalt automatisch of `apiBaseUrl` verwijst naar `http://192.168.2.1:8090` of `https://deurbel.foxway.nl`.
- WebSockets worden transparant omgezet naar `wss://deurbel.foxway.nl` buiten LAN.

### 2.2 Live Beeld & Audio Lifecycle (`LiveScreen.kt`)
- **Lifecycle Koppeling (`ON_PAUSE` / `ON_STOP`):**
  - Gebruikt `LocalLifecycleOwner.current` en `DisposableEffect`.
  - Zodra de gebruiker de app verlaat of het scherm vergrendelt:
    - Audio van de videospeler wordt gemute en de verbinding wordt verbroken.
    - Eventuele actieve microfoonopname (praten) wordt direct gestopt.
    - Handmatige opname timer wordt gepauzeerd/afgerond.
  - Bij terugkeer (`ON_RESUME`): stream wordt automatisch herstart.
- **Handmatige Opname Knop ("Opnemen"):**
  - Prominente pil-knop met pulserende rode indicator en live timer (`00:12`).
  - Roept `POST /api/v1/recordings/manual/start` en `stop` aan via `FoxwayApi`.
- **SD / HD Schakelaar:**
  - Wisselt tussen de hoofdstream (`deurbel`) en substream (`deurbel_sub`) voor snelle weergave bij zwakke 4G/5G-verbindingen.

### 2.3 Onboarding Scherm (`OnboardingScreen.kt`)
- Geen hardcoded namen of knoppen.
- Bevat een schoon tekstinvoerveld: **"Jouw naam (voor gezinsleden)"** met placeholder `"Vul je naam in..."`.
- Valideert dat de naam niet leeg is voordat koppeling kan plaatsvinden.
- Slaat `user_name` en `user_id` op in `SharedPreferences`.

### 2.4 Instellingenscherm (`SettingsScreen.kt`)
- **Opname- & Detectie-instellingen:**
  - Hoofdschakelaar voor slimme detectie.
  - Individuele toggles voor:
    - Mensen opnemen
    - Voertuigen opnemen
    - Dieren opnemen
    - Overige beweging opnemen
  - Stille Uren / Nachtmodus (schakelaar + tijdschema bijv. 23:00 - 07:00).
  - Statusindicatie van de actieve detectiezone (bovenste 30% genegeerd).
- **Bewaarduur & Opslagbeheer:**
  - Selector voor bewaarduur: `3, 7, 14, 30, 60, 90 dagen`.
  - Live opslaggebruiksbalk met weergave van huidig MB-verbruik en percentage van de 10 GB limiet.

### 2.5 Opnamescherm (`ClipsScreen.kt`)
- Weergave van opgeslagen videoclips met thumbnails en tijdsindicatie.
- **Ster / Vergrendel (⭐) Knop:** Maakt het mogelijk om belangrijke clips direct in de app te vergrendelen (`is_locked = true`), waardoor ze beschermd zijn tegen automatische retentieopruiming.
- **Categorie Badges:** Toont duidelijke tags zoals `Persoon`, `Voertuig`, `Dier`, `Beweging` of `Handmatig door <Naam>`.
- Ondersteuning voor direct afspelen en delen.

---

## 3. Beheer & Onderhoud

### 3.1 Container Herstarten
```bash
cd /mnt/opslag/deurbel/backend
docker compose down && docker compose up -d
```

### 3.2 Live Logs Bekijken
```bash
docker logs -f foxway-doorbell-backend
```

### 3.3 Swagger / OpenAPI Documentatie
De volledige interactieve documentatie is live beschikbaar via:
- Binnen LAN: `http://192.168.2.1:8090/docs`
- Buiten LAN: `https://deurbel.foxway.nl/docs`
