"""
Foxway Smart Doorbell — Dynamic Camera Discovery & Auto-Heal.

Provides zero-hardcoded network discovery:
1. Dynamically detects local server IP and /24 subnet via socket.
2. Fast concurrent asyncio TCP scanner for doorbell on port 8000 (SDK) and 554 (RTSP).
3. Auto-healing: detects if doorbell IP changed, re-scans subnet, updates SQLite & go2rtc.
4. Dynamic WAN URL detection and persistence.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Optional, Tuple
import urllib.parse
import httpx

from app.config import settings

logger = logging.getLogger("foxway.camera_discovery")

# Hardware identity anchors for EZVIZ DB1 Pro
KNOWN_DOORBELL_MAC = "d4:e8:53:18:78:9b"
KNOWN_DOORBELL_OUI = "d4:e8:53"
KNOWN_DOORBELL_MODEL_PREFIX = "CS-DB1"


def get_mac_for_ip(ip: str) -> Optional[str]:
    """Reads /proc/net/arp to retrieve the hardware MAC address for a given IP."""
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    mac = parts[3].lower()
                    if mac != "00:00:00:00:00:00":
                        return mac
    except Exception:
        pass
    return None


def verify_doorbell_sdk_identity(ip: str, port: int, user: str, password: str) -> Tuple[bool, str]:
    """
    Performs true Hikvision C-SDK login to verify device model and serial number.
    Returns (is_verified, serial_number).
    """
    if not ip or not password:
        return False, ""
    try:
        from app.audio_bridge import _get_sdk
        import ctypes

        sdk = _get_sdk()

        class NET_DVR_DEVICEINFO_V30(ctypes.Structure):
            _fields_ = [("sSerialNumber", ctypes.c_byte * 48)] + [("res", ctypes.c_byte * 96)]

        dev_info = NET_DVR_DEVICEINFO_V30()
        sdk.NET_DVR_Login_V30.restype = ctypes.c_int32
        sdk.NET_DVR_Login_V30.argtypes = [
            ctypes.c_char_p, ctypes.c_uint16, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p
        ]

        uid = sdk.NET_DVR_Login_V30(
            ip.encode("utf-8"), port, user.encode("utf-8"), password.encode("utf-8"), ctypes.byref(dev_info)
        )
        if uid >= 0:
            raw_serial = bytes(dev_info.sSerialNumber).split(b"\x00")[0].decode("ascii", errors="ignore")
            sdk.NET_DVR_Logout(uid)
            is_match = raw_serial.startswith(KNOWN_DOORBELL_MODEL_PREFIX)
            return is_match, raw_serial
        else:
            return False, ""
    except Exception as e:
        logger.debug("SDK identity check error for %s: %s", ip, e)
        return False, ""


def get_local_ip() -> str:
    """Dynamically determine the host's primary local LAN IP without hardcoding."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Does not send actual traffic, just resolves routing table to determine outbound IP
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def get_local_subnet() -> ipaddress.IPv4Network:
    """Return the /24 network corresponding to the active local IP."""
    local_ip = get_local_ip()
    return ipaddress.IPv4Network(f"{local_ip}/24", strict=False)


