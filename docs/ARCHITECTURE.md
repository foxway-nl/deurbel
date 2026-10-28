# Foxway Smart Doorbell Architectuur (v2.3.0)

Dit document beschrijft de technische opzet en datastromen van versie 2.3.0.

---

## 1. Systeemoverzicht

```mermaid
flowchart TD
    subgraph Camera ["EZVIZ DB1 Deurbel (192.168.2.31)"]
        CamSensor["4:3 Sensor (960x720)"]
        CamMic["Microfoon"]
        CamSpk["Luidspreker"]
    end

    subgraph Server ["Foxway Server (192.168.2.1)"]
        RTSP["go2rtc (Poort 8554/1984)"]
        SDK["Hikvision C-SDK (Poort 8000)"]
        RingBuffer["RAM Ring-Buffer (/dev/shm/deurbel_ring)"]
        
        subgraph Backend ["FastAPI Backend (Docker :8090)"]
            LiveDrain["Zero-Lag Drain Worker (<100ms)"]
            Gatekeeper["3-Laags AI Gatekeeper (YOLOv8s)"]
            AdminUI["Web Admin Dashboard (/admin)"]
            SQLite[("SQLite DB (deurbel.db)")]
        end
    end

    subgraph Client ["Android Toestel (v2.3.0)"]
        Telecom["Telecom Framework (Belscherm)"]
        LiveView["LiveScreen (4:3 RGB_565)"]
        CrashHandler["Crash Logger (crash_last.json)"]
    end

    CamSensor -->|RTSP| RTSP
    RTSP -->|FFmpeg| RingBuffer
    RingBuffer --> LiveDrain
    LiveDrain -->|Binary WS :8090| LiveView
    
    CamMic <-->|SDK Audio| SDK
    SDK <-->|PCM Audio Bridge| LiveView
    
    RingBuffer --> Gatekeeper
    Gatekeeper -->|Event & Stub| SQLite
    Gatekeeper -->|Push <1.5s| Telecom
    
    CrashHandler -.->|POST crash| AdminUI
```

---

## 2. Het 3-Laags AI Verificatie Traject

```mermaid
sequenceDiagram
    autonumber
    participant C as Camera Stream
    participant L1 as Laag 1: Context & Footpoint
    participant L2 as Laag 2: 3-Frame Trajectverificatie
    participant DB as SQLite & Audit Logs
    participant Push as FCM / WebSocket Push
    participant Rec as Recorder (Smart Clip)

    C->>L1: Frame binnenkomst (960x720)
    L1->>L1: YOLOv8s full-frame detectie
    alt Onderkant box < 0.25h of in uitsluitingszone
        L1-->>DB: Afkeuren (Dakrand/boom/uitsluitingszone)
    else Geldig kandidaat-object
        L1->>L2: Start Gatekeeper tracking (T0, T0+0.5s, T0+1.0s)
        L2->>L2: Bereken verplaatsing Delta d
        alt Delta d < 6% (persoon) of < 12% (voertuig)
            L2-->>DB: Status REJECTED (Stilstaand/struik/geparkeerde auto)
        else Delta d >= d_min (Werkelijk bewegend object)
            L2->>DB: Status APPROVED & Event opslaan
            L2->>Push: Direct Push Notificatie (< 1.5s)
            L2->>Rec: Start smart clip (Pre-roll 6s + passage + 5s post-roll)
        end
    end
```

---

## 3. Zero-Lag Live Stream Drain Architectuur

In traditionele pipelines buffert een OS pipe frames wanneer een client langzamer leest, waardoor de weergave 5 tot 15 seconden achterloopt op de realiteit. 

In v2.3.0 is een dedicated `_drain_worker` geïmplementeerd in `live_hub.py`:
1. Leest continu met `os.read(pipe_fd, 65536)` blokken uit de OS pipe.
2. Parsed markers (`SOI` `0xFFD8` tot `EOI` `0xFFD9`) en assembleert direct een volwaardig JPEG-frame.
3. Overschrijft onmiddellijk `_latest_jpeg_frame` in geheugen en gooit eventuele tussenliggende frames direct weg (`drop`).
4. Clients die verbonden zijn via `GET /api/v1/ws/live` ontvangen altijd het actuele beeld met een leeftijd van $<100\text{ms}$.
