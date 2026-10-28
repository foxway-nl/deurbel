# Foxway Smart Doorbell System Specification (v2.3.0)

**Versie:** `v2.3.0` (`versionCode = 14`)  
**Datum:** 28 Oktober 2026  
**Productie-omgeving:** `foxway` (`192.168.2.1`), WAN: `https://deurbel.foxway.nl`  
**Camera:** EZVIZ DB1 (`192.168.2.31`), native **4:3 aspect ratio** (`768x576` / `960x720`)

---

## 1. Systeemoverzicht & Architectuur
Het Foxway Deurbelsysteem is een end-to-end, privacy-vriendelijk en ultra-low-latency videodeurbelsysteem bestaande uit:
1. **EZVIZ DB1 Camera (Poort 8000 C-SDK & RTSP):**
   - Native 4:3 beeldsensor met microfoon en luidspreker.
   - Tweeweg-audio gestuurd via `libhcnetsdk.so` C-SDK (`app/audio_bridge.py`) over TCP poort 8000.
2. **FastAPI & Python Media Server (`backend` in Docker):**
   - Draait op poort 8090 met `network_mode: host`.
   - **Zero-lag Live Drain:** Dedicated achtergrond-draad leest direct van FFmpeg OS pipe (`/dev/shm/deurbel_ring`) en behoudt alleen het allerlaatste complete frame (`<100ms` latentie).
   - **3-Laags AI Gatekeeper (`VerificationGatekeeper`):** YOLOv8s op volledig 4:3 frame, grondvlak-filtering ($y_2 \ge 0.25h$), interactieve uitsluitingszones, 3-frame trajectcontrole ($\Delta d \ge 6\%$ voor persoon/dier, $\ge 12\%$ voor voertuig).
   - **Direct Push Coupling:** Stuurt FCM en WebSocket push notificaties binnen 1.5s (stub-event registratie op $T_0 + 1.0s$).
   - **Web Admin Paneel (`/admin`):** Dark-mode dashboard met sessie-authenticatie, live videomonitor, dynamische variabelen, uitsluitingszones canvas, AI inspectietabel en Android crash logs.
3. **Android Client App (`android-app`):**
   - Native Jetpack Compose, Kotlin coroutines.
   - Telecom framework integratie met `ConnectionService` voor volscherm belscherm (`TelecomManager`).
   - `RGB_565` bitmap decodering en `AudioTrack` mutex-bescherming tegen OOM en threading crashes.
   - Automatische crash logger naar `crash_last.json` en `POST /api/v1/logs/client-crash`.

---

## 2. Harde Systeemeisen & Beschermde Componenten
- **Geen Regressie:** Eerdere functionaliteit van v2.2.0 (Telecom belscherm, C-SDK tweeweg-audio, mDNS discovery) mag onder geen enkel beding worden gesloopt.
- **Tweeweg-Audio (`backend/app/audio_bridge.py`):** De C-SDK `libhcnetsdk.so` met pre-warmed login sessies en 1000 Hz loopback FFT test is beschermd.
- **4:3 Aspect Ratio:** Alle weergaven in Android (`LiveScreen.kt`, `CallScreen.kt`, `ClipsScreen.kt`) en Web Admin (`/admin`) zijn strikt 4:3.
- **RAM Ring-Buffer:** `/dev/shm/deurbel_ring` draait in RAM voor 6 seconden Pre-Roll.
- **Systeembronnen:** FFmpeg draait altijd met `nice -n 15`. Gradle builds met `--max-workers=2`.

---

## 3. 3-Laags AI Verificatie Gatekeeper (`ai_detector.py`)
Om valse meldingen door bewegende struiken, wind en geparkeerde auto's 100% uit te sluiten:
1. **Laag 1 — Volledige 4:3 Frame Context & Grondvlak:**
   - Geen misleidende mini-crops. YOLOv8s analyseert het volledige $960 \times 720$ frame.
   - Grondvlak filter: Box onderkant $y_2 < 0.25 \times \text{hoogte}$ wordt direct genegeerd (vogels in de lucht, dakranden, bomen).
   - Interactieve Uitsluitingszones: Bounding box overlaps met gedefinieerde polygonen/rects worden genegeerd.
2. **Laag 2 — 3-Frame Trajectverificatie ($1.2\text{s}$ Bewijs):**
   - Evaluatie op $t_0$, $t_0 + 0.5\text{s}$, $t_0 + 1.0\text{s}$.
   - Berekening van de verplaatsing:
     $$\Delta d = \frac{\sqrt{(cx_3 - cx_1)^2 + (cy_3 - cy_1)^2}}{\text{frame\_width}}$$
   - **Persoon / Dier:** Vereist $\Delta d \ge 0.06$ ($6\%$ schermbreedte).
   - **Voertuig:** Vereist $\Delta d \ge 0.12$ ($12\%$ schermbreedte).
   - Statische objecten ($\Delta d < \text{threshold}$) worden als `REJECTED` opgeslagen in `ai_audit_logs`.
3. **Laag 3 — Directe Push Koppeling & Object-Gestuurde Opnamelengte:**
   - Op $T_0 + 1.0s$ wordt direct een event in de database opgeslagen met clip-stub.
   - Push notificatie vertrekt binnen 1.5s naar alle toestellen.
   - Opname stopt wanneer het gedetecteerde object het beeld verlaat $+ 5\text{s}$ post-roll (maximale lengte: 25s). Geen clips van 65s door wind/bladeren meer.

---

## 4. Web Admin Dashboard (`/admin`)
- **Login:** `/admin/login` beveiligd met cookie-gebaseerde sessie (`doorbell_admin_session`) en bcrypt/sha256 wachtwoord hash.
- **Tab 1: Live Systeem & Stream Monitor:**
   - 4:3 live canvas, frame-leeftijd in ms, FPS, CPU/RAM meters, actieve processen.
   - Actieknoppen: Resync Live Stream, Schoon Valse Clips Op, Test Aanbellen, Test Notificatie.
- **Tab 2: Dynamische Variabelen & Uitsluitingszones:**
   - Realtime instellen van thresholds, displacement %, pre/post roll, ring FFT frequenties.
   - Interactief tekenen van maskeringszones op het camerabeeld.
- **Tab 3: AI Verificatie Inspector:**
   - Tabel met de laatste 50 evaluaties (Status: GOEDGEKEURD / GEBLOKKEERD), snapshot, categorie, confidence, $\Delta d\%$, en auditreden.
- **Tab 4: Foutenlogboek & Android Crash Logs:**
   - Realtime backend ring-logger (laatste 100 logs).
   - Android client crash logs ontvangen via `POST /api/v1/logs/client-crash`.

---

## 5. Android Client Specificaties
- **Versie:** `v2.3.0` (`versionCode = 14`).
- **Crash Safety:** Globale `Thread.setDefaultUncaughtExceptionHandler` en `CoroutineExceptionHandler` schrijven direct naar `crash_last.json` en versturen direct én bij app-start naar de server.
- **Geheugen:** `BitmapFactory.Options` gebruikt `inPreferredConfig = Bitmap.Config.RGB_565`.
- **AudioTrack:** Beschermd met interne mutex en `state == STATE_INITIALIZED` controles.
- **Sync Knop:** Prominente `"🔄 Sync Live"` knop in `LiveScreen.kt` herstart de verbinding zonder zwart beeld (laatste frame blijft zichtbaar).
