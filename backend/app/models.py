"""
Foxway Smart Doorbell — Pydantic schemas & SQLite persistence.

Stores paired family devices, call logs, and clip metadata in a single
lightweight SQLite database on the NAS volume.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import aiosqlite
from pydantic import BaseModel, Field

from app.config import settings

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
    lan_url: str
    wan_url: str
    go2rtc_lan_ws: str
    go2rtc_wan_ws: str


class UserInfo(BaseModel):
    user_id: str
    user_name: str
    device_name: str
    android_version: str = ""
    connection_type: str = ""
    online: bool = False
    last_seen: str = ""


# — Ring —

class RingEvent(BaseModel):
    call_id: str
    state: CallState
    snapshot_url: str
    timestamp: str


# — Claim —

class ClaimRequest(BaseModel):
    user_id: str
    user_name: str
    call_id: Optional[str] = None


class ClaimResponse(BaseModel):
    status: str
    webrtc_src: Optional[str] = None
    answered_by: Optional[str] = None


# — Quick-reply —

class QuickReplyRequest(BaseModel):
    reply_id: str = Field(..., examples=["pakketje", "kom_eraan"])
    user_name: str
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
    user_id       TEXT PRIMARY KEY,
    user_name     TEXT NOT NULL,
    device_name   TEXT NOT NULL,
    fcm_token     TEXT NOT NULL DEFAULT '',
    jwt_token     TEXT NOT NULL,
    paired_at     TEXT NOT NULL,
    last_seen     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS call_logs (
    call_id       TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    state         TEXT NOT NULL DEFAULT 'RINGING',
    answered_by   TEXT,
    answered_by_user_id TEXT,
    snapshot_path TEXT,
    clip_path     TEXT,
    duration      REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS clips (
    clip_id       TEXT PRIMARY KEY,
    call_id       TEXT,
    created_at    TEXT NOT NULL,
    duration      REAL DEFAULT 0.0,
    snapshot_path TEXT,
    video_path    TEXT NOT NULL,
    answered_by   TEXT,
    starred       INTEGER DEFAULT 0,
    FOREIGN KEY (call_id) REFERENCES call_logs(call_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,
    category      TEXT NOT NULL,
    title         TEXT NOT NULL,
    clip_id       TEXT DEFAULT '',
    snapshot_url  TEXT DEFAULT '',
    created_at    TEXT NOT NULL
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
"""


async def init_db() -> None:
    """Create tables if they don't exist and run schema migrations."""
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.executescript(_SCHEMA_SQL)
        # Migration: ensure last_seen, device_id, android_version, connection_type columns exist
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN last_seen TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN device_id TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN android_version TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN connection_type TEXT DEFAULT ''")
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

        # Seed default system_settings
        default_settings = [
            ("person_conf_threshold", "0.52", "Minimale betrouwbaarheid voor personen (0.1 - 1.0)"),
            ("vehicle_conf_threshold", "0.55", "Minimale betrouwbaarheid voor voertuigen (0.1 - 1.0)"),
            ("animal_conf_threshold", "0.52", "Minimale betrouwbaarheid voor dieren (0.1 - 1.0)"),
            ("min_person_displacement_pct", "6.0", "Minimale verplaatsing persoon (% schermbreedte)"),
            ("min_vehicle_displacement_pct", "12.0", "Minimale verplaatsing voertuig (% schermbreedte)"),
            ("verify_frames_required", "3", "Aantal opeenvolgende frames voor traject-verificatie"),
            ("pre_roll_seconds", "6", "Pre-roll seconden uit RAM buffer"),
            ("post_roll_seconds", "5", "Post-roll seconden na object verdwijnt"),
            ("max_clip_seconds", "25", "Maximale opnameduur bij AI detectie (sec)"),
            ("event_cooldown_seconds", "20", "Afkoelperiode tussen opeenvolgende meldingen (sec)"),
            ("ring_ding_hz", "728", "Frequentie Ding toonbel (Hz)"),
            ("ring_dong_hz", "580", "Frequentie Dong toonbel (Hz)"),
            ("ring_sharpness", "4.5", "Scherpte / resonantiefactor toonbel"),
            ("exclusion_zones", "[]", "JSON lijst van gemaskeerde zones [x1, y1, x2, y2]"),
            ("admin_password", "foxway2024", "Beheerderswachtwoord voor /admin"),
        ]
        now_iso = datetime.now(timezone.utc).isoformat()
        for k, v, desc in default_settings:
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO system_settings (key, value, description, updated_at) VALUES (?, ?, ?, ?)",
                    (k, v, desc, now_iso),
                )
            except Exception:
                pass

        # Cleanup orphan events without a valid clip_id in clips table and deduplicate to strict 1-to-1 coupling
        try:
            await db.execute("""
                DELETE FROM events
                WHERE clip_id NOT IN (SELECT clip_id FROM clips)
                   OR clip_id IS NULL
                   OR clip_id = ''
            """)
            await db.execute("""
                DELETE FROM events
                WHERE id NOT IN (
                    SELECT max(id)
                    FROM events
                    GROUP BY clip_id
                )
            """)
        except Exception:
            pass

        await db.commit()


