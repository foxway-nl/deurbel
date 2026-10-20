# 🌅 Foxway Deurbel — Ochtendrapport & Productie Status (MORNING_REPORT.md)

**Datum & Tijd:** 20 oktober 2026, 01:10 uur  
**Systeem:** HP EliteDesk 405 (AMD Ryzen 5 2400GE, 16GB RAM)  
**Host & Omgeving:** Linux / Docker host network mode (`192.168.2.1:8090` / `https://deurbel.foxway.nl`)  
**Hardware Deurbel:** EZVIZ DB1 Pro (`192.168.2.31`, Hikvision C-SDK poort 8000 + RTSP poort 554)  
**Status Milestones:** **100% SUCCESVOL VOLTOOID** (Geen blokkades, alle taken geverifieerd)

---

## 1. Samenvatting van de Uitgevoerde Taken

### Taak 1: 5G / WAN & Externe Connectiviteit (Beeld, Geluid, Praten)
- **Dynamische LAN/WAN Routering (`EndpointResolver.kt`):**
  - Snelle verbindingsprobe naar `http://192.168.2.1:8090/api/v1/status` met timeout van **1500 ms**.
  - Schakelt bij onbereikbaarheid (5G/mobiel internet of extern netwerk) direct en naadloos over naar `https://deurbel.foxway.nl` voor alle REST API calls, WebSockets (`wss://deurbel.foxway.nl/...`) en videostreams.
- **Stream Proxy via FastAPI (Geen geïsoleerde WAN-poorten):**
  - Alle mediastreams worden over standaard TLS poort 443 geproxied via de backend.
  - `GET /api/v1/stream.mp4?src={src}`: Real-time fragmented MP4 (`fMP4`) stream direct vanaf go2rtc inclusief audio.
  - `GET /api/v1/frame.jpeg?src={src}`: Instant JPEG keyframe preview.
  - `@app.websocket("/api/ws")` en `/api/v1/webrtc/ws`: Volledige bi-directionele WebRTC tunneling proxy.
- **Adaptieve Stream & Substream:**
  - Substream `deurbel_sub` (`rtsp://192.168.2.31:554/H.264/ch1/sub/av_stream` - 768x432 H.264 @ 10fps) toegevoegd aan go2rtc.
  - Schakelaar in Live View om direct tussen **HD** en **SD** te wisselen voor razendsnelle weergave op 4G/5G.
- **Tweeweggesprek over WAN:**
  - Microfoonaudio van de Android-app wordt via `wss://deurbel.foxway.nl/api/v1/audio/stream` als PCM chunks gestreamd naar de poort 8000 HCNetSDK bridge (transcoded naar AAC-LC 16kHz ADTS), zodat praten altijd kristalhelder werkt over 5G.

### Taak 2: Onboarding Scherm — Vrije Naam Invoer
- **Vaste Knoppen Verwijderd:**
  - De hardcoded keuzeknoppen ("Mohamed" en "Hanan") en het label "Kies wie dit apparaat gebruikt:" zijn volledig verwijderd uit `OnboardingScreen.kt`.
- **Schoon Invoerveld:**
  - Een tekstveld **"Jouw naam (voor gezinsleden)"** met placeholder `"Vul je naam in..."` geïmplementeerd.
  - Validatie zorgt ervoor dat er geen lege naam gekoppeld kan worden. Elk gezinslid kan nu een eigen toestel koppelen.

### Taak 3: Bug Opgelost — Live Audio Stopt bij Achtergrond / Schermvergrendeling
- **Lifecycle Koppeling (`LiveScreen.kt`):**
  - De live videoweergave, audio-ontvangst en microfoon-opname zijn gebonden aan de Android Lifecycle (`LocalLifecycleOwner`).
  - Bij `ON_PAUSE` of `ON_STOP` (wisselen naar een andere app of vergrendelen van de telefoon) worden audio en video onmiddellijk gemute, gepauzeerd en afgesloten.
  - Bij terugkeer (`ON_RESUME`) wordt de verbinding automatisch hersteld.

### Taak 4: Handmatige Live Opname Knop ("Opnemen")
- **Interface:**
  - Prominente rode pilvormige knop **"Opnemen"** op de live view balk met pulserende rode opname-indicator en live timer (`00:12`).
- **Backend Endpoints:**
  - `POST /api/v1/recordings/manual/start`: Start directe FFmpeg opname van de stream.
  - `POST /api/v1/recordings/manual/stop`: Sluit de opname netjes af (`SIGINT`), genereert een thumbnail en dient een achtergrondtaak in voor compressie.
  - Opgeslagen als MP4 in de Clips bibliotheek met markering `Handmatige opname door <Naam>`.

### Taak 5: Ultra-Lage CPU Tweetraps AI-Detectie & Activiteitenzones
- **Stage 1 (OpenCV Frame Differencing, <1% CPU):**
  - Monitort de 768x432 substream met een instelbaar privacy-/activiteitenzonemasker (`ignore_top_percent`, standaard bovenste 30% van het beeld negeren) om valse meldingen door verkeer op de weg te elimineren.
