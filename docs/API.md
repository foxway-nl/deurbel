# Foxway Smart Doorbell API Documentatie (v2.4.0)

De API draait op de FastAPI backend via poort `8090` (LAN: `http://192.168.2.1:8090`, WAN: `https://deurbel.foxway.nl`).

---

## 1. Web Admin Endpoints (`/admin`)

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

## 2. Admin REST API (`/api/v1/admin/*`)
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

### `GET /api/v1/admin/detections`
- **Beschrijving:** Haalt de laatste 50 AI audit logs op (zowel goedgekeurd als geblokkeerd).
- **Parameters:** `limit` (standaard 50).

### `GET /api/v1/admin/crash-logs`
- **Beschrijving:** Haalt alle geregistreerde Android crash rapporten op.

### `POST /api/v1/admin/resync-live`
- **Beschrijving:** Forceert een herstart van de live FFmpeg stream en herinitialiseert de centrale frame buffer.

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
  "device_model": "Samsung SM-S928B",
  "os_version": "Android 14 (API 34)",
  "error_message": "OutOfMemoryError: Failed to allocate",
  "stacktrace": "java.lang.OutOfMemoryError: ... at nl.foxway.deurbel...",
  "screen_state": "LiveScreen",
  "app_version": "2.4.0"
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
