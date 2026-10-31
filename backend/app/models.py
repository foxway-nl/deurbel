"""
Foxway Smart Doorbell — Pydantic schemas & SQLite persistence.

Stores paired family devices, call logs, clip metadata, system settings,
and audit logs in a robust SQLite database using WAL mode and write-lock serialization.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from app.config import settings
from app.database import get_db, init_wal_mode

# ──────────────────────────────────────────────────────────────────────
#  Pydantic request / response schemas
# ──────────────────────────────────────────────────────────────────────


class CallState(str, Enum):
    IDLE = "IDLE"
    RINGING = "RINGING"
    IN_CALL = "IN_CALL"
    ENDED = "ENDED"


# — Pair —

class PairRequest(BaseModel):
    user_name: Optional[str] = Field("", description="Optional custom user name; defaults to 'Onbekend #X'")
    device_name: str = Field(..., examples=["Samsung S25 Plus"])
    fcm_token: str = Field("", description="Firebase Cloud Messaging token")
    device_id: Optional[str] = Field("", description="Stable Android device identifier (Settings.Secure.ANDROID_ID)")
    android_version: Optional[str] = Field("", description="Android OS version (e.g. 15 / API 35)")
    connection_type: Optional[str] = Field("", description="LAN or 5G")
    token: Optional[str] = Field("", description="Optional bootstrap / QR pairing token")


class PairResponse(BaseModel):
    user_id: str
    user_name: str
    device_name: str
    jwt_token: str
    lan_url: str = ""
    wan_url: str = ""
    go2rtc_lan_ws: str = ""
    go2rtc_wan_ws: str = ""


class UserInfo(BaseModel):
    user_id: str
    user_name: str
    device_name: str
    android_version: str = ""
    connection_type: str = ""
    online: bool = False
    last_seen: str = ""


class DeviceInfo(BaseModel):
    user_id: str
    user_name: str
    device_name: str
    fcm_token: str
    paired_at: str
    last_seen: str = ""
    device_id: str = ""
    android_version: str = ""
    connection_type: str = ""


class RingEvent(BaseModel):
    call_id: str
    state: CallState
    snapshot_url: str
    timestamp: str
    android_version: str = ""
    connection_type: str = ""


# — Call state machine —

class RingRequest(BaseModel):
    caller_name: Optional[str] = "Bezoeker"


class ClaimRequest(BaseModel):
    user_id: str
    user_name: str
    call_id: Optional[str] = None


CallClaimRequest = ClaimRequest


class ClaimResponse(BaseModel):
    status: str
    webrtc_src: Optional[str] = None
    answered_by: Optional[str] = None


class CallHangupRequest(BaseModel):
    user_id: Optional[str] = None
    reason: Optional[str] = "Normal hangup"


class QuickReplyRequest(BaseModel):
    audio_file: Optional[str] = None
    reply_id: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    call_id: Optional[str] = None


class CallStatusResponse(BaseModel):
    state: CallState
    call_id: Optional[str] = None
    started_at: Optional[str] = None
    answered_by: Optional[str] = None
    active_user_name: Optional[str] = None
    snapshot_url: Optional[str] = None
    participants: list[str] = []
    quick_replies: list[dict] = []


class CallLogItem(BaseModel):
    call_id: str
    started_at: str
    ended_at: Optional[str] = None
    answered_by: Optional[str] = None
    state: str
    snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None


# — Event —

class EventItem(BaseModel):
    id: int
    type: str  # "ring" | "motion"
    category: str = "general"
    title: str = "Gebeurtenis"
    created_at: str
    clip_id: Optional[str] = None
    snapshot_url: Optional[str] = None
    call_id: Optional[str] = None


# — Clip —

class ClipInfo(BaseModel):
    id: str
    timestamp: str
    duration: float
    snapshot_url: str
    video_url: str
    answered_by: Optional[str] = None
    starred: bool = False
    status: str = "READY"
    category: str = ""
    size_bytes: int = 0


class TrimRequest(BaseModel):
    start_sec: float
    end_sec: float
    clip_id: Optional[str] = None


# — WebSocket push events —

class WSEvent(BaseModel):
    event: str
    data: dict


# ──────────────────────────────────────────────────────────────────────
#  SQLite persistence layer (async)
# ──────────────────────────────────────────────────────────────────────

_DB_PATH = str(settings.DB_PATH)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS devices (
    user_id     TEXT PRIMARY KEY,
    user_name   TEXT NOT NULL,
    device_name TEXT NOT NULL,
    fcm_token   TEXT NOT NULL,
    jwt_token   TEXT NOT NULL,
    paired_at   TEXT NOT NULL,
    last_seen   TEXT DEFAULT '',
    device_id   TEXT DEFAULT '',
    android_version TEXT DEFAULT '',
    connection_type TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS call_logs (
    call_id       TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    answered_by   TEXT,
    state         TEXT NOT NULL,
    snapshot_path TEXT,
    clip_path     TEXT
);

CREATE TABLE IF NOT EXISTS clips (
    clip_id       TEXT PRIMARY KEY,
    call_id       TEXT,
    created_at    TEXT NOT NULL,
    duration      REAL NOT NULL DEFAULT 0.0,
    snapshot_path TEXT,
    video_path    TEXT NOT NULL,
    answered_by   TEXT,
    starred       INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'READY',
    category      TEXT DEFAULT '',
    file_size     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,
    category      TEXT DEFAULT 'general',
    title         TEXT NOT NULL,
    clip_id       TEXT,
    snapshot_url  TEXT,
    silent        INTEGER DEFAULT 0,
    trigger_source TEXT DEFAULT 'UNKNOWN',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_audit_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    status           TEXT NOT NULL,
    category         TEXT NOT NULL,
    label            TEXT NOT NULL,
    confidence       REAL NOT NULL,
    displacement_pct REAL NOT NULL,
    reason           TEXT NOT NULL,
    snapshot_path    TEXT DEFAULT '',
    boxes_json       TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS crash_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    device_name   TEXT DEFAULT '',
    device_id     TEXT DEFAULT '',
    app_version   TEXT DEFAULT '',
    error_message TEXT NOT NULL,
    stacktrace    TEXT NOT NULL,
    screen_state  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sdk_alarm_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT NOT NULL,
    command_id     INTEGER NOT NULL,
    command_hex    TEXT NOT NULL,
    command_name   TEXT NOT NULL,
    event_type     INTEGER,
    event_name     TEXT,
    details        TEXT DEFAULT '',
    dw_buf_len     INTEGER DEFAULT 0,
    by_deploy_type INTEGER DEFAULT 0,
    payload_hex    TEXT DEFAULT '',
    payload_ascii  TEXT DEFAULT '',
    action_taken   TEXT DEFAULT 'Logged'
);
"""


