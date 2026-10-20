# Masterplan & Milestones: Custom Smart Doorbell (EZVIZ DB1 Pro + go2rtc + Android)

## Huidige Server Omgeving
- **Locatie:** `/mnt/opslag/deurbel`
- **Media Engine:** `go2rtc` draait al lokaal op `http://127.0.0.1:1984` (stream naam: `deurbel`, WebRTC poort: `8555` TCP/UDP).
- **Externe URL's:** Backend via `https://deurbel.foxway.nl` (wijst naar poort `8090`), WebRTC extern via `sip.foxway.nl:8555` / `foxway.nl:8555`.
- **Opslagmappen:** `/mnt/opslag/deurbel/clips`, `/mnt/opslag/deurbel/snapshots`, `/mnt/opslag/deurbel/audio`.

## Project Milestones

### Milestone 1: FastAPI Signaling & Media Backend (`/mnt/opslag/deurbel/backend`)
- Draait in Docker (`network_mode: host`, poort `8090`, inclusief `ffmpeg` en `zeroconf`/`avahi`).
- **Aanbel-trigger (`POST /api/ring`):**
  1. Haalt direct een live JPEG-snapshot op bij `http://127.0.0.1:1984/api/frame.jpeg?src=deurbel` en slaat deze op in `/mnt/opslag/deurbel/snapshots`.
  2. Start op de achtergrond een FFmpeg-opname van de `go2rtc` RTSP-stream (`rtsp://127.0.0.1:8554/deurbel`) naar `/mnt/opslag/deurbel/clips` (30 sec of tot einde gesprek).
  3. Zet de call-state op `RINGING` en stuurt direct een WebSocket-event + FCM High-Priority push naar alle geregistreerde Android-toestellen.
- **Multi-user Call State Machine:**
  - `POST /api/call/claim` (met `user_id` en `user_name`): Eerste gebruiker die opneemt krijgt de lock (`ACTIVE`). Alle andere toestellen ontvangen direct een `CALL_ANSWERED_BY` event (met naam van degene die opnam) zodat het rinkelen daar stopt.
  - `POST /api/call/hangup` en `POST /api/call/quick-reply` (speelt vooraf opgenomen audiobestand af via `go2rtc` backchannel naar de deurbel).
- **mDNS / Zeroconf Discovery:** Zendt `_deurbel._tcp.local.` uit op poort `8090` zodat de Android-app op hetzelfde wifi-netwerk de server met 1 klik vindt en configureert.
- **Clips API:** Endpoints om opgeslagen MP4-clips in `/mnt/opslag/deurbel/clips` op te vragen (`GET /api/clips`), te streamen, bij te knippen met FFmpeg (`POST /api/clips/trim` met start/eind-tijd) en te downloaden.

### Milestone 2: Android App Basis & 1-Klik Netwerk Discovery
- Native Android-app (Kotlin, Jetpack Compose, Material 3 UI).
- Scant via Android `NsdManager` naar `_deurbel._tcp.local.`.
- Met één druk op de knop koppelt de gebruiker (kiest eigen naam, bijv. "Mohamed" of "Hanan"), slaat zowel het lokale IP (`http://192.168.2.1:8090`) als de externe URL (`https://deurbel.foxway.nl`) op en houdt een persistente WebSocket/FCM-verbinding actief.

### Milestone 3: Echt Systeem-Belscherm & Multi-User Afhandeling
- Gebruikt Android `TelecomManager` / `ConnectionService` en `fullScreenIntent` zodat de telefoon bij een aanbel-actie altijd direct een volscherm inkomend gesprek toont (ook op een vergrendelde Samsung S25 Plus).
- Toont op het rinkelende belscherm direct de live snapshot van de bezoeker + knoppen voor: *Opnemen*, *Weigeren*, en *Quick-Reply* ("Zet pakketje bij de deur" / "Ik kom eraan").
- Als een andere gebruiker als eerste opneemt, sluit het belscherm direct en verschijnt een melding: *"Oproep beantwoord door [Naam]"*.

### Milestone 4: Razendsnelle 2-Weg WebRTC Video & Audio (< 300ms)
- **WebRTC Pre-warming:** Zodra de telefoon begint te rinkelen (`RINGING`), bouwt de app op de achtergrond de WebRTC-verbinding met `go2rtc` al op (gedempt). Bij drukken op *Opnemen* zijn beeld en spraak direct live zonder laadtijd.
- **Hardware Echo-onderdrukking:** Gebruikt `JavaAudioDeviceModule` op `USAGE_VOICE_COMMUNICATION` met hardwarematige `AcousticEchoCanceler` en `NoiseSuppressor` ingeschakeld tegen rondzingen.
- **Stealth Live-View:** Handmatig live kijken vanuit de app start altijd met een **gemute microfoon** (Push-to-Talk knop om te spreken).

### Milestone 5: Clips Tijdlijn, Speler & In-App Video Trimmer
- Overzichtelijke tab in de app met alle opnames en snapshots van de server.
- Ingebouwde ExoPlayer/Media3 videospeler met een schuifbalk (trimmer) om een fragment te selecteren, via de backend te laten knippen en direct op de telefoon op te slaan of te delen.
