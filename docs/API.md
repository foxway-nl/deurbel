# Foxway Smart Doorbell API Documentatie (v2.6.0)

De API draait op de FastAPI backend via poort `8090` (LAN: `http://192.168.2.1:8090`, WAN: `https://deurbel.foxway.nl`).

---

## 1. go2rtc Native Hardware Streaming Endpoints (`v2.6.0`)

### `GET /api/v1/live/player` (en `/live/player`)
- **Beschrijving:** Minimal fullscreen 4:3 HTML5 speler (MSE + WebRTC) die direct verbindt met de WebSocket proxy.
- **Query Parameters:** `src` (standaard `deurbel`).
- **Reactie:** `text/html` met native `<video-stream>` web component.

### `GET /api/go2rtc/ws` (en `/api/v1/go2rtc/ws`)
- **Type:** WebSocket
- **Beschrijving:** Zero-buffer bidirectionele proxy naar `ws://127.0.0.1:1984/api/ws?src=deurbel`.
- **Geschikt voor:** MSE streaming over 5G/Cloudflare WSS (TCP 443) zonder UDP vereiste, en WebRTC op LAN.

### `GET /api/go2rtc/{asset_name:path}`
- **Beschrijving:** Proxy voor go2rtc statische web-assets (`video-stream.js`, `video-rtc.js`).

---

## 2. Web Admin Endpoints (`/admin`)

### `GET /admin/login`
- **Beschrijving:** Toont het dark-mode inlogscherm.

### `POST /admin/login`
- **Form Data:** `username`, `password`
- **Reactie:** Zet een beveiligde, ondertekende HTTP-only sessiecookie `doorbell_admin_session` en stuurt door naar `/admin`.

### `GET /admin`
- **Beschrijving:** Hoofddashboard met 4 tabs: Live Stream Monitor, Dynamische Instellingen & Maskering, AI Inspector, Foutenlogboek.

### `GET /admin/logout`
- **Beschrijving:** Wist de sessiecookie en stuurt door naar `/admin/login`.

---

## 2. Admin REST API (`/api/v1/admin/*` & `/api/v1/live/*`)
*Vereist actieve admin sessiecookie.*

### `GET /api/v1/admin/settings`
- **Beschrijving:** Haalt alle actuele configuratiewaarden op.
- **Output:** JSON object met AI thresholds, `pre_roll_seconds`, `post_roll_seconds`, `max_active_motion_seconds`, `exclusion_zones_json`, etc.

### `POST /api/v1/admin/settings`
- **Beschrijving:** Update realtime de systeeminstellingen en herlaadt de AI parameters direct in de actieve workers.
- **Payload:**
```json
{
  "person_conf_threshold": 0.52,
  "vehicle_conf_threshold": 0.55,
  "animal_conf_threshold": 0.52,
  "min_person_displacement_pct": 6.0,
  "min_vehicle_displacement_pct": 12.0,
  "pre_roll_seconds": 20,
  "post_roll_seconds": 20,
  "max_active_motion_seconds": 30,
  "exclusion_zones_json": "[{\"name\":\"Zone 1\",\"x1\":0.1,\"y1\":0.2,\"x2\":0.4,\"y2\":0.5}]"
}
```

### `POST /api/v1/live/restart` (ook `POST /api/v1/admin/resync-live`)
- **Beschrijving:** Forceert een herstart van go2rtc en de live FFmpeg stream, wist het bevroren frame, en wacht tot er een daadwerkelijk nieuw frame met klokbeweging is ontvangen.

### `GET /api/v1/admin/detections`
- **Beschrijving:** Haalt de laatste 50 AI audit logs op (zowel goedgekeurd als geblokkeerd).
- **Parameters:** `limit` (standaard 50).

### `GET /api/v1/admin/crash-logs`
- **Beschrijving:** Haalt alle geregistreerde Android crash rapporten op.

### `POST /api/v1/admin/cleanup-false-clips`
- **Beschrijving:** Wist valse clips van vandaag die door wind/struiken zijn ontstaan.

### `POST /api/v1/admin/test-ring`
- **Beschrijving:** Simuleert een aanbel-event (`RINGING`), activeert telecom notificaties op alle geregistreerde Android toestellen.

### `POST /api/v1/admin/test-notification`
- **Beschrijving:** Stuurt een test-notificatie naar alle actieve WebSocket/FCM clients.

---

## 3. Client Crash Logging (`/api/v1/logs/client-crash`)

### `POST /api/v1/logs/client-crash`
- **Toegang:** Publiek (aangeroepen door Android app bij een uncaught exception).
- **Payload:**
```json
{
  "device_model": "Samsung SM-S936B",
  "os_version": "Android 15 (API 35)",
  "error_message": "OutOfMemoryError: Failed to allocate",
  "stacktrace": "java.lang.OutOfMemoryError: ... at nl.foxway.deurbel...",
  "screen_state": "LiveScreen",
  "app_version": "2.5.0"
}
```
- **Reactie:** `{"status": "ok", "id": 12}`

---

## 4. Live Stream & WebSockets (`/api/v1/ws/*` & `/api/v1/live/*`)

### `GET /api/v1/ws/live`
- **Beschrijving:** Binaire WebSocket stream voor live video en audio.
- **Formaat:**
  - Byte `0x01`: JPEG videobeeld (4:3 aspect ratio, 768x576).
  - Byte `0x02`: 16kHz 16-bit mono PCM audio van deurbelmicrofoon.

### `GET /api/v1/live/instant.jpg`
- **Beschrijving:** Levert direct het meest recente JPEG snapshot uit RAM met low latency (< 100ms).
- **Gebruik:** Snapshot preview in `/admin` en HTTP fallback polling (150ms) voor de Android client.

### `GET /api/v1/ws/events`
- **Beschrijving:** JSON WebSocket voor realtime belsignalen, bewegingsdetecties en statusupdates.

---

## 5. Deurbel & Clips Endpoints

### `POST /api/v1/ring`
- **Beschrijving:** Aanbel-trigger endpoint (aangeroepen via Shelly webhook of C-SDK events).

### `GET /api/v1/clips`
- **Beschrijving:** Lijst van opgeslagen clips met thumbnails, duur en AI-gebeurtenislabels.

### `GET /api/v1/app/version`
- **Beschrijving:** Geeft huidige server-versie (`2.5.0`), build code (`16`) en download link voor de Android APK.
