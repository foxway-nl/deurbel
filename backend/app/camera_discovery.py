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
from typing import Optional
import urllib.parse
import httpx

from app.config import settings

logger = logging.getLogger("foxway.camera_discovery")


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


async def is_doorbell_alive(ip: str, sdk_port: int = 8000, rtsp_port: int = 554) -> bool:
    """Check if the doorbell responds on either its SDK port (8000) or RTSP port (554)."""
    if not ip or ip in ("127.0.0.1", "0.0.0.0"):
        return False
    results = await asyncio.gather(
        check_port(ip, sdk_port, timeout=0.5),
        check_port(ip, rtsp_port, timeout=0.5),
        return_exceptions=True,
    )
    return any(r is True for r in results)


async def scan_subnet_for_doorbell(timeout_per_host: float = 0.35) -> Optional[dict]:
    """
    Fast parallel asynchronous scan of all hosts in the local /24 subnet for doorbell ports.
    Hikvision / EZVIZ smart doorbells expose port 8000 (Hikvision SDK) and port 554 (RTSP).
    """
    local_ip = get_local_ip()
    subnet = get_local_subnet()
    logger.info("Scanning local subnet %s for doorbell (server IP: %s)...", subnet, local_ip)

    hosts = [str(h) for h in subnet.hosts() if str(h) != local_ip]

    # Check both port 8000 and 554 concurrently
    async def probe(host: str) -> Optional[dict]:
        has_8000 = await check_port(host, 8000, timeout=timeout_per_host)
        has_554 = await check_port(host, 554, timeout=timeout_per_host)
        if has_8000 and has_554:
            return {"ip": host, "sdk_port": 8000, "rtsp_port": 554, "confidence": 1.0}
        if has_8000:
            return {"ip": host, "sdk_port": 8000, "rtsp_port": 554, "confidence": 0.8}
        if has_554:
            return {"ip": host, "sdk_port": 8000, "rtsp_port": 554, "confidence": 0.5}
        return None

    tasks = [probe(h) for h in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates = [r for r in results if isinstance(r, dict)]
    if not candidates:
        logger.warning("No doorbell device discovered on subnet %s", subnet)
        return None

    # Pick the highest confidence candidate (both 8000 and 554 open)
    candidates.sort(key=lambda x: x.get("confidence", 0.0), reverse=True)
    best = candidates[0]
    logger.info("Discovered doorbell at %s (confidence: %.2f)", best["ip"], best["confidence"])
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
    Check if the registered doorbell IP is responding.
    If unreachable (e.g. after DHCP change), scan the subnet, find the new IP,
    update SQLite settings, and reconfigure go2rtc.
    """
    from app.models import get_camera_credentials, set_camera_credentials

    creds = await get_camera_credentials()
    current_ip = creds.get("ip", "")

    # Check if current IP is alive
    if current_ip and await is_doorbell_alive(current_ip):
        logger.debug("Doorbell at %s is healthy and responsive", current_ip)
        return current_ip

    logger.warning("Doorbell at %s unreachable! Triggering auto-heal subnet scan...", current_ip)
    discovered = await scan_subnet_for_doorbell()
    if not discovered:
        logger.error("Auto-heal failed: no doorbell found on local subnet")
        return None

    new_ip = discovered["ip"]
    logger.info("Auto-heal: Migrating doorbell IP from '%s' to '%s'", current_ip, new_ip)

    # Persist updated IP in database
    await set_camera_credentials(
        ip=new_ip,
        port=discovered.get("sdk_port", 8000),
        user=creds.get("user", "admin"),
        password=creds.get("pass", ""),
    )

    # Update go2rtc if password is configured
    if creds.get("pass"):
        await update_go2rtc_streams(
            camera_ip=new_ip,
            user=creds.get("user", "admin"),
            password=creds.get("pass", ""),
        )

    return new_ip