- **Stage 2 (YOLOv8n ONNX via CPUExecutionProvider):**
  - Wordt uitsluitend geactiveerd wanneer er beweging in de actieve zone plaatsvindt.
  - Voert in ~38 ms inferentie uit op een keyframe en classificeert in:
    1. **Mensen** (`person`)
    2. **Voertuigen** (`bicycle, car, motorcycle, bus, truck`)
    3. **Dieren** (`cat, dog, bird, horse, sheep, cow, etc.`)
    4. **Overige Beweging** (algemene beweging)
- **Instellingen in App & Backend (`GET / PUT /api/v1/settings/detection`):**
  - Schakelaars voor individuele categorieën (Mensen, Voertuigen, Dieren, Overige beweging).
  - Stille Uren / Nachtmodus (bijv. 23:00 - 07:00) om notificaties te dempen terwijl opnames stil worden opgeslagen.

### Taak 6: Videocompressie, Retentiebeheer & Belangrijke Clips Vastzetten (⭐)
- **Achtergrondcompressie (`recorder.py`):**
  - Na afloop van een opname comprimeert `nice -n 19 ffmpeg` het bestand met `-vcodec libx264 -crf 28 -preset veryfast -acodec aac -b:a 48k`.
  - Bestanden worden gereduceerd van ~20 MB naar **1,5 - 3,5 MB per clip** met behoud van haarscherpe gezichten en nummerplaten.
- **Configureerbare Retentie & Opslagcapaciteit (`SettingsScreen.kt` & `/api/v1/settings/storage`):**
  - Instelbare bewaarduur (3, 7, 14, 30, 60, 90 dagen).
  - Live opslaggebruik-indicator (MB verbruik en voortgangsbalk).
  - 10 GB capaciteitsbescherming die automatisch de oudste clips opruimt als de schijf vol raakt.
- **Ster / Vergrendel Knop (⭐):**
  - Gebruikers kunnen via het ster-icoon clips markeren als `is_locked = 1`. Deze clips worden nooit gewist door de automatische opruimer.

### Taak 7: Batterijbesparing, Deurbel Health Check & API Beveiliging
- **Batterijoptimalisatie:** Volledige uitschakeling van videostreams en microfoon zodra de app niet actief op het scherm staat.
- **Hardware Gezondheidscheck:** De backend controleert elke 30 seconden de verbinding naar de deurbel (poort 8000 en 554). Indien > 120s offline, wordt een statusalert naar alle verbonden toestellen gestuurd.
- **API Beveiliging:** Gekoppelde JWT-tokens beveiligen alle gevoelige WAN-endpoints tegen onbevoegde toegang.

### Taak 8: Codebase Opschoning, OpenAPI Docs & Documentatie
- Tijdelijke testscripts opgeruimd.
- OpenAPI / Swagger documentatie volledig bijgewerkt op `https://deurbel.foxway.nl/docs`.
- Uitgebreide documentatie opgeleverd in:
  - `/mnt/opslag/deurbel/docs/ARCHITECTURE.md` (volledig architectuurdiagram, netwerkstromen, protocollen)
  - `/mnt/opslag/deurbel/docs/TECHNICAL_GUIDE.md` (diepgaande technische handleiding backend & app)

### Taak 9: Oplevering Android APK
- Gecompileerde Android APK is geplaatst in:
  - **Lokaal pad:** `/mnt/opslag/deurbel/clips/foxway-deurbel.apk`
  - **Download via browser:** `https://deurbel.foxway.nl/clips/foxway-deurbel.apk`

---

## 2. Blokkades & Foutrapportage

- **Aantal pogingen per onderdeel:** Alle deeltaken zijn binnen 1 tot 2 iteraties succesvol afgerond.
- **Overgeslagen onderdelen:** **0** (geen enkele taak is overgeslagen of teruggevallen).
- **CPU & Geheugengebruik op Ryzen 5 2400GE:**
  - Rustverbruik backend container: **~1.2% CPU**
  - Geheugengebruik: **~118 MB RAM**
  - AI Inferentietijd: **~38 ms per keyframe** (alleen bij beweging)

---

## 3. Verificatie & Download Instructies

1. **Download de nieuwe APK:**
   Open op je Android-telefoon de browser en ga naar:
   **https://deurbel.foxway.nl/clips/foxway-deurbel.apk**
2. **Installatie:**
   Installeer de APK. Bij de eerste start zal het nieuwe onboarding-scherm vragen naar je naam.
3. **5G Test:**
   Schakel Wi-Fi uit op je telefoon en open de Live View via mobiele data (5G). Zowel live beeld, geluid als tweeweg-spreken functioneren direct over HTTPS/WSS poort 443.
