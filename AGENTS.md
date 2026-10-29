# Regels & Instructies voor Ontwikkelaars en AI-Agents (AGENTS.md)

Welkom in het Foxway Smart Doorbell project. Lees deze regels zorgvuldig voordat je code wijzigt of commando's uitvoert.

---

## 🚨 HARDE ANTI-REGRESSIE REGELS

1. **NOOIT OUDE SPECS GEBRUIKEN:**
   - De codebase bevindt zich op versie **v2.9.0** (`versionCode = 20`).
   - Oude documenten over v1.0 / v2.0 / v2.3 / v2.4 / v2.5 / v2.6 / v2.8 mogen nooit leidend zijn.

2. **BESCHERMDE KERN-COMPONENTEN (NIET AANRAKEN):**
   - **Tweeweg-Audio (`backend/app/audio_bridge.py`):** De Hikvision Poort 8000 C-SDK (`libhcnetsdk.so`) met pre-warmed login sessies en 1000 Hz loopback FFT test werkt foutloos. Wijzig deze logica NIET.
   - **4:3 Beeldverhouding:** De EZVIZ DB1 heeft een native 4:3 sensor. Alle videostreams, opnames en Android UI-containers (`LiveScreen.kt`, `CallScreen.kt`, `ClipsScreen.kt`, `/admin`) moeten 4:3 blijven.
   - **Zero-UI Live Player & Native Hardware Streaming (go2rtc):** Live streaming verloopt via `/api/v1/live/player` (Zero-UI `<zero-player>` met snapshot-poster op milliseconde 0, geobserveerde controle-vrije weergave, automatische freeze-watchdog en JS `reconnectStream()`).
   - **Onbeperkte Deurbel Triggers:** `/api/v1/webhook/shelly-ring?force=true` omzeilt alle debounces en actieve states; ophangen in Android reset direct deduplicatie.
   - **Stille Uren (23:30 - 07:30 Europe/Amsterdam):** AI-beweging muteert pushnotificaties (`silent = True`, geen FCM/push geluid), terwijl deurbeloproepen (`CALL_RINGING`) altijd rinkelen.
   - **Anti-Parked-Car Swap AI (`backend/app/ai_detector.py`):** `parked_spots` pre-filtering (`IoU >= 0.55`, `ratio < 0.30` direct afgewezen) en stap-verplaatsing validatie (`0.02*w <= step_disp <= 0.12*w`, `IoU >= 0.15`) voorkomen het verspringen tussen stilstaande auto's.
   - **2-Fasen Clip Lifecycle (`RECORDING` -> `READY`):** Bij detectie $T_0$ wordt EERST de thumbnail synchroon weggeschreven naar `/mnt/opslag/deurbel/clips/thumb_<clip_id>.jpg` (>5 KB), waarna de clip-rij direct met `status = "RECORDING"` in de database wordt geplaatst vóór het push-event. Pas nadat de 20s Post-Roll en concat klaar zijn (`.mp4 > 100 KB`, `duration >= 5s`), wordt de clip bijgewerkt naar `status = "READY"`.
   - **Push Notificatie Miniatuurfoto's:** Notificaties bevatten direct `.setLargeIcon(bitmap)` én `.setStyle(BigPictureStyle().bigPicture(bitmap))` via `$activeBaseUrl/api/v1/clips/<clip_id>/thumbnail` met 500ms fallback retry.
   - **RAM Ring-Buffer & Sourcing:** `/dev/shm/deurbel_ring` schrijft chronologische 2-seconden Unix-timestamp segmenten (`seg_%s.ts`) met automatische gap-detectie en 20s pre-roll / 20s post-roll.
   - **Gecentraliseerde SQLite WAL (`backend/app/database.py`):** Alle database-operaties verlopen via `get_db()` met WAL mode, busy timeout 60s en write-lock bescherming.
   - **Netwerk & Routing:** Server draait lokaal op `http://192.168.2.1:8090` en extern via Cloudflare op `https://deurbel.foxway.nl`. Bij 5G/Cellular data wordt LAN probing overgeslagen om ping-pong te voorkomen.

3. **SYSTEEMBRONNEN & CGROUP REGELS:**
   - Draai nooit zware parallelle processen op de host.
   - Gebruik voor `ffmpeg` altijd `nice -n 15`.
   - Bouw Android altijd met `--max-workers=2`.

---

## 🛠️ STANDAARD COMMANDO'S

### Python Tests Uitvoeren
Python packages draaien in de Docker backend container. Draai tests altijd via:
```bash
docker exec foxway-doorbell-backend pytest -v /mnt/opslag/deurbel/backend/tests/
```

### Android APK Compileren
Java en de Android SDK zijn geïnstalleerd in de container `thyrlian/android-sdk:latest`. Bouw altijd via:
```bash
docker run --rm -v /mnt/opslag/deurbel:/mnt/opslag/deurbel -w /mnt/opslag/deurbel/android-app thyrlian/android-sdk:latest ./gradlew assembleRelease --max-workers=2
```
Kopieer na succes de APK naar `/mnt/opslag/deurbel/clips/foxway-deurbel.apk`.

### Git Workflow
Commits moeten duidelijke conventionele commitberichten hebben (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
Push altijd naar de `main` branch van `git@github.com:foxway-nl/deurbel.git`.
