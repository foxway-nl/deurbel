"""
Foxway Smart Doorbell — Hikvision Port 8000 C-SDK Full Packet Sniffer & Dual Alarm Ingest.

Strict v3.3.0 Architecture:
1. Native C-SDK file logger enabled via NET_DVR_SetLogToFile(3, "/mnt/opslag/deurbel/logs/hcnetsdk_native", False).
2. Exact 28-Byte NET_DVR_SETUPALARM_PARAM struct according to official HCNetSDK.h.
3. Dual Alarm Channel Ingest:
   - byDeployType = 0 (Client / Indoor Station mode, required for Video Intercom events)
   - byDeployType = 1 (Server mode)
4. Passive TCP Listen-Mode via NET_DVR_StartListen_V30 on Port 7200.
5. 100% Raw Buffer Sniffer & Byte-Diffing:
   - Captures 100% of buffer bytes (no 16/64 truncation).
   - Writes all non-motion events and motion diffs to daily black-box JSONL and SQLite sdk_alarm_logs.
   - Triggers doorbell ringing on:
     * COMM_UPLOAD_VIDEO_INTERCOM_EVENT (0x1132) with byte 44 in (1, 0)
     * COMM_ALARM_VIDEO_INTERCOM (0x1133) with byte 44 in (1, 14)
     * COMM_ALARM_V30 (0x4000) / COMM_ALARM_V40 (0x4007) with dwAlarmType == 0 or != 3
     * COMM_ISAPI_ALARM (0x5002 / 0x6009) containing 'ring', 'call', or 'videoIntercom'
"""

from __future__ import annotations

import asyncio
import collections
import ctypes
import datetime
import logging
import os
import sqlite3
import struct
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from app.logger import log_sdk_packet

logger = logging.getLogger("foxway.sdk_alarm")

# Hikvision Alarm Message Commands
COMM_ALARM = 0x1100                          # 4352
COMM_UPLOAD_VIDEO_INTERCOM_EVENT = 0x1132    # 4402
COMM_ALARM_VIDEO_INTERCOM = 0x1133           # 4403
COMM_ALARM_V30 = 0x4000                      # 16384
COMM_ALARM_V40 = 0x4007                      # 16391
COMM_ISAPI_ALARM_V50 = 0x5002                # 20482
COMM_ISAPI_ALARM = 0x6009                    # 24585

COMMAND_NAMES = {
    0x1100: "COMM_ALARM",
    0x1132: "COMM_UPLOAD_VIDEO_INTERCOM_EVENT",
    0x1133: "COMM_ALARM_VIDEO_INTERCOM",
    0x4000: "COMM_ALARM_V30",
    0x4007: "COMM_ALARM_V40",
    0x5002: "COMM_ISAPI_ALARM_V50",
    0x6009: "COMM_ISAPI_ALARM",
}

INTERCOM_EVENT_TYPES = {
    0: "Visitor Call / Ring (Type 0)",
    1: "Visitor Call (Drukknop Deurbel)",
    2: "Call Answered",
    3: "Call Timeout",
    4: "Call Ended",
    5: "Door Unlocked",
    6: "Alarm Ringing",
    14: "Incoming Call",
}