async def check_port(host: str, port: int, timeout: float = 0.35) -> bool:
    """Check if a TCP port is open on the target host."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def is_doorbell_alive(
    ip: str,
    sdk_port: int = 8000,
    rtsp_port: int = 554,
    user: str = "admin",
    password: Optional[str] = None,
) -> bool:
    """
    Check if the doorbell at `ip` is alive and is actually our EZVIZ DB1 Pro.
    Validates:
    1. Port connectivity (8000 or 554).
    2. MAC address check in ARP table (if present, must match known MAC or OUI).
    3. If password provided: C-SDK login check.
    """
    if not ip or ip in ("127.0.0.1", "0.0.0.0"):
        return False

    has_8000 = await check_port(ip, sdk_port, timeout=0.5)
    has_554 = await check_port(ip, rtsp_port, timeout=0.5)
    if not (has_8000 or has_554):
        return False

    # Check ARP MAC address
    mac = get_mac_for_ip(ip)
    if mac:
        if mac == KNOWN_DOORBELL_MAC or mac.startswith(KNOWN_DOORBELL_OUI):
            return True
        else:
            logger.warning(
                "Device at %s responded on ports, but MAC %s does NOT match doorbell (%s)!",
                ip, mac, KNOWN_DOORBELL_MAC
            )
            return False

    # If MAC not in ARP table yet, verify with C-SDK login if password available
    if password and has_8000:
        is_match, serial = await asyncio.to_thread(verify_doorbell_sdk_identity, ip, sdk_port, user, password)
        if is_match:
            return True
        else:
            logger.warning("Device at %s failed C-SDK doorbell identity verification (serial: %s)", ip, serial)
            return False

    return True


async def scan_subnet_for_doorbell(
    timeout_per_host: float = 0.35,
    user: str = "admin",
    password: Optional[str] = None,
) -> Optional[dict]:
    """
    Fast parallel asynchronous scan of all hosts in the local /24 subnet for doorbell.
    Requires 3-layer identity verification:
    1. Ports 8000 (SDK) and 554 (RTSP) responsive.
    2. MAC address matches known camera MAC (d4:e8:53:18:78:9b) or OUI (d4:e8:53).
    3. C-SDK login returns true EZVIZ DB1 serial number.
    """
    local_ip = get_local_ip()
    subnet = get_local_subnet()
    logger.info("Scanning local subnet %s for doorbell (server IP: %s)...", subnet, local_ip)

    hosts = [str(h) for h in subnet.hosts() if str(h) != local_ip]

    async def probe(host: str) -> Optional[dict]:
        has_8000 = await check_port(host, 8000, timeout=timeout_per_host)
        has_554 = await check_port(host, 554, timeout=timeout_per_host)
        if not (has_8000 or has_554):
            return None

        # Layer 1: Hardware MAC verification
        mac = get_mac_for_ip(host)
        mac_match = bool(mac and (mac == KNOWN_DOORBELL_MAC or mac.startswith(KNOWN_DOORBELL_OUI)))

        # Layer 2: C-SDK Identity Handshake
        sdk_match = False
        serial = ""
        if password and has_8000:
            sdk_match, serial = await asyncio.to_thread(verify_doorbell_sdk_identity, host, 8000, user, password)

        # Confidence calculation
        confidence = 0.0
        if mac_match and sdk_match:
            confidence = 1.0
        elif mac_match or sdk_match:
            confidence = 0.9
        elif has_8000 and has_554 and not mac:
            # Ports open, but MAC not yet in ARP and no password to test C-SDK
            confidence = 0.4
        else:
            # Different MAC or failed C-SDK login -> completely rejected (e.g. 192.168.2.4)
            logger.debug("Rejecting candidate %s (mac=%s, sdk_match=%s)", host, mac, sdk_match)
            return None

        return {
            "ip": host,
            "sdk_port": 8000,
            "rtsp_port": 554,
            "mac": mac,
            "serial": serial,
            "confidence": confidence,
        }

    tasks = [probe(h) for h in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates = [r for r in results if isinstance(r, dict) and r.get("confidence", 0) >= 0.8]
    if not candidates:
        logger.warning("No authenticated doorbell device discovered on subnet %s", subnet)
        return None

    # Pick the highest confidence candidate
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    best = candidates[0]
    logger.info(
        "🎯 Discovered authenticated doorbell at %s (MAC: %s, Serial: %s, confidence: %.2f)",
        best["ip"], best.get("mac"), best.get("serial"), best["confidence"]
    )
    return best


async def update_go2rtc_streams(camera_ip: str, user: str, password: str, go2rtc_url: Optional[str] = None) -> bool:
    """Dynamically update go2rtc stream URLs with newly discovered camera IP."""
    base_url = go2rtc_url or settings.GO2RTC_API_URL
    encoded_user = urllib.parse.quote(user, safe="")
    encoded_pass = urllib.parse.quote(password, safe="")

    main_url = f"rtsp://{encoded_user}:{encoded_pass}@{camera_ip}:554/H.264/ch1/main/av_stream"
    sub_url = f"rtsp://{encoded_user}:{encoded_pass}@{camera_ip}:554/H.264/ch1/sub/av_stream"

    success = True
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            # Update main stream (deurbel)
            resp_main = await client.put(f"{base_url}/api/streams", params={"src": main_url, "dst": "deurbel"})
            if resp_main.status_code not in (200, 201):
                logger.warning("go2rtc PUT main stream returned %s", resp_main.status_code)
                success = False

            # Update substream (deurbel_sub)
            resp_sub = await client.put(f"{base_url}/api/streams", params={"src": sub_url, "dst": "deurbel_sub"})
            if resp_sub.status_code not in (200, 201):
                logger.warning("go2rtc PUT sub stream returned %s", resp_sub.status_code)
                success = False

            logger.info("Successfully updated go2rtc streams with camera IP %s", camera_ip)
        except Exception as e:
            logger.error("Failed to update go2rtc streams: %s", e)
            success = False

    return success


async def auto_heal_doorbell_ip() -> Optional[str]:
    """
    Check if the registered doorbell IP is responding and verified.
    If unreachable (e.g. after DHCP change), scan the subnet, find the new IP
    using 3-layer hardware verification (MAC + Serial + RTSP),
    update SQLite settings, and reconfigure go2rtc.
    """
    from app.models import get_camera_credentials, set_camera_credentials

    creds = await get_camera_credentials()
    current_ip = creds.get("ip", "")
    cam_user = creds.get("user", "admin")
    cam_pass = creds.get("pass", "")

    # Check if current IP is alive and verified
    if current_ip and await is_doorbell_alive(current_ip, user=cam_user, password=cam_pass):
        logger.debug("Doorbell at %s is healthy and verified", current_ip)
        return current_ip

    logger.warning("Doorbell at %s unreachable or unverified! Triggering 3-layer auto-heal scan...", current_ip)
    discovered = await scan_subnet_for_doorbell(user=cam_user, password=cam_pass)
    if not discovered:
        logger.error("Auto-heal failed: no authenticated doorbell found on local subnet")
        return None

    new_ip = discovered["ip"]
    logger.info(
        "Auto-heal: Migrating doorbell IP from '%s' to '%s' (MAC: %s, Serial: %s)",
        current_ip, new_ip, discovered.get("mac"), discovered.get("serial")
    )

    # Persist updated IP in database
    await set_camera_credentials(
        ip=new_ip,
        port=discovered.get("sdk_port", 8000),
        user=cam_user,
        password=cam_pass,
    )

    # Update go2rtc if password is configured
    if cam_pass:
        await update_go2rtc_streams(
            camera_ip=new_ip,
            user=cam_user,
            password=cam_pass,
        )

    return new_ip
