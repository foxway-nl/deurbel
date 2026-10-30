"""
Foxway Smart Doorbell — Hikvision Port 8000 C-SDK Hardware Alarm Listener.

Listens for hardware doorbell button presses directly on Port 8000 using libhcnetsdk.so:
- NET_DVR_SetDVRMessageCallBack_V31: C callback registration.
- NET_DVR_SetupAlarmChan_V41: Open persistent alarm channel to EZVIZ DB1 Pro (192.168.2.31:8000).
- Intercepts:
    * COMM_UPLOAD_VIDEO_INTERCOM_EVENT (0x1132 = 4402, byEventType == 1: Visitor Call)
    * COMM_ALARM_VIDEO_INTERCOM (0x1133 = 4403)
    * COMM_ALARM_V30 (0x4000 = 16384) / COMM_ALARM (0x1100 = 4352)

Strict Hardware Constraint:
- Port 80 (HTTP / ISAPI / ONVIF) is NOT open on the EZVIZ DB1 Pro.
- All hardware alarms MUST be handled over Port 8000 C-SDK or RTSP Port 554.
"""

from __future__ import annotations

import asyncio
import collections
import ctypes
import datetime
import logging
import struct
import threading
import time
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("foxway.sdk_alarm")

# Hikvision Alarm Message Commands
COMM_ALARM = 0x1100                          # 4352
COMM_UPLOAD_VIDEO_INTERCOM_EVENT = 0x1132    # 4402
COMM_ALARM_VIDEO_INTERCOM = 0x1133           # 4403
COMM_ALARM_V30 = 0x4000                      # 16384
COMM_ALARM_V40 = 0x4007                      # 16391
COMM_ISAPI_ALARM = 0x6009                    # 24585

COMMAND_NAMES = {
    0x1100: "COMM_ALARM",
    0x1132: "COMM_UPLOAD_VIDEO_INTERCOM_EVENT",
    0x1133: "COMM_ALARM_VIDEO_INTERCOM",
    0x4000: "COMM_ALARM_V30",
    0x4007: "COMM_ALARM_V40",
    0x6009: "COMM_ISAPI_ALARM",
}

# Event type names for COMM_UPLOAD_VIDEO_INTERCOM_EVENT
INTERCOM_EVENT_TYPES = {
    1: "Visitor Call (Drukknop Deurbel)",
    2: "Call Answered",
    3: "Call Timeout",
    4: "Call Ended",
    5: "Door Unlocked",
    6: "Alarm Ringing",
}

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
    ):
        self.timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.command_id = command_id
        self.command_hex = f"0x{command_id:04x}"
        self.command_name = command_name
        self.event_type = event_type
        self.event_name = event_name
        self.details = details
        self.action_taken = action_taken

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
        }