async def init_db() -> None:
    """Create tables if they don't exist, initialize WAL mode, and run schema migrations."""
    init_wal_mode()
    async with get_db(write=True) as db:
        await db.executescript(_SCHEMA_SQL)
        # Migration: ensure last_seen, device_id, android_version, connection_type columns exist
        for col_def in [
            "last_seen TEXT DEFAULT ''",
            "device_id TEXT DEFAULT ''",
            "android_version TEXT DEFAULT ''",
            "connection_type TEXT DEFAULT ''",
        ]:
            try:
                await db.execute(f"ALTER TABLE devices ADD COLUMN {col_def}")
            except Exception:
                pass

        # Migration: ensure status, category, file_size exist in clips table (v2.8.0 2-phase lifecycle)
        for col_def in [
            "status TEXT NOT NULL DEFAULT 'READY'",
            "category TEXT DEFAULT ''",
            "file_size INTEGER DEFAULT 0",
        ]:
            try:
                await db.execute(f"ALTER TABLE clips ADD COLUMN {col_def}")
            except Exception:
                pass

        # Migration: ensure trigger_source exists in events table (v3.3.1)
        try:
            await db.execute("ALTER TABLE events ADD COLUMN trigger_source TEXT DEFAULT 'UNKNOWN'")
        except Exception:
            pass

        # Deduplicate stale duplicate registrations keeping the newest record
        try:
            await db.execute("""
                DELETE FROM devices
                WHERE rowid NOT IN (
                    SELECT max(rowid)
                    FROM devices
                    GROUP BY user_name, device_name
                )
            """)
        except Exception:
            pass

        # Ensure server_wan_url default exists in app_settings
        try:
            await db.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('server_wan_url', 'https://deurbel.foxway.nl')"
            )
        except Exception:
            pass

        # Seed default system_settings (v2.6.0 standards: 20s pre-roll, 20s post-roll, 8s cooldown, 2 frames verify)
        now_iso = datetime.now(timezone.utc).isoformat()
        default_settings = [
            ("person_conf_threshold", "0.52", "Minimale betrouwbaarheid voor personen (0.1 - 1.0)"),
            ("vehicle_conf_threshold", "0.50", "Minimale betrouwbaarheid voor voertuigen (0.1 - 1.0)"),
            ("animal_conf_threshold", "0.50", "Minimale betrouwbaarheid voor dieren (0.1 - 1.0)"),
            ("min_person_displacement_pct", "3.0", "Minimale verplaatsing persoon (% schermbreedte)"),
            ("min_vehicle_displacement_pct", "5.0", "Minimale verplaatsing voertuig (% schermbreedte)"),
            ("verify_frames_required", "2", "Aantal opeenvolgende frames voor traject-verificatie"),
            ("pre_roll_seconds", "20", "Pre-roll seconden uit RAM buffer vóór gebeurtenis"),
            ("post_roll_seconds", "20", "Post-roll seconden ná verdwijnen van object"),
            ("max_active_motion_seconds", "30", "Maximale actieve bewegingsduur tijdens opname (sec)"),
            ("max_clip_seconds", "70", "Totale maximale clipduur (sec)"),
            ("event_cooldown_seconds", "8", "Afkoelperiode tussen opeenvolgende meldingen (sec)"),
            ("exclusion_zones", "[]", "JSON lijst van gemaskeerde zones [x1, y1, x2, y2]"),
            ("admin_password", "foxway2024", "Beheerderswachtwoord voor /admin"),
            ("quiet_hours_enabled", "true", "Stille uren inschakelen (geen pushmeldingen voor beweging)"),
            ("quiet_hours_start", "23:30", "Begintijd stille uren (HH:MM)"),
            ("quiet_hours_end", "07:30", "Eindtijd stille uren (HH:MM)"),
        ]
        for k, v, desc in default_settings:
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO system_settings (key, value, description, updated_at) VALUES (?, ?, ?, ?)",
                    (k, v, desc, now_iso),
                )
            except Exception:
                pass

        # Migration to v2.6.0, v2.9.0 & v3.1.4: update defaults and purge acoustic chime
        try:
            await db.execute("DELETE FROM system_settings WHERE key IN ('audio_chime_fallback_enabled', 'ring_ding_hz', 'ring_dong_hz', 'ring_sharpness')")
            await db.execute("UPDATE system_settings SET value = '20' WHERE key = 'pre_roll_seconds' AND value = '6'")
            await db.execute("UPDATE system_settings SET value = '20' WHERE key = 'post_roll_seconds' AND value = '5'")
            await db.execute("UPDATE system_settings SET value = '2' WHERE key = 'verify_frames_required'")
            await db.execute("UPDATE system_settings SET value = '5.0' WHERE key = 'min_vehicle_displacement_pct'")
            await db.execute("UPDATE system_settings SET value = '8' WHERE key = 'event_cooldown_seconds'")
            await db.execute("UPDATE system_settings SET value = '23:30' WHERE key = 'quiet_hours_start' AND value = '23:00'")
            await db.execute("UPDATE system_settings SET value = '07:30' WHERE key = 'quiet_hours_end' AND value = '07:00'")
            await db.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, description, updated_at) VALUES ('quiet_hours_enabled', 'true', 'Stille uren inschakelen (geen pushmeldingen voor beweging)', ?)",
                (now_iso,),
            )
            await db.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, description, updated_at) VALUES ('quiet_hours_start', '23:30', 'Begintijd stille uren (HH:MM)', ?)",
                (now_iso,),
            )
            await db.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, description, updated_at) VALUES ('quiet_hours_end', '07:30', 'Eindtijd stille uren (HH:MM)', ?)",
                (now_iso,),
            )
            await db.execute("ALTER TABLE events ADD COLUMN silent INTEGER DEFAULT 0")
            await db.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, description, updated_at) VALUES ('max_active_motion_seconds', '30', 'Maximale actieve bewegingsduur tijdens opname (sec)', ?)",
                (now_iso,),
            )
        except Exception:
            pass

        # Cleanup orphan events without a valid clip_id in clips table and deduplicate to strict 1-to-1 coupling
        try:
            await db.execute("""
                DELETE FROM events
                WHERE clip_id IS NOT NULL
                  AND clip_id != ''
                  AND clip_id NOT IN (SELECT clip_id FROM clips)
                  AND (julianday('now') - julianday(created_at)) >= 0.003
            """)
            await db.execute("""
                DELETE FROM events
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM events
                    WHERE clip_id IS NOT NULL AND clip_id != ''
                    GROUP BY clip_id
                )
                AND clip_id IS NOT NULL AND clip_id != ''
            """)
        except Exception:
            pass