# ── Device helpers ────────────────────────────────────────────────────

async def find_existing_device(
    device_id: str = "",
    user_name: str = "",
    device_name: str = "",
) -> Optional[dict]:
    """Find existing device by unique hardware ID or matching user and device name."""
    clean_user = (user_name or "").strip().lower()
    clean_dev_id = (device_id or "").strip()
    clean_dev_name = (device_name or "").strip().lower()
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(_DB_PATH) as db:
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
    async with aiosqlite.connect(_DB_PATH) as db:
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
        await db.commit()


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
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("DELETE FROM devices WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0


async def update_device_last_seen(user_id: str, last_seen: Optional[str] = None) -> None:
    if not last_seen:
        last_seen = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE devices SET last_seen = ? WHERE user_id = ?",
            (last_seen, user_id),
        )
        await db.commit()


async def get_all_devices() -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM devices")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_device(user_id: str) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM devices WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_fcm_token(user_id: str, fcm_token: str) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE devices SET fcm_token = ? WHERE user_id = ?",
            (fcm_token, user_id),
        )
        await db.commit()


# ── Call log helpers ──────────────────────────────────────────────────

async def save_call_log(
    call_id: str,
    state: str,
    snapshot_path: str = "",
    clip_path: str = "",
) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
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
        await db.commit()


async def update_call_log(call_id: str, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [call_id]
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            f"UPDATE call_logs SET {set_clause} WHERE call_id = ?",
            values,
        )
        await db.commit()


async def get_call_log(call_id: str) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
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
) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO clips (clip_id, call_id, created_at, duration, snapshot_path, video_path, answered_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clip_id) DO UPDATE SET
                duration = excluded.duration,
                snapshot_path = COALESCE(NULLIF(excluded.snapshot_path, ''), clips.snapshot_path),
                answered_by = COALESCE(NULLIF(excluded.answered_by, ''), clips.answered_by)
            """,
            (clip_id, call_id, datetime.now(timezone.utc).isoformat(), duration, snapshot_path, video_path, answered_by),
        )
        await db.commit()


async def list_clips(limit: int = 100, offset: int = 0) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM clips ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_clip(clip_id: str) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM clips WHERE clip_id = ?", (clip_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_clip(clip_id: str) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("DELETE FROM clips WHERE clip_id = ?", (clip_id,))
        await db.commit()
        return cursor.rowcount > 0


async def update_clip(clip_id: str, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [clip_id]
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            f"UPDATE clips SET {set_clause} WHERE clip_id = ?",
            values,
        )
        await db.commit()


async def get_expired_clips(retention_days: int) -> list[dict]:
    """Return clips older than *retention_days* that are not starred."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM clips WHERE starred = 0 ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_setting(key: str, default: str = "") -> str:
    """Retrieve an application setting by key."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    """Save or update an application setting."""
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def save_event(
    event_type: str,
    category: str,
    title: str,
    clip_id: str = "",
    snapshot_url: str = "",
) -> dict:
    """Record an event (ring or confirmed motion) in the events table."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO events (type, category, title, clip_id, snapshot_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_type, category, title, clip_id, snapshot_url, now_iso),
        )
        await db.commit()
        event_id = cursor.lastrowid
        return {
            "id": event_id,
            "type": event_type,
            "category": category,
            "title": title,
            "clip_id": clip_id,
            "snapshot_url": snapshot_url,
            "created_at": now_iso,
        }


async def get_events_since(since_id: int = 0, limit: int = 50, order: str = "asc") -> list[dict]:
    """
    Retrieve new events with strict clip coupling or recent active event window.
    If order == 'desc' or (since_id == 0 and limit == 1), returns the latest events.
    Permits events coupled to clips table OR events created in the last 180 seconds
    (so immediate push notifications are visible on phone before 25s video finishes recording).
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("SELECT key, value FROM system_settings")
        rows = await cursor.fetchall()
        return {r[0]: r[1] for r in rows}


async def get_system_setting(key: str, default: str = "") -> str:
    """Get a single system setting value by key."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_system_setting(key: str, value: str, description: str = "") -> None:
    """Update or insert a system setting."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
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
        await db.commit()


async def update_system_settings(updates: dict[str, str]) -> None:
    """Batch update multiple system settings."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        for k, v in updates.items():
            await db.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (k, str(v), now_iso),
            )
        await db.commit()


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
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO ai_audit_logs (timestamp, status, category, label, confidence, displacement_pct, reason, snapshot_path, boxes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso, status, category, label, confidence, displacement_pct, reason, snapshot_path, boxes_json),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_recent_ai_audit_logs(limit: int = 50) -> list[dict]:
    """Return the most recent AI verification audit logs."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO crash_logs (timestamp, device_name, device_id, app_version, error_message, stacktrace, screen_state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso, device_name, device_id, app_version, error_message, stacktrace, screen_state),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_recent_crash_logs(limit: int = 50) -> list[dict]:
    """Retrieve recent client crash logs."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM crash_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