class SDKAlarmListener:
    """
    Dedicated Port 8000 Alarm Channel supervisor.
    Maintains an open alarm channel with the EZVIZ DB1 Pro and triggers doorbell ringing.
    """

    def __init__(self):
        self._sdk = None
        self._user_id: int = -1
        self._alarm_handle: int = -1
        self._callback_ref = None  # Prevent Python GC on C callback pointer
        self._cb_ref = None
        self._running: bool = False
        self._supervisor_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._channel_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._recent_events: Deque[SDKAlarmEvent] = collections.deque(maxlen=100)
        self._last_trigger_ts: float = 0.0

    @property
    def is_connected(self) -> bool:
        return self._alarm_handle >= 0 and self._user_id >= 0

    def get_recent_events(self) -> List[Dict[str, Any]]:
        with self._events_lock:
            return [ev.to_dict() for ev in reversed(self._recent_events)]

    def _record_event(
        self,
        command_id: int,
        event_type: Optional[int] = None,
        event_name: Optional[str] = None,
        details: str = "",
        action_taken: str = "Logged",
    ) -> SDKAlarmEvent:
        cmd_name = COMMAND_NAMES.get(command_id, f"UNKNOWN_COMMAND_{command_id}")
        ev = SDKAlarmEvent(
            command_id=command_id,
            command_name=cmd_name,
            event_type=event_type,
            event_name=event_name,
            details=details,
            action_taken=action_taken,
        )
        with self._events_lock:
            self._recent_events.append(ev)
        return ev

    def _dispatch_ring_if_debounced(
        self,
        command_id: int,
        event_type: Optional[int],
        event_name: Optional[str],
        details: str,
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
                    asyncio.run_coroutine_threadsafe(call_sm.ring(), self._loop)
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
        )

    def _c_alarm_callback(self, lCommand: int, pAlarmer: Any, pAlarmInfo: Any, dwBufLen: int, pUser: Any) -> bool:
        """
        Native C callback executed by HCNetSDK worker thread upon hardware event.
        Strict v3.2.0 logic:
        - Geval 1: 0x4000 (COMM_ALARM_V30) / 0x4007: dwAlarmType == 3 (Motion) is COMPLETELY IGNORED.
                   Other dwAlarmType (e.g. 0 = button IO) triggers ring!
        - Geval 2: 0x1132 (COMM_UPLOAD_VIDEO_INTERCOM_EVENT): byte 44 is event_type. If 1 -> trigger ring!
        - Geval 3: 0x1133 (COMM_ALARM_VIDEO_INTERCOM): byte 44 is alarm_type. If 1 or 14 -> trigger ring!
        - Geval 4: Other commands logged with 64 hex bytes in /admin table.
        """
        try:
            cmd_name = COMMAND_NAMES.get(lCommand, f"0x{lCommand:04x}")
            raw_bytes = b""
            if pAlarmInfo and dwBufLen > 0:
                try:
                    raw_bytes = ctypes.string_at(pAlarmInfo, min(dwBufLen, 512))
                except Exception as ex:
                    logger.debug("Failed reading alarm buffer: %s", ex)

            # Geval 1: COMM_ALARM_V30 (0x4000) of COMM_ALARM_V40 (0x4007)
            if lCommand in (COMM_ALARM_V30, COMM_ALARM_V40):
                dwAlarmType = -1
                if len(raw_bytes) >= 4:
                    dwAlarmType = struct.unpack("<I", raw_bytes[:4])[0]
                
                # Als dwAlarmType == 3 (gewone Motion Detection): NEGEER VOLLEDIG en schrijf dit NIET naar de database/logs
                if dwAlarmType == 3:
                    return True

                payload_hex_64 = raw_bytes[:64].hex() if raw_bytes else ""
                logger.info("🚨 [Port 8000 SDK Alarm] COMM_ALARM_V30 non-motion alarm detected (dwAlarmType=%d)! payload_hex_64=%s",
                            dwAlarmType, payload_hex_64)
                self._dispatch_ring_if_debounced(
                    command_id=lCommand,
                    event_type=dwAlarmType,
                    event_name=f"AlarmV30_Type_{dwAlarmType}",
                    details=f"Hardware Alarm 0x{lCommand:04x} (dwAlarmType={dwAlarmType}), payload_hex={payload_hex_64}",
                )
                return True

            payload_hex_64 = raw_bytes[:64].hex() if raw_bytes else ""

            # Geval 2: COMM_UPLOAD_VIDEO_INTERCOM_EVENT (0x1132)
            if lCommand == COMM_UPLOAD_VIDEO_INTERCOM_EVENT:
                event_type = raw_bytes[44] if len(raw_bytes) > 44 else (raw_bytes[12] if len(raw_bytes) > 12 else (raw_bytes[0] if raw_bytes else 0))
                event_name = INTERCOM_EVENT_TYPES.get(event_type, f"IntercomEvent_{event_type}")
                logger.info("🔔 [Port 8000 SDK Alarm] 0x1132 COMM_UPLOAD_VIDEO_INTERCOM_EVENT (byte 44 event_type=%d, %s) payload_hex_64=%s",
                            event_type, event_name, payload_hex_64)
                if event_type == 1:
                    logger.info("🚨 [Port 8000 SDK Alarm] MATCHED VISITOR CALL (event_type=1)! Triggering Doorbell Ringing...")
                    self._dispatch_ring_if_debounced(
                        command_id=lCommand,
                        event_type=event_type,
                        event_name=event_name,
                        details=f"Visitor Call (byEventType=1 at byte 44), payload_hex={payload_hex_64}",
                    )
                else:
                    self._record_event(
                        command_id=lCommand,
                        event_type=event_type,
                        event_name=event_name,
                        details=f"Intercom Event (byEventType={event_type} {event_name}, Not visitor call), payload_hex={payload_hex_64}",
                        action_taken="Logged (Intercom Event)",
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
                logger.info("🔔 [Port 8000 SDK Alarm] 0x1133 COMM_ALARM_VIDEO_INTERCOM (byte 44 alarm_type=%d, %s) payload_hex_64=%s",
                            alarm_type, event_name, payload_hex_64)
                if alarm_type in (1, 14):
                    logger.info("🚨 [Port 8000 SDK Alarm] MATCHED VIDEO INTERCOM ALARM (alarm_type=%d)! Triggering Doorbell Ringing...", alarm_type)
                    self._dispatch_ring_if_debounced(
                        command_id=lCommand,
                        event_type=alarm_type,
                        event_name=event_name,
                        details=f"Video Intercom Doorbell Ring (byAlarmType={alarm_type} at byte 44), payload_hex={payload_hex_64}",
                    )
                else:
                    self._record_event(
                        command_id=lCommand,
                        event_type=alarm_type,
                        event_name=event_name,
                        details=f"Video Intercom Alarm (byAlarmType={alarm_type}), payload_hex={payload_hex_64}",
                        action_taken="Logged (Intercom Alarm)",
                    )
                return True

            # Geval 4: Elk ander onbekend lCommand dat NIET 0x4000 motion is
            logger.info("ℹ️ [Port 8000 SDK Alarm] Hardware command 0x%04x (%s), payload_hex_64=%s",
                        lCommand, cmd_name, payload_hex_64)
            self._record_event(
                command_id=lCommand,
                event_type=None,
                event_name=None,
                details=f"Hardware Command 0x{lCommand:04x} ({cmd_name}), payload_hex={payload_hex_64}",
                action_taken="Logged (Raw Command)",
            )
            return True

        except Exception as e:
            logger.exception("Error in SDK alarm callback: %s", e)

        return True

    def _setup_channel_sync(self) -> bool:
        """Synchronously log in and open alarm channel via HCNetSDK."""
        with self._channel_lock:
            try:
                from app.audio_bridge import _get_sdk, _get_credentials
                from HCNetSDK.SDK_Struct import NET_DVR_SETUPALARM_PARAM
                from HCNetSDK.SDK_Callback import MSGCallBack_V31

                if self._sdk is None:
                    self._sdk = _get_sdk()

                # Clean up previous handles if any
                if self._alarm_handle >= 0:
                    try:
                        self._sdk.NET_DVR_CloseAlarmChan_V30(self._alarm_handle)
                    except Exception:
                        pass
                    self._alarm_handle = -1

                if self._user_id >= 0:
                    try:
                        self._sdk.NET_DVR_Logout(self._user_id)
                    except Exception:
                        pass
                    self._user_id = -1

                # 1. Login
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

                # 3. Setup Alarm Channel V41
                setup = NET_DVR_SETUPALARM_PARAM()
                setup.dwSize = ctypes.sizeof(NET_DVR_SETUPALARM_PARAM)
                setup.byLevel = 1             # Level 1 priority
                setup.byAlarmInfoType = 1     # Return extended structure
                setup.byDeployType = 1        # Deploy type 1 per spec

                self._sdk.NET_DVR_SetupAlarmChan_V41.restype = ctypes.c_int32
                self._sdk.NET_DVR_SetupAlarmChan_V41.argtypes = [
                    ctypes.c_int32, ctypes.POINTER(NET_DVR_SETUPALARM_PARAM)
                ]

                alarm_h = self._sdk.NET_DVR_SetupAlarmChan_V41(uid, ctypes.byref(setup))
                if alarm_h < 0:
                    err = self._sdk.NET_DVR_GetLastError()
                    logger.warning("NET_DVR_SetupAlarmChan_V41 failed: %d", err)
                    return False

                self._alarm_handle = alarm_h
                logger.info("⚡ [Port 8000 SDK Alarm] Active & listening on Port 8000 (user_id=%d, alarm_h=%d)", uid, alarm_h)
                self._record_event(
                    command_id=0,
                    details=f"SDK Alarm Channel successfully opened on Port 8000 (alarm_h={alarm_h})",
                    action_taken="Connected",
                )
                return True

            except Exception as e:
                logger.error("Exception setting up Port 8000 alarm channel: %s", e)
                return False

    async def _supervisor_loop(self) -> None:
        """Continuous background monitor that maintains connection and auto-reconnects."""
        while self._running:
            try:
                if not self.is_connected:
                    logger.info("Connecting Port 8000 SDK Alarm Channel...")
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
        logger.info("Hikvision Port 8000 Hardware Alarm Listener started")

    async def stop(self) -> None:
        """Stop listener and close alarm channel."""
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
                if self._alarm_handle >= 0:
                    try:
                        self._sdk.NET_DVR_CloseAlarmChan_V30(self._alarm_handle)
                    except Exception:
                        pass
                    self._alarm_handle = -1
                if self._user_id >= 0:
                    try:
                        self._sdk.NET_DVR_Logout(self._user_id)
                    except Exception:
                        pass
                    self._user_id = -1


sdk_alarm_listener = SDKAlarmListener()

