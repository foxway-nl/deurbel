"""
Foxway Smart Doorbell — Zeroconf mDNS Broadcaster.

Advertises ``_foxway-doorbell._tcp.local.`` on the LAN so that Android
devices can discover the backend via NsdManager without any manual IP
configuration.  TXT records carry all endpoint URLs the app needs.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

from zeroconf import IPVersion, ServiceInfo, Zeroconf
from zeroconf.asyncio import AsyncZeroconf

from app.config import settings

logger = logging.getLogger("foxway.mdns")

SERVICE_TYPE_DEURBEL = "_deurbel._tcp.local."
SERVICE_NAME_DEURBEL = "Foxway Deurbel._deurbel._tcp.local."

SERVICE_TYPE_FOXWAY = "_foxway-doorbell._tcp.local."
SERVICE_NAME_FOXWAY = "Foxway Deurbel._foxway-doorbell._tcp.local."


class MdnsService:
    """
    Manages the lifecycle of the Zeroconf service advertisement.
    Call ``start()`` at app startup and ``stop()`` at shutdown.
    """

    def __init__(self) -> None:
        self._zeroconf: Optional[AsyncZeroconf] = None
        self._infos: list[ServiceInfo] = []

    async def start(self) -> None:
        """Register the mDNS service on all available interfaces."""
        try:
            # Resolve the host IP address for the service record
            host_ip = self._get_local_ip()
            logger.info("Advertising mDNS on %s:%d", host_ip, settings.HOST_PORT)

            txt_records = {
                "lan_url": settings.LAN_URL,
                "wan_url": settings.WAN_URL,
                "go2rtc_lan": settings.GO2RTC_LAN_WS,
                "go2rtc_wan": settings.GO2RTC_WAN_WS,
                "version": "1.0.0",
            }

            info_deurbel = ServiceInfo(
                type_=SERVICE_TYPE_DEURBEL,
                name=SERVICE_NAME_DEURBEL,
                addresses=[socket.inet_aton(host_ip)],
                port=settings.HOST_PORT,
                properties=txt_records,
                server="foxway-deurbel.local.",
            )

            info_foxway = ServiceInfo(
                type_=SERVICE_TYPE_FOXWAY,
                name=SERVICE_NAME_FOXWAY,
                addresses=[socket.inet_aton(host_ip)],
                port=settings.HOST_PORT,
                properties=txt_records,
                server="foxway-deurbel.local.",
            )

            self._infos = [info_deurbel, info_foxway]
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
            for info in self._infos:
                await self._zeroconf.async_register_service(info)
            logger.info("mDNS services registered: %s & %s", SERVICE_NAME_DEURBEL, SERVICE_NAME_FOXWAY)
        except Exception:
            logger.exception("Failed to start mDNS service")

    async def stop(self) -> None:
        """Unregister the mDNS services."""
        if self._zeroconf and self._infos:
            try:
                for info in self._infos:
                    await self._zeroconf.async_unregister_service(info)
                await self._zeroconf.async_close()
                logger.info("mDNS services unregistered")
            except Exception:
                logger.exception("Error stopping mDNS service")

    @staticmethod
    def _get_local_ip() -> str:
        """Best-effort dynamic detection of the machine's LAN IP without hardcoded fallback."""
        try:
            from app.camera_discovery import get_local_ip
            return get_local_ip()
        except Exception:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("1.1.1.1", 80))
                ip = s.getsockname()[0]
                s.close()
                return ip
            except Exception:
                return "127.0.0.1"


# Module-level singleton
mdns_service = MdnsService()