# Official 28-Byte HCNetSDK NET_DVR_SETUPALARM_PARAM struct definition
class NET_DVR_SETUPALARM_PARAM(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("dwSize", ctypes.c_uint32),             # 28 bytes
        ("byLevel", ctypes.c_uint8),             # 0 = High priority voor intercom
        ("byAlarmInfoType", ctypes.c_uint8),     # 1 = New alarm info
        ("byRetAlarmTypeV40", ctypes.c_uint8),   # 1 = V40 alarm support
        ("byRetDevInfoVersion", ctypes.c_uint8), # 1 = CVR alarm info
        ("byRetVQDAlarmType", ctypes.c_uint8),   # 1 = VQD alarm
        ("byFaceAlarmDetection", ctypes.c_uint8),# 1
        ("bySupport", ctypes.c_uint8),           # 0x00 (zodat de camera NIET bepaalde alarmtypes onderdrukt!)
        ("byBrokenNetHttp", ctypes.c_uint8),     # 0
        ("wTaskNo", ctypes.c_uint16),            # 0
        ("byDeployType", ctypes.c_uint8),        # 0 = Client / Indoor Station Mode (ontvangt deurbel-events!)
        ("bySubScribeType", ctypes.c_uint8),     # 0xFF = Subscribe ALL
        ("byRes1", ctypes.c_uint8 * 2),          # 2 bytes padding
        ("byAlarmTypeURL", ctypes.c_uint8),      # 0 = Binary struct mode
        ("byCustomCtrl", ctypes.c_uint8),        # 0
        ("byRes", ctypes.c_uint8 * 8),           # 8 bytes zodat sizeof == 28
    ]

assert ctypes.sizeof(NET_DVR_SETUPALARM_PARAM) == 28, "Struct size MOET exact 28 bytes zijn!"


class SDKAlarmEvent:
    """Represents a logged hardware event from Port 8000 C-SDK."""

    def __init__(
        self,
        command_id: int,
        command_name: str,
        event_type: Optional[int] = None,
        event_name: Optional[str] = None,
        details: str = "",
        action_taken: str = "Logged",
        dw_buf_len: int = 0,
        by_deploy_type: int = 0,
        payload_hex: str = "",
        payload_ascii: str = "",
    ):
        self.timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.command_id = command_id
        self.command_hex = f"0x{command_id:04x}"
        self.command_name = command_name
        self.event_type = event_type
        self.event_name = event_name
        self.details = details
        self.action_taken = action_taken
        self.dw_buf_len = dw_buf_len
        self.by_deploy_type = by_deploy_type
        self.payload_hex = payload_hex
        self.payload_ascii = payload_ascii

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "command_id": self.command_id,
            "command_hex": self.command_hex,
            "command_name": self.command_name,
            "event_type": self.event_type,
            "event_name": self.event_name,
            "details": self.details,
            "action_taken": self.action_taken,
            "dw_buf_len": self.dw_buf_len,
            "by_deploy_type": self.by_deploy_type,
            "payload_hex": self.payload_hex,
            "payload_ascii": self.payload_ascii,
        }


