"""
Foxway Smart Doorbell — Central Black-Box JSONL Logging Subsystem.

Writes all server, C-SDK, Shazam, AI, and Android client events into
chronological structured daily JSONL files:
/mnt/opslag/deurbel/logs/YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("foxway.blackbox")
LOG_DIR = "/mnt/opslag/deurbel/logs"
_log_lock = threading.Lock()


def get_today_log_path() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"{today}.jsonl")


def log_blackbox(
    category: str,
    event: str,
    level: str = "INFO",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Appends a structured JSON entry to today's black-box log.
    Thread-safe and failsafe.
    """
    now = datetime.datetime.now()
    record = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "category": category,
        "event": event,
        "level": level.upper(),
        "data": data or {},
    }

    try:
        log_path = get_today_log_path()
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _log_lock:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        logger.error("Failed writing black-box log: %s", e)

    return record


def log_sdk_packet(
    command_id: int,
    command_hex: str,
    command_name: str,
    dw_buf_len: int,
    by_deploy_type: int,
    payload_hex: str,
    payload_ascii: str = "",
    action_taken: str = "Logged",
    event_type: Optional[int] = None,
    event_name: Optional[str] = None,
) -> None:
    log_blackbox(
        category="C-SDK",
        event=command_name,
        level="INFO" if "Triggered" not in action_taken else "WARNING",
        data={
            "command_id": command_id,
            "command_hex": command_hex,
            "command_name": command_name,
            "dw_buf_len": dw_buf_len,
            "by_deploy_type": by_deploy_type,
            "event_type": event_type,
            "event_name": event_name,
            "payload_hex": payload_hex,
            "payload_ascii": payload_ascii,
            "action_taken": action_taken,
        },
    )


def log_shazam_event(
    ncc: float,
    threshold: float,
    matched: bool,
    f_tone1: float,
    f_tone2: float,
    details: str = "",
) -> None:
    log_blackbox(
        category="SHAZAM",
        event="ChimeAnalysis",
        level="WARNING" if matched else "DEBUG",
        data={
            "ncc": round(ncc, 4),
            "threshold": threshold,
            "matched": matched,
            "f_tone1": round(f_tone1, 1),
            "f_tone2": round(f_tone2, 1),
            "details": details,
        },
    )


def log_ai_event(
    category: str,
    label: str,
    confidence: float,
    displacement_pct: float,
    silent: bool,
    action: str,
    details: str = "",
) -> None:
    log_blackbox(
        category="AI",
        event="Detection",
        level="INFO",
        data={
            "category": category,
            "label": label,
            "confidence": round(confidence, 3),
            "displacement_pct": round(displacement_pct, 4),
            "silent": silent,
            "action": action,
            "details": details,
        },
    )


def log_client_dump(
    device_info: Dict[str, Any],
    screen_state: str,
    recent_logs: str,
    ip: str = "",
) -> None:
    log_blackbox(
        category="CLIENT_DUMP",
        event="StatusDump",
        level="INFO",
        data={
            "ip": ip,
            "device": device_info,
            "screen_state": screen_state,
            "client_logs": recent_logs,
        },
    )