# ── Device helpers ────────────────────────────────────────────────────

async def find_existing_device(
    device_id: str = "",
    user_name: str = "",
    device_name: str = "",
) -> Optional[dict]:
    """Find existing device by unique hardware ID or matching user AND device model."""
    clean_user = (user_name or "").strip().lower()
    clean_dev_id = (device_id or "").strip()
    clean_dev_name = (device_name or "").strip().lower()
    async with get_db(write=False) as db:
        if clean_dev_id:
            cursor = await db.execute("SELECT * FROM devices WHERE device_id = ?", (clean_dev_id,))
            row = await cursor.fetchone()
            if row:
                return dict(row)
        if clean_user and clean_dev_name:
            cursor = await db.execute(
                "SELECT * FROM devices WHERE LOWER(TRIM(user_name)) = ? AND LOWER(TRIM(device_name)) = ? ORDER BY rowid DESC LIMIT 1",
                (clean_user, clean_dev_name),
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
        return None


async def is_username_taken(user_name: str, device_id: str = "", device_name: str = "") -> bool:
    """Return True if user_name is already registered to a DIFFERENT physical device."""
    clean_user = (user_name or "").strip().lower()
    clean_dev_id = (device_id or "").strip()
    clean_dev_name = (device_name or "").strip().lower()
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT * FROM devices WHERE LOWER(TRIM(user_name)) = ?", (clean_user,))
        rows = await cursor.fetchall()
        for r in rows:
            r_dev_id = (r["device_id"] or "").strip()
            r_dev_name = (r["device_name"] or "").strip().lower()
            if clean_dev_id and r_dev_id and clean_dev_id == r_dev_id:
                continue
            if clean_dev_name and r_dev_name and clean_dev_name == r_dev_name:
                continue
            return True
        return False


async def get_next_default_username() -> str:
    """Return next available 'Onbekend #X' username."""
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT user_name FROM devices WHERE user_name LIKE 'Onbekend #%'")
        rows = await cursor.fetchall()
        existing_numbers = set()
        for (name,) in rows:
            try:
                num = int(name.split("#")[-1].strip())
                existing_numbers.add(num)
            except Exception:
                pass
        next_num = 1
        while next_num in existing_numbers:
            next_num += 1
        return f"Onbekend #{next_num}"


async def save_device(
    user_id: str,
    user_name: str,
    device_name: str,
    fcm_token: str,
    jwt_token: str,
    device_id: str = "",
    android_version: str = "",
    connection_type: str = "",
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_dev_name = (device_name or "").strip().lower()
    clean_dev_id = (device_id or "").strip()
    async with get_db(write=True) as db:
        if clean_dev_id:
            await db.execute("DELETE FROM devices WHERE device_id = ? AND user_id != ?", (clean_dev_id, user_id))
        if clean_dev_name:
            await db.execute("DELETE FROM devices WHERE LOWER(TRIM(device_name)) = ? AND user_id != ?", (clean_dev_name, user_id))

        await db.execute(
            """
            INSERT INTO devices (user_id, user_name, device_name, fcm_token, jwt_token, paired_at, last_seen, device_id, android_version, connection_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_name = excluded.user_name,
                device_name = excluded.device_name,
                fcm_token = excluded.fcm_token,
                jwt_token = excluded.jwt_token,
                paired_at = excluded.paired_at,
                last_seen = excluded.last_seen,
                device_id = COALESCE(NULLIF(excluded.device_id, ''), devices.device_id),
                android_version = COALESCE(NULLIF(excluded.android_version, ''), devices.android_version),
                connection_type = COALESCE(NULLIF(excluded.connection_type, ''), devices.connection_type)
            """,
            (user_id, user_name, device_name, fcm_token, jwt_token, now_iso, now_iso, device_id, android_version, connection_type),
        )


async def get_camera_credentials() -> dict:
    """Return dynamic camera credentials from DB or fallback to environment variables."""
    ip = await get_setting("camera_ip", settings.DOORBELL_IP)
    port = int(await get_setting("camera_sdk_port", str(settings.DOORBELL_SDK_PORT)))
    user = await get_setting("camera_user", settings.DOORBELL_USER)
    pw = await get_setting("camera_pass", settings.DOORBELL_PASS)
    return {
        "ip": ip,
        "port": port,
        "user": user,
        "pass": pw,
        "is_configured": bool(pw),
    }


async def set_camera_credentials(ip: str, port: int, user: str, password: str) -> None:
    """Persist updated camera credentials to SQLite app_settings."""
    await set_setting("camera_ip", ip)
    await set_setting("camera_sdk_port", str(port))
    await set_setting("camera_user", user)
    await set_setting("camera_pass", password)


async def delete_device(user_id: str) -> bool:
    """Delete a paired device by user_id."""
    async with get_db(write=True) as db:
        cursor = await db.execute("DELETE FROM devices WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0


async def update_device_last_seen(user_id: str, last_seen: Optional[str] = None) -> None:
    if not last_seen:
        last_seen = datetime.now(timezone.utc).isoformat()
    async with get_db(write=True) as db:
        await db.execute(
            "UPDATE devices SET last_seen = ? WHERE user_id = ?",
            (last_seen, user_id),
        )


async def get_all_devices() -> list[dict]:
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT * FROM devices")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_device(user_id: str) -> Optional[dict]:
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT * FROM devices WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_fcm_token(user_id: str, fcm_token: str) -> None:
    async with get_db(write=True) as db:
        await db.execute(
            "UPDATE devices SET fcm_token = ? WHERE user_id = ?",
            (fcm_token, user_id),
        )


# ── Call log helpers ──────────────────────────────────────────────────

async def save_call_log(
    call_id: str,
    state: str,
    snapshot_path: str = "",
    clip_path: str = "",
) -> None:
    async with get_db(write=True) as db:
        await db.execute(
            """
            INSERT INTO call_logs (call_id, started_at, state, snapshot_path, clip_path)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET
                state = excluded.state,
                snapshot_path = COALESCE(NULLIF(excluded.snapshot_path, ''), call_logs.snapshot_path),
                clip_path = COALESCE(NULLIF(excluded.clip_path, ''), call_logs.clip_path)
            """,
            (call_id, datetime.now(timezone.utc).isoformat(), state, snapshot_path, clip_path),
        )


async def update_call_log(call_id: str, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [call_id]
    async with get_db(write=True) as db:
        await db.execute(
            f"UPDATE call_logs SET {set_clause} WHERE call_id = ?",
            values,
        )


async def get_call_log(call_id: str) -> Optional[dict]:
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT * FROM call_logs WHERE call_id = ?", (call_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


# ── Clip helpers ──────────────────────────────────────────────────────

async def save_clip(
    clip_id: str,
    call_id: str,
    video_path: str,
    snapshot_path: str = "",
    answered_by: str = "",
    duration: float = 0.0,
    status: str = "READY",
    category: str = "",
    file_size: int = 0,
    created_at: Optional[str] = None,
) -> None:
    now_iso = created_at or datetime.now(timezone.utc).isoformat()
    async with get_db(write=True) as db:
        await db.execute(
            """
            INSERT INTO clips (clip_id, call_id, created_at, duration, snapshot_path, video_path, answered_by, status, category, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clip_id) DO UPDATE SET
                duration = excluded.duration,
                snapshot_path = COALESCE(NULLIF(excluded.snapshot_path, ''), clips.snapshot_path),
                answered_by = COALESCE(NULLIF(excluded.answered_by, ''), clips.answered_by),
                status = excluded.status,
                category = COALESCE(NULLIF(excluded.category, ''), clips.category),
                file_size = excluded.file_size
            """,
            (clip_id, call_id, now_iso, duration, snapshot_path, video_path, answered_by, status, category, file_size),
        )


async def list_clips(limit: int = 100, offset: int = 0) -> list[dict]:
    async with get_db(write=False) as db:
        cursor = await db.execute(
            "SELECT * FROM clips ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_clip(clip_id: str) -> Optional[dict]:
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT * FROM clips WHERE clip_id = ?", (clip_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_clip(clip_id: str) -> bool:
    async with get_db(write=True) as db:
        cursor = await db.execute("DELETE FROM clips WHERE clip_id = ?", (clip_id,))
        return cursor.rowcount > 0


async def update_clip(clip_id: str, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [clip_id]
    async with get_db(write=True) as db:
        await db.execute(
            f"UPDATE clips SET {set_clause} WHERE clip_id = ?",
            values,
        )


async def get_expired_clips(retention_days: int) -> list[dict]:
    """Return clips older than *retention_days* that are not starred."""
    async with get_db(write=False) as db:
        cursor = await db.execute(
            """
            SELECT * FROM clips
            WHERE starred = 0
              AND julianday('now') - julianday(created_at) > ?
            """,
            (retention_days,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_unlocked_clips_oldest_first() -> list[dict]:
    """Return all clips where starred = 0, sorted oldest first (for storage cap pruning)."""
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT * FROM clips WHERE starred = 0 ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_setting(key: str, default: str = "") -> str:
    """Retrieve an application setting by key."""
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    """Save or update an application setting."""
    async with get_db(write=True) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


async def save_event(
    event_type: str,
    category: str,
    title: str,
    clip_id: str = "",
    snapshot_url: str = "",
    silent: bool = False,
    trigger_source: str = "UNKNOWN",
) -> dict:
    """Record an event (ring or confirmed motion) in the events table."""
    now_iso = datetime.now(timezone.utc).isoformat()
    if not snapshot_url and clip_id:
        snapshot_url = f"/api/v1/clips/{clip_id}/thumbnail"
    silent_val = 1 if silent else 0
    async with get_db(write=True) as db:
        cursor = await db.execute(
            """
            INSERT INTO events (type, category, title, clip_id, snapshot_url, silent, trigger_source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, category, title, clip_id, snapshot_url, silent_val, trigger_source, now_iso),
        )
        event_id = cursor.lastrowid
        return {
            "id": event_id,
            "type": event_type,
            "category": category,
            "title": title,
            "clip_id": clip_id,
            "snapshot_url": snapshot_url,
            "silent": silent,
            "trigger_source": trigger_source,
            "created_at": now_iso,
        }


async def get_events_since(since_id: int = 0, limit: int = 50, order: str = "asc") -> list[dict]:
    """
    Retrieve new events with strict clip coupling or recent active event window.
    If order == 'desc' or (since_id == 0 and limit == 1), returns the latest events.
    Permits events coupled to clips table OR events created in the last 180 seconds
    (so immediate push notifications are visible on phone before video finishes recording).
    """
    async with get_db(write=False) as db:
        filter_clause = """
            (e.clip_id IN (SELECT clip_id FROM clips) 
             OR (julianday('now') - julianday(e.created_at)) < 0.003)
        """
        if order.lower() == "desc" or (since_id == 0 and limit == 1):
            cursor = await db.execute(
                f"""
                SELECT e.* FROM events e
                WHERE {filter_clause}
                ORDER BY e.id DESC LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            )
        else:
            cursor = await db.execute(
                f"""
                SELECT e.* FROM events e
                WHERE e.id > ? AND {filter_clause}
                ORDER BY e.id ASC LIMIT ?
                """,
                (since_id, max(1, min(limit, 200))),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_dynamic_wan_url() -> str:
    """Return the currently detected or configured dynamic WAN URL."""
    from app.config import settings
    val = await get_setting("server_wan_url", settings.WAN_URL or "https://deurbel.foxway.nl")
    if val and val.startswith("http"):
        return val.rstrip("/")
    return (settings.WAN_URL or "https://deurbel.foxway.nl").rstrip("/")


async def set_dynamic_wan_url(url: str) -> None:
    """Persist dynamic WAN URL to app_settings."""
    if url and url.startswith(("http://", "https://")):
        await set_setting("server_wan_url", url.rstrip("/"))


# ── System Settings (Task 1 & Task 3) ────────────────────────────────

async def get_system_settings() -> dict[str, str]:
    """Return all system settings as a dictionary."""
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT key, value FROM system_settings")
        rows = await cursor.fetchall()
        return {r[0]: r[1] for r in rows}


async def get_system_setting(key: str, default: str = "") -> str:
    """Get a single system setting value by key."""
    async with get_db(write=False) as db:
        cursor = await db.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_system_setting(key: str, value: str, description: str = "") -> None:
    """Update or insert a system setting."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db(write=True) as db:
        if description:
            await db.execute(
                """
                INSERT INTO system_settings (key, value, description, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, description = excluded.description, updated_at = excluded.updated_at
                """,
                (key, value, description, now_iso),
            )
        else:
            await db.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now_iso),
            )


async def update_system_settings(updates: dict[str, str]) -> None:
    """Batch update multiple system settings."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db(write=True) as db:
        for k, v in updates.items():
            await db.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (k, str(v), now_iso),
            )


# ── AI Audit Logs (Task 1 & Task 3) ──────────────────────────────────

async def save_ai_audit_log(
    status: str,
    category: str,
    label: str,
    confidence: float,
    displacement_pct: float,
    reason: str,
    snapshot_path: str = "",
    boxes_json: str = "[]",
) -> int:
    """Record an AI evaluation attempt (both APPROVED and REJECTED) for auditing."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db(write=True) as db:
        cursor = await db.execute(
            """
            INSERT INTO ai_audit_logs (timestamp, status, category, label, confidence, displacement_pct, reason, snapshot_path, boxes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso, status, category, label, confidence, displacement_pct, reason, snapshot_path, boxes_json),
        )
        return cursor.lastrowid or 0


async def get_recent_ai_audit_logs(limit: int = 50) -> list[dict]:
    """Return the most recent AI verification audit logs."""
    async with get_db(write=False) as db:
        cursor = await db.execute(
            "SELECT * FROM ai_audit_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ── Client Crash Logs (Task 1 & Task 2) ──────────────────────────────

async def save_crash_log(
    device_name: str,
    device_id: str,
    app_version: str,
    error_message: str,
    stacktrace: str,
    screen_state: str = "",
) -> int:
    """Save an Android client crash stacktrace."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db(write=True) as db:
        cursor = await db.execute(
            """
            INSERT INTO crash_logs (timestamp, device_name, device_id, app_version, error_message, stacktrace, screen_state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso, device_name, device_id, app_version, error_message, stacktrace, screen_state),
        )
        return cursor.lastrowid or 0


async def get_recent_crash_logs(limit: int = 50) -> list[dict]:
    """Retrieve recent client crash logs."""
    async with get_db(write=False) as db:
        cursor = await db.execute(
            "SELECT * FROM crash_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