class SDKAlarmListener:
    """
    Dedicated Port 8000 Alarm Channel supervisor and raw packet sniffer.
    Maintains dual open alarm channels and TCP port 7200 listen server.
    """

    def __init__(self):
        self._sdk = None
        self._user_id: int = -1
        self._alarm_handle_deploy0: int = -1
        self._alarm_handle_deploy1: int = -1
        self._listen_handle: int = -1
        self._callback_ref = None  # Prevent Python GC on C callback pointer
        self._cb_ref = None
        self._running: bool = False
        self._supervisor_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._channel_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._recent_events: Deque[SDKAlarmEvent] = collections.deque(maxlen=100)
        self._last_trigger_ts: float = 0.0
        self._last_motion_payload: Optional[bytes] = None

    @property
    def is_connected(self) -> bool:
        return (self._alarm_handle_deploy0 >= 0 or self._alarm_handle_deploy1 >= 0) and self._user_id >= 0

    @property
    def alarm_handle(self) -> int:
        return self._alarm_handle_deploy0 if self._alarm_handle_deploy0 >= 0 else self._alarm_handle_deploy1

    @property
    def _alarm_handle(self) -> int:
        return self._alarm_handle_deploy0 if self._alarm_handle_deploy0 >= 0 else self._alarm_handle_deploy1

    def get_recent_events(self) -> List[Dict[str, Any]]:
        with self._events_lock:
            return [ev.to_dict() for ev in reversed(self._recent_events)]

    def _insert_db_log(
        self,
        command_id: int,
        command_hex: str,
        command_name: str,
        event_type: Optional[int],
        event_name: Optional[str],
        details: str,
        action_taken: str,
        dw_buf_len: int,
        by_deploy_type: int,
        payload_hex: str,
        payload_ascii: str,
    ) -> None:
        try:
            from app.database import get_db_path
            db_path = get_db_path()
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO sdk_alarm_logs (
                        timestamp, command_id, command_hex, command_name,
                        event_type, event_name, details, dw_buf_len,
                        by_deploy_type, payload_hex, payload_ascii, action_taken
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                        command_id,
                        command_hex,
                        command_name,
                        event_type,
                        event_name,
                        details,
                        dw_buf_len,
                        by_deploy_type,
                        payload_hex,
                        payload_ascii,
                        action_taken,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug("Failed saving sdk_alarm_log to DB: %s", e)

    def _record_event(
        self,
        command_id: int,
        event_type: Optional[int] = None,
        event_name: Optional[str] = None,
        details: str = "",
        action_taken: str = "Logged",
        dw_buf_len: int = 0,
        by_deploy_type: int = 0,
        payload_hex: str = "",
        payload_ascii: str = "",
    ) -> SDKAlarmEvent:
        cmd_name = COMMAND_NAMES.get(command_id, f"UNKNOWN_COMMAND_{command_id}")
        ev = SDKAlarmEvent(
            command_id=command_id,
            command_name=cmd_name,
            event_type=event_type,
            event_name=event_name,
            details=details,
            action_taken=action_taken,
            dw_buf_len=dw_buf_len,
            by_deploy_type=by_deploy_type,
            payload_hex=payload_hex,
            payload_ascii=payload_ascii,
        )
        with self._events_lock:
            self._recent_events.append(ev)

        # Write to daily black-box JSONL
        log_sdk_packet(
            command_id=command_id,
            command_hex=f"0x{command_id:04x}",
            command_name=cmd_name,
            dw_buf_len=dw_buf_len,
            by_deploy_type=by_deploy_type,
            payload_hex=payload_hex,
            payload_ascii=payload_ascii,
            action_taken=action_taken,
            event_type=event_type,
            event_name=event_name,
        )

        # Write to SQLite sdk_alarm_logs table in background
        threading.Thread(
            target=self._insert_db_log,
            args=(
                command_id,
                f"0x{command_id:04x}",
                cmd_name,
                event_type,
                event_name,
                details,
                action_taken,
                dw_buf_len,
                by_deploy_type,
                payload_hex,
                payload_ascii,
            ),
            daemon=True,
        ).start()

        return ev

    def _dispatch_ring_if_debounced(
        self,
        command_id: int,
        event_type: Optional[int],
        event_name: Optional[str],
        details: str,
        dw_buf_len: int = 0,
        by_deploy_type: int = 0,
        payload_hex: str = "",
        payload_ascii: str = "",
    ) -> None:
        now = time.time()
        action = "Logged"
        if (now - self._last_trigger_ts) >= 5.0:
            self._last_trigger_ts = now
            action = "Triggered Doorbell Ringing"
            logger.info("🚨 [Port 8000 SDK Alarm] Ring trigger dispatched to Call State Machine!")
            if self._loop and self._loop.is_running():
                try:
                    from app.main import call_sm
                    asyncio.run_coroutine_threadsafe(call_sm.ring(trigger_source="CSDK_PORT_8000"), self._loop)
                except Exception as e:
                    logger.error("Failed to dispatch ring to call_sm: %s", e)
        else:
            action = "Debounced (<5s since last trigger)"
            logger.info("Doorbell ring debounced (<5s)")

        self._record_event(
            command_id=command_id,
            event_type=event_type,
            event_name=event_name,
            details=details,
            action_taken=action,
            dw_buf_len=dw_buf_len,
            by_deploy_type=by_deploy_type,
            payload_hex=payload_hex,
            payload_ascii=payload_ascii,
        )

    def _c_alarm_callback(self, lCommand: int, pAlarmer: Any, pAlarmInfo: Any, dwBufLen: int, pUser: Any) -> bool:
        """
        Native C callback executed by HCNetSDK worker thread upon hardware event.
        Captures 100% of buffer bytes and logs payload hex + ASCII.
        """
        try:
            cmd_name = COMMAND_NAMES.get(lCommand, f"0x{lCommand:04x}")
            logger.info("⚡ [Port 8000 C-SDK Callback] Command 0x%04x (%s), dwBufLen=%d", lCommand, cmd_name, dwBufLen)
            raw_bytes = b""
            if pAlarmInfo and dwBufLen > 0:
                try:
                    raw_bytes = ctypes.string_at(pAlarmInfo, min(dwBufLen, 65536))
                except Exception as ex:
                    logger.debug("Failed reading alarm buffer: %s", ex)

            payload_hex = raw_bytes.hex() if raw_bytes else ""
            payload_ascii = "".join([chr(b) if 32 <= b <= 126 else "." for b in raw_bytes[:128]]) if raw_bytes else ""

            # Check if pAlarmer provides byDeployType
            by_deploy_type = 0
            if pAlarmer:
                try:
                    # In NET_EALARM_ALARMINFO or NET_DVR_ALARMER, byte offset 12/16 can hold deploy type
                    pass
                except Exception:
                    pass

            # Geval 1: COMM_ALARM_V30 (0x4000) of COMM_ALARM_V40 (0x4007)
            if lCommand in (COMM_ALARM_V30, COMM_ALARM_V40):
                dwAlarmType = -1
                if len(raw_bytes) >= 4:
                    dwAlarmType = struct.unpack("<I", raw_bytes[:4])[0]

                # Als dwAlarmType == 3 (Motion Detection): Byte-Diffing
                if dwAlarmType == 3:
                    # Vergelijk de 268 bytes met de vorige payload (negeer struTime timestamps bytes 4..12)
                    has_diff = False
                    if self._last_motion_payload is None or len(raw_bytes) != len(self._last_motion_payload):
                        has_diff = True
                    else:
                        # Compare body beyond timestamp
                        body_now = raw_bytes[12:] if len(raw_bytes) > 12 else raw_bytes
                        body_last = self._last_motion_payload[12:] if len(self._last_motion_payload) > 12 else self._last_motion_payload
                        if body_now != body_last:
                            has_diff = True

                    self._last_motion_payload = raw_bytes
                    if has_diff:
                        logger.debug("Motion alarm packet variation detected (0x4000), logging diff payload (%d bytes)", len(raw_bytes))
                        log_sdk_packet(
                            command_id=lCommand,
                            command_hex=f"0x{lCommand:04x}",
                            command_name="COMM_ALARM_V30",
                            dw_buf_len=dwBufLen,
                            by_deploy_type=by_deploy_type,
                            payload_hex=payload_hex,
                            payload_ascii=payload_ascii,
                            action_taken="Logged Motion Diff",
                            event_type=dwAlarmType,
                            event_name="Motion_Diff",
                        )
                    return True

                # dwAlarmType == 0 (Button/Semaphore IO) of elk ander non-motion alarm:
                logger.info("🚨 [Port 8000 SDK Alarm] COMM_ALARM_V30 non-motion alarm (dwAlarmType=%d)! Triggering Doorbell Ringing...", dwAlarmType)
                self._dispatch_ring_if_debounced(
                    command_id=lCommand,
                    event_type=dwAlarmType,
                    event_name=f"AlarmV30_Type_{dwAlarmType}",
                    details=f"Hardware Alarm 0x{lCommand:04x} (dwAlarmType={dwAlarmType}), payload_hex={payload_hex}",
                    dw_buf_len=dwBufLen,
                    by_deploy_type=by_deploy_type,
                    payload_hex=payload_hex,
                    payload_ascii=payload_ascii,
                )
                return True

            # Geval 2: COMM_UPLOAD_VIDEO_INTERCOM_EVENT (0x1132)
            if lCommand == COMM_UPLOAD_VIDEO_INTERCOM_EVENT:
                event_type = raw_bytes[44] if len(raw_bytes) > 44 else (raw_bytes[12] if len(raw_bytes) > 12 else (raw_bytes[0] if raw_bytes else 0))
                event_name = INTERCOM_EVENT_TYPES.get(event_type, f"IntercomEvent_{event_type}")
                logger.info("🔔 [Port 8000 SDK Alarm] 0x1132 COMM_UPLOAD_VIDEO_INTERCOM_EVENT (byte 44 event_type=%d, %s) bufLen=%d",
                            event_type, event_name, dwBufLen)
                # raw_bytes[44] in (1, 0) is visitor call / ringing
                if event_type in (1, 0):
                    logger.info("🚨 [Port 8000 SDK Alarm] MATCHED VISITOR CALL (event_type=%d)! Triggering Doorbell Ringing...", event_type)
                    self._dispatch_ring_if_debounced(
                        command_id=lCommand,
                        event_type=event_type,
                        event_name=event_name,
                        details=f"Visitor Call (byEventType={event_type} at byte 44), payload_hex={payload_hex}",
                        dw_buf_len=dwBufLen,
                        by_deploy_type=by_deploy_type,
                        payload_hex=payload_hex,
                        payload_ascii=payload_ascii,
                    )
                else:
                    self._record_event(
                        command_id=lCommand,
                        event_type=event_type,
                        event_name=event_name,
                        details=f"Intercom Event (byEventType={event_type} {event_name}, Not visitor call), payload_hex={payload_hex}",
                        action_taken="Logged (Intercom Event)",
                        dw_buf_len=dwBufLen,
                        by_deploy_type=by_deploy_type,
                        payload_hex=payload_hex,
                        payload_ascii=payload_ascii,
                    )
                return True

            # Geval 3: COMM_ALARM_VIDEO_INTERCOM (0x1133)
            if lCommand == COMM_ALARM_VIDEO_INTERCOM:
                alarm_type = raw_bytes[44] if len(raw_bytes) > 44 else (raw_bytes[0] if raw_bytes else 0)
                event_name = f"VideoIntercomAlarm_{alarm_type}"
                if alarm_type == 1:
                    event_name = "Doorbell Ringing (Type 1)"
                elif alarm_type == 14:
                    event_name = "Incoming Call (Type 14)"
                logger.info("🔔 [Port 8000 SDK Alarm] 0x1133 COMM_ALARM_VIDEO_INTERCOM (byte 44 alarm_type=%d, %s) bufLen=%d",
                            alarm_type, event_name, dwBufLen)
                if alarm_type in (1, 14):
                    logger.info("🚨 [Port 8000 SDK Alarm] MATCHED VIDEO INTERCOM ALARM (alarm_type=%d)! Triggering Doorbell Ringing...", alarm_type)
                    self._dispatch_ring_if_debounced(
                        command_id=lCommand,
                        event_type=alarm_type,
                        event_name=event_name,
                        details=f"Video Intercom Doorbell Ring (byAlarmType={alarm_type} at byte 44), payload_hex={payload_hex}",
                        dw_buf_len=dwBufLen,
                        by_deploy_type=by_deploy_type,
                        payload_hex=payload_hex,
                        payload_ascii=payload_ascii,
                    )
                else:
                    self._record_event(
                        command_id=lCommand,
                        event_type=alarm_type,
                        event_name=event_name,
                        details=f"Video Intercom Alarm (byAlarmType={alarm_type}), payload_hex={payload_hex}",
                        action_taken="Logged (Intercom Alarm)",
                        dw_buf_len=dwBufLen,
                        by_deploy_type=by_deploy_type,
                        payload_hex=payload_hex,
                        payload_ascii=payload_ascii,
                    )
                return True

            # Geval 4: COMM_ISAPI_ALARM (0x5002 / 0x6009)
            if lCommand in (COMM_ISAPI_ALARM_V50, COMM_ISAPI_ALARM):
                logger.info("🔔 [Port 8000 SDK Alarm] ISAPI Alarm received: 0x%04x (%d bytes)", lCommand, len(raw_bytes))
                lower_payload = payload_ascii.lower()
                if "ring" in lower_payload or "call" in lower_payload or "videointercom" in lower_payload:
                    logger.info("🚨 [Port 8000 SDK Alarm] MATCHED ISAPI RING KEYWORD! Triggering Doorbell Ringing...")
                    self._dispatch_ring_if_debounced(
                        command_id=lCommand,
                        event_type=1,
                        event_name="ISAPI_Ring",
                        details=f"ISAPI Ring Event, payload_hex={payload_hex}",
                        dw_buf_len=dwBufLen,
                        by_deploy_type=by_deploy_type,
                        payload_hex=payload_hex,
                        payload_ascii=payload_ascii,
                    )
                else:
                    self._record_event(
                        command_id=lCommand,
                        event_type=None,
                        event_name=None,
                        details=f"ISAPI Alarm Event, payload_hex={payload_hex}",
                        action_taken="Logged (ISAPI Alarm)",
                        dw_buf_len=dwBufLen,
                        by_deploy_type=by_deploy_type,
                        payload_hex=payload_hex,
                        payload_ascii=payload_ascii,
                    )
                return True

            # Geval 5: Elk ander onbekend lCommand
            logger.info("ℹ️ [Port 8000 SDK Alarm] Hardware command 0x%04x (%s), bufLen=%d", lCommand, cmd_name, dwBufLen)
            self._record_event(
                command_id=lCommand,
                event_type=None,
                event_name=None,
                details=f"Hardware Command 0x{lCommand:04x} ({cmd_name}), payload_hex={payload_hex}",
                action_taken="Logged (Raw Command)",
                dw_buf_len=dwBufLen,
                by_deploy_type=by_deploy_type,
                payload_hex=payload_hex,
                payload_ascii=payload_ascii,
            )
            return True

        except Exception as e:
            logger.exception("Error in SDK alarm callback: %s", e)

        return True

    def _setup_channel_sync(self) -> bool:
        """Synchronously log in, enable C-SDK file logging, start listen mode, and open dual alarm channels."""
        with self._channel_lock:
            try:
                from app.audio_bridge import _get_sdk, _get_credentials
                from HCNetSDK.SDK_Callback import MSGCallBack_V31

                if self._sdk is None:
                    self._sdk = _get_sdk()

                # Enable native C-SDK file logger (level 3 = DEBUG)
                native_log_dir = "/mnt/opslag/deurbel/logs/hcnetsdk_native"
                os.makedirs(native_log_dir, exist_ok=True)
                try:
                    self._sdk.NET_DVR_SetLogToFile(3, native_log_dir.encode("utf-8"), False)
                    logger.info("Native C-SDK file logging enabled -> %s", native_log_dir)
                except Exception as log_ex:
                    logger.debug("NET_DVR_SetLogToFile note: %s", log_ex)

                # Clean up previous handles if any
                if self._alarm_handle_deploy0 >= 0:
                    try:
                        self._sdk.NET_DVR_CloseAlarmChan_V30(self._alarm_handle_deploy0)
                    except Exception:
                        pass
                    self._alarm_handle_deploy0 = -1

                if self._alarm_handle_deploy1 >= 0:
                    try:
                        self._sdk.NET_DVR_CloseAlarmChan_V30(self._alarm_handle_deploy1)
                    except Exception:
                        pass
                    self._alarm_handle_deploy1 = -1

                if self._listen_handle >= 0:
                    try:
                        self._sdk.NET_DVR_StopListen_V30(self._listen_handle)
                    except Exception:
                        pass
                    self._listen_handle = -1

                if self._user_id >= 0:
                    try:
                        self._sdk.NET_DVR_Logout(self._user_id)
                    except Exception:
                        pass
                    self._user_id = -1

                # 1. Login to Camera
                class NET_DVR_DEVICEINFO_V30(ctypes.Structure):
                    _fields_ = [("sSerialNumber", ctypes.c_byte * 48)] + [("res", ctypes.c_byte * 96)]

                dev_info = NET_DVR_DEVICEINFO_V30()
                self._sdk.NET_DVR_Login_V30.restype = ctypes.c_int32
                self._sdk.NET_DVR_Login_V30.argtypes = [
                    ctypes.c_char_p, ctypes.c_uint16, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p
                ]

                cam_ip, cam_port, cam_user, cam_pass = _get_credentials()
                uid = self._sdk.NET_DVR_Login_V30(
                    cam_ip.encode("utf-8"),
                    cam_port,
                    cam_user.encode("utf-8"),
                    cam_pass.encode("utf-8"),
                    ctypes.byref(dev_info)
                )

                if uid < 0:
                    err = self._sdk.NET_DVR_GetLastError()
                    logger.warning("SDK alarm channel login failed: %d", err)
                    return False

                self._user_id = uid

                # 2. Register Callback
                CB_TYPE = MSGCallBack_V31
                self._callback_ref = CB_TYPE(self._c_alarm_callback)
                self._cb_ref = self._callback_ref
                self._sdk.NET_DVR_SetDVRMessageCallBack_V31.restype = ctypes.c_bool
                self._sdk.NET_DVR_SetDVRMessageCallBack_V31.argtypes = [CB_TYPE, ctypes.c_void_p]

                if not self._sdk.NET_DVR_SetDVRMessageCallBack_V31(self._callback_ref, None):
                    err = self._sdk.NET_DVR_GetLastError()
                    logger.warning("NET_DVR_SetDVRMessageCallBack_V31 failed: %d", err)
                    return False

                # 3. Start Passive TCP Listen-Mode on Port 7200
                try:
                    self._sdk.NET_DVR_StartListen_V30.restype = ctypes.c_int32
                    self._sdk.NET_DVR_StartListen_V30.argtypes = [ctypes.c_char_p, ctypes.c_uint16, CB_TYPE, ctypes.c_void_p]
                    lh = self._sdk.NET_DVR_StartListen_V30(b"0.0.0.0", 7200, self._callback_ref, None)
                    if lh >= 0:
                        self._listen_handle = lh
                        logger.info("⚡ [Port 8000 SDK Alarm] Passive TCP Listen active on 0.0.0.0:7200 (handle=%d)", lh)
                except Exception as listen_ex:
                    logger.debug("NET_DVR_StartListen_V30 note: %s", listen_ex)

                # 4. Open Dual Alarm Channels (deployType 0 = Client/Intercom, deployType 1 = Server)
                self._sdk.NET_DVR_SetupAlarmChan_V41.restype = ctypes.c_int32
                self._sdk.NET_DVR_SetupAlarmChan_V41.argtypes = [
                    ctypes.c_int32, ctypes.POINTER(NET_DVR_SETUPALARM_PARAM)
                ]

                # Channel 0: byDeployType = 0 (Client / Indoor Station mode for Intercom events)
                setup0 = NET_DVR_SETUPALARM_PARAM()
                setup0.dwSize = ctypes.sizeof(NET_DVR_SETUPALARM_PARAM)
                setup0.byLevel = 0             # 0 = High priority voor intercom
                setup0.byAlarmInfoType = 1
                setup0.byRetAlarmTypeV40 = 1
                setup0.byDeployType = 0        # Client / Indoor Station mode
                setup0.bySubScribeType = 0xFF   # Subscribe ALL

                h0 = self._sdk.NET_DVR_SetupAlarmChan_V41(uid, ctypes.byref(setup0))
                if h0 >= 0:
                    self._alarm_handle_deploy0 = h0
                    logger.info("⚡ [Port 8000 SDK Alarm] Channel 0 (byDeployType=0 Client/Intercom) active (handle=%d)", h0)

                # Channel 1: byDeployType = 1 (Server mode)
                setup1 = NET_DVR_SETUPALARM_PARAM()
                setup1.dwSize = ctypes.sizeof(NET_DVR_SETUPALARM_PARAM)
                setup1.byLevel = 1
                setup1.byAlarmInfoType = 1
                setup1.byRetAlarmTypeV40 = 1
                setup1.byDeployType = 1        # Server mode
                setup1.bySubScribeType = 0xFF   # Subscribe ALL

                h1 = self._sdk.NET_DVR_SetupAlarmChan_V41(uid, ctypes.byref(setup1))
                if h1 >= 0:
                    self._alarm_handle_deploy1 = h1
                    logger.info("⚡ [Port 8000 SDK Alarm] Channel 1 (byDeployType=1 Server) active (handle=%d)", h1)

                if self._alarm_handle_deploy0 < 0 and self._alarm_handle_deploy1 < 0:
                    err = self._sdk.NET_DVR_GetLastError()
                    logger.warning("NET_DVR_SetupAlarmChan_V41 failed for both deploy types: %d", err)
                    return False

                logger.info("⚡ [Port 8000 SDK Alarm] DUAL ALARM INGEST ACTIVE (user_id=%d, h0=%d, h1=%d, listen=%d)",
                            uid, self._alarm_handle_deploy0, self._alarm_handle_deploy1, self._listen_handle)

                self._record_event(
                    command_id=0,
                    details=f"Dual SDK Alarm Channels opened on Port 8000 (h0={self._alarm_handle_deploy0}, h1={self._alarm_handle_deploy1}, listen={self._listen_handle})",
                    action_taken="Connected",
                )
                return True

            except Exception as e:
                logger.error("Exception setting up Port 8000 alarm channels: %s", e)
                return False

    async def _supervisor_loop(self) -> None:
        """Continuous background monitor that maintains connection and auto-reconnects."""
        while self._running:
            try:
                if not self.is_connected:
                    logger.info("Connecting Port 8000 SDK Dual Alarm Channels...")
                    success = await asyncio.to_thread(self._setup_channel_sync)
                    if not success:
                        logger.warning("Port 8000 SDK Alarm Channel setup failed, retrying in 10s...")
                        await asyncio.sleep(10.0)
                        continue
                await asyncio.sleep(15.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in SDK alarm supervisor: %s", e)
                await asyncio.sleep(10.0)

    async def start(self) -> None:
        """Start the SDK alarm listener service."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        logger.info("Hikvision Port 8000 Full Sniffer & Dual Alarm Listener started")

    async def stop(self) -> None:
        """Stop listener and close alarm channels."""
        self._running = False
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor_task = None

        await asyncio.to_thread(self._close_channel_sync)
        logger.info("Hikvision Port 8000 Hardware Alarm Listener stopped")

    def _close_channel_sync(self) -> None:
        with self._channel_lock:
            if self._sdk is not None:
                if self._alarm_handle_deploy0 >= 0:
                    try:
                        self._sdk.NET_DVR_CloseAlarmChan_V30(self._alarm_handle_deploy0)
                    except Exception:
                        pass
                    self._alarm_handle_deploy0 = -1
                if self._alarm_handle_deploy1 >= 0:
                    try:
                        self._sdk.NET_DVR_CloseAlarmChan_V30(self._alarm_handle_deploy1)
                    except Exception:
                        pass
                    self._alarm_handle_deploy1 = -1
                if self._listen_handle >= 0:
                    try:
                        self._sdk.NET_DVR_StopListen_V30(self._listen_handle)
                    except Exception:
                        pass
                    self._listen_handle = -1
                if self._user_id >= 0:
                    try:
                        self._sdk.NET_DVR_Logout(self._user_id)
                    except Exception:
                        pass
                    self._user_id = -1


sdk_alarm_listener = SDKAlarmListener()
