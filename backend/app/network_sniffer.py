"""
Foxway Smart Doorbell — Host-Level EZVIZ Cloud Packet Sniffer & ARP Redirector (v3.4.1).

Architecture & Safety Guarantees:
1. Targeted ARP-Redirect: Sends an ARP reply exclusively to 192.168.2.31 every 2.0s
   (never broadcast!), directing its WAN-bound traffic through 192.168.2.1.
2. Mandatory Fail-Safe: atexit, SIGTERM, and SIGINT handlers send 3 original ARP restore
   packets directly to 192.168.2.31, ensuring the doorbell NEVER loses cloud connectivity.
3. 1000ms Burst Aggregation: Groups outbound packets (dst not 192.168.0.0/16) into 1-second
   burst windows and logs to /mnt/opslag/deurbel/logs/network_sniff.jsonl and SQLite.
4. Auto-Calibration: Whenever SHAZAM_CHIME triggers, records the outbound packet signature
   to /mnt/opslag/deurbel/backend/app/assets/network_ring_signature.json.
5. Trigger Source: When a matching cloud-burst pattern occurs, triggers a doorbell ring
   with trigger_source = "NETWORK_SNIFF" (or "SHAZAM + SNIFF" if within 2s of Shazam).
"""

from __future__ import annotations

import atexit
import asyncio
import collections
import datetime
import json
import logging
import os
import signal
import socket
import struct
import threading
import time
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("foxway.network_sniffer")

DOORBELL_IP = "192.168.2.31"
DOORBELL_MAC = "d4:e8:53:18:78:9b"
GATEWAY_IP = "192.168.2.254"
GATEWAY_MAC = "00:f8:cc:d0:9e:00"
SERVER_IP = "192.168.2.1"
SERVER_MAC = "80:e8:2c:1b:7a:46"
IFACE = "eno1"

SIGNATURE_FILE = "/mnt/opslag/deurbel/backend/app/assets/network_ring_signature.json"
SNIFF_LOG_FILE = "/mnt/opslag/deurbel/logs/network_sniff.jsonl"


def _build_arp_packet(hwsrc: str, psrc: str, hwdst: str, pdst: str, op: int = 2) -> bytes:
    """Build raw Ethernet + ARP frame (42 bytes)."""
    src_mac_bytes = bytes.fromhex(hwsrc.replace(":", ""))
    dst_mac_bytes = bytes.fromhex(hwdst.replace(":", ""))
    eth_header = dst_mac_bytes + src_mac_bytes + struct.pack("!H", 0x0806)

    src_ip_bytes = socket.inet_aton(psrc)
    dst_ip_bytes = socket.inet_aton(pdst)

    arp_header = struct.pack(
        "!HHBBH6s4s6s4s",
        0x0001,           # Hardware type: Ethernet
        0x0800,           # Protocol type: IPv4
        6,                # Hardware size
        4,                # Protocol size
        op,               # Opcode: 1=Request, 2=Reply
        src_mac_bytes,
        src_ip_bytes,
        dst_mac_bytes,
        dst_ip_bytes,
    )
    return eth_header + arp_header


class NetworkSniffer:
    """Active ARP-Redirect and EZVIZ Cloud Packet Sniffer."""

    def __init__(self):
        self._running: bool = False
        self._stop_event = threading.Event()
        self._arp_thread: Optional[threading.Thread] = None
        self._sniff_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.doorbell_ip = DOORBELL_IP
        self.doorbell_mac = DOORBELL_MAC
        self.gateway_ip = GATEWAY_IP
        self.gateway_mac = GATEWAY_MAC
        self.server_ip = SERVER_IP
        self.server_mac = SERVER_MAC
        self.iface = IFACE

        self.recent_bursts: Deque[dict] = collections.deque(maxlen=200)
        self.recent_packets: Deque[dict] = collections.deque(maxlen=100)
        self._current_window_packets: List[dict] = []
        self._current_window_start: float = time.time()
        self._window_lock = threading.Lock()

        self._last_shazam_ring_time: float = 0.0
        self._last_sniff_trigger_time: float = 0.0
        self._cooldown_seconds: float = 5.0

        self.known_signature: dict = self._load_signature()

        # Register fail-safe cleanup on exit
        atexit.register(self._restore_arp_sync)
        try:
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGINT, self._signal_handler)
        except Exception:
            pass

    def _signal_handler(self, signum, frame):
        logger.info("Signal %s received -> restoring doorbell ARP table immediately", signum)
        self._restore_arp_sync()

    def _load_signature(self) -> dict:
        """Load network ring signature from file or return sensible defaults."""
        default_sig = {
            "enabled": True,
            "dst_port": 6404,
            "protocol": "UDP",
            "min_packet_count": 2,
            "min_bytes": 60,
            "max_bytes": 4000,
            "last_calibrated": "2026-10-31T16:53:08Z",
            "signature_type": "EZVIZ_CLOUD_P2P_BURST",
        }
        try:
            if os.path.exists(SIGNATURE_FILE):
                with open(SIGNATURE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data
        except Exception as e:
            logger.warning("Could not load network signature: %s", e)
        return default_sig

    def _save_signature(self, sig: dict) -> None:
        try:
            os.makedirs(os.path.dirname(SIGNATURE_FILE), exist_ok=True)
            with open(SIGNATURE_FILE, "w", encoding="utf-8") as f:
                json.dump(sig, f, indent=2)
            self.known_signature = sig
            logger.info("💾 Saved updated network ring signature: %s", sig)
        except Exception as e:
            logger.error("Failed saving network signature: %s", e)

    def _restore_arp_sync(self) -> None:
        """Send 3x ARP restore packets to restore the original router MAC in the doorbell's cache."""
        try:
            restore_pkt = _build_arp_packet(
                hwsrc=self.gateway_mac,
                psrc=self.gateway_ip,
                hwdst=self.doorbell_mac,
                pdst=self.doorbell_ip,
                op=2,
            )
            raw_sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
            raw_sock.bind((self.iface, 0))
            for _ in range(3):
                raw_sock.send(restore_pkt)
                time.sleep(0.05)
            raw_sock.close()
            logger.info("🛡️ [Fail-Safe] Restored original gateway ARP (%s -> %s) on %s", self.gateway_ip, self.gateway_mac, self.doorbell_ip)
        except Exception as e:
            logger.debug("ARP restore exception: %s", e)

    def _arp_redirect_loop(self) -> None:
        """Sends targeted ARP reply to doorbell every 2.0s."""
        logger.info("🚀 Starting targeted ARP redirect for %s via %s (%s)", self.doorbell_ip, self.server_ip, self.server_mac)
        arp_pkt = _build_arp_packet(
            hwsrc=self.server_mac,
            psrc=self.gateway_ip,
            hwdst=self.doorbell_mac,
            pdst=self.doorbell_ip,
            op=2,
        )

        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
            sock.bind((self.iface, 0))
        except Exception as e:
            logger.error("Failed opening raw socket for ARP: %s", e)
            return

        while not self._stop_event.is_set():
            try:
                sock.send(arp_pkt)
            except Exception as se:
                logger.debug("ARP send error: %s", se)
            time.sleep(2.0)

        try:
            sock.close()
        except Exception:
            pass

    def _sniff_loop(self) -> None:
        """Raw socket sniffer capturing outbound WAN packets from 192.168.2.31."""
        logger.info("🌐 Packet sniffer active on %s (filtering ip src %s -> WAN)", self.iface, self.doorbell_ip)
        try:
            # AF_PACKET with ETH_P_IP (0x0800)
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0800))
            sock.bind((self.iface, 0x0800))
            sock.settimeout(1.0)
        except Exception as e:
            logger.error("Failed opening raw sniffer socket: %s", e)
            return

        while not self._stop_event.is_set():
            # Flush 1000ms window periodically
            now_t = time.time()
            if (now_t - self._current_window_start) >= 1.0:
                self._flush_window(now_t)

            try:
                raw_data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.debug("Sniffer recv error: %s", e)
                continue

            if len(raw_data) < 34:
                continue

            # Parse Ethernet (14 bytes)
            eth_proto = struct.unpack("!H", raw_data[12:14])[0]
            if eth_proto != 0x0800:
                continue

            ip_header = raw_data[14:34]
            ip_ver_ihl = ip_header[0]
            ihl = (ip_ver_ihl & 0x0F) * 4
            src_ip = socket.inet_ntoa(ip_header[12:16])
            dst_ip = socket.inet_ntoa(ip_header[16:20])

            if src_ip != self.doorbell_ip:
                continue

            # Ignore local LAN and multicast/broadcast
            if dst_ip.startswith("192.168.") or dst_ip.startswith("224.") or dst_ip.startswith("255."):
                continue

            proto_num = ip_header[9]
            total_ip_len = struct.unpack("!H", ip_header[2:4])[0]
            payload_len = max(0, total_ip_len - ihl)

            sport, dport = 0, 0
            proto_name = "OTHER"
            is_syn = False

            transport_offset = 14 + ihl
            if proto_num == 6 and len(raw_data) >= (transport_offset + 20):  # TCP
                proto_name = "TCP"
                tcp_hdr = raw_data[transport_offset:transport_offset + 20]
                sport, dport = struct.unpack("!HH", tcp_hdr[:4])
                flags = tcp_hdr[13]
                is_syn = bool(flags & 0x02)
                tcp_hl = ((tcp_hdr[12] >> 4) & 0x0F) * 4
                payload_len = max(0, payload_len - tcp_hl)
            elif proto_num == 17 and len(raw_data) >= (transport_offset + 8):  # UDP
                proto_name = "UDP"
                udp_hdr = raw_data[transport_offset:transport_offset + 8]
                sport, dport = struct.unpack("!HH", udp_hdr[:4])
                payload_len = max(0, payload_len - 8)

            pkt_record = {
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "src_ip": src_ip,
                "src_port": sport,
                "dst_ip": dst_ip,
                "dst_port": dport,
                "protocol": proto_name,
                "payload_len": payload_len,
                "total_len": total_ip_len,
                "is_syn": is_syn,
            }

            with self._window_lock:
                self._current_window_packets.append(pkt_record)
                self.recent_packets.append(pkt_record)

        try:
            sock.close()
        except Exception:
            pass

    def _flush_window(self, now_t: float) -> None:
        """Aggregates 1000ms packet window into structured burst record."""
        with self._window_lock:
            packets = list(self._current_window_packets)
            self._current_window_packets.clear()
            self._current_window_start = now_t

        if not packets:
            return

        # Group by (dst_ip, dst_port, protocol)
        groups: Dict[tuple, List[dict]] = collections.defaultdict(list)
        for p in packets:
            key = (p["dst_ip"], p["dst_port"], p["protocol"])
            groups[key].append(p)

        for (dst_ip, dst_port, proto), group_pkts in groups.items():
            total_bytes = sum(p["total_len"] for p in group_pkts)
            payload_sizes = [p["payload_len"] for p in group_pkts]
            has_syn = any(p["is_syn"] for p in group_pkts)
            is_p2p = (proto == "UDP" and dst_port in (6404, 8555, 8666, 8883))

            burst_dict = {
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "dst_ip": dst_ip,
                "dst_port": dst_port,
                "protocol": proto,
                "packet_count": len(group_pkts),
                "payload_sizes": payload_sizes,
                "total_bytes": total_bytes,
                "is_new_tcp_syn": has_syn,
                "is_p2p_burst": is_p2p,
                "signature_matched": False,
            }

            # Check if this matches known doorbell ring signature
            is_ring = self._check_ring_signature(burst_dict)
            if is_ring:
                burst_dict["signature_matched"] = True
                self._dispatch_network_ring(burst_dict)

            self.recent_bursts.append(burst_dict)
            self._append_burst_log(burst_dict)

    def _append_burst_log(self, burst: dict) -> None:
        try:
            os.makedirs(os.path.dirname(SNIFF_LOG_FILE), exist_ok=True)
            line = json.dumps(burst, ensure_ascii=False) + "\n"
            with open(SNIFF_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.debug("Failed writing sniff log: %s", e)

    def _check_ring_signature(self, burst: dict) -> bool:
        """
        Determines if the burst matches the EZVIZ Cloud doorbell ring signature (v3.5.0):
        1. Pattern A: TCP 8820 packet with payload between 195 and 400 bytes (or total_len > 250B).
        2. Pattern B: Burst of >= 3 TCP packets of 191 bytes (>= 573B payload / >= 690B total) within 1000ms.
        3. Keepalive Filter: Ignores isolated 1 or 2 packets of 191 bytes keepalive to port 8820.
        4. Calibrated Signature: Matches calibrated cloud burst fallback if enabled.
        """
        now_t = time.time()
        if (now_t - self._last_sniff_trigger_time) < self._cooldown_seconds:
            return False

        dst_port = burst.get("dst_port")
        protocol = burst.get("protocol")
        pkt_count = burst.get("packet_count", 0)
        payload_sizes = burst.get("payload_sizes", [])
        total_bytes = burst.get("total_bytes", 0)

        # ── EZVIZ Cloud Port 8820 TCP Analysis ──
        if protocol == "TCP" and dst_port == 8820:
            # Filter out isolated 1 or 2 packet keepalives of 191B
            is_191_keepalive = all(185 <= sz <= 194 for sz in payload_sizes)
            if pkt_count < 3 and is_191_keepalive:
                logger.debug("[Sniffer] Ignoring isolated %d-packet 191B keepalive on TCP 8820", pkt_count)
                return False

            # Pattern A: TCP 8820 packet with payload between 195 and 400 bytes (or total > 250B)
            has_pattern_a_pkt = any(195 <= sz <= 400 for sz in payload_sizes) or (pkt_count == 1 and total_bytes > 250)
            if has_pattern_a_pkt:
                logger.info("🎯 [Sniffer] Pattern A Matched on TCP 8820 (payload in [195, 400]B or len>250B): %s", payload_sizes)
                return True

            # Pattern B: Burst of >= 3 TCP packets of 191B (>= 573B payload / >= 690B total) within 1000ms
            if pkt_count >= 3 and (sum(payload_sizes) >= 573 or total_bytes >= 690):
                logger.info("🎯 [Sniffer] Pattern B Matched on TCP 8820 (>=3 packets of 191B, total=%dB): %s", total_bytes, payload_sizes)
                return True

        # ── Fallback: Calibrated Cloud Ring Signature ──
        sig = self.known_signature
        if sig.get("enabled", True):
            sig_port = sig.get("dst_port")
            sig_proto = sig.get("protocol")
            min_count = sig.get("min_packet_count", 2)
            min_bytes = sig.get("min_bytes", 60)
            max_bytes = sig.get("max_bytes", 6000)

            port_match = (sig_port is None) or (dst_port == sig_port)
            proto_match = (sig_proto is None) or (protocol == sig_proto)
            count_match = pkt_count >= min_count
            bytes_match = min_bytes <= total_bytes <= max_bytes

            if port_match and proto_match and count_match and bytes_match:
                logger.info("🎯 [Sniffer] Calibrated Signature Matched (%s:%s, pkts=%d, bytes=%d)", protocol, dst_port, pkt_count, total_bytes)
                return True

        return False

    def on_shazam_chime_triggered(self, chime_timestamp: float) -> None:
        """
        Called when SHAZAM_CHIME detects the physical chime.
        Auto-calibrates the network signature by finding the corresponding cloud packet burst!
        """
        self._last_shazam_ring_time = chime_timestamp
        logger.info("🔔 [Sniffer Auto-Calibrate] Shazam chime detected at %.3f — scanning for cloud packet burst...", chime_timestamp)

        # Look at bursts in recent history within +-2.0s of chime
        candidates = []
        for b in list(self.recent_bursts):
            try:
                b_ts = datetime.datetime.strptime(b["timestamp"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
                if abs(b_ts - chime_timestamp) <= 2.5:
                    candidates.append(b)
            except Exception:
                pass

        if candidates:
            # Pick the burst with most packets / largest size
            best_burst = max(candidates, key=lambda x: (x["packet_count"], x["total_bytes"]))
            new_sig = {
                "enabled": True,
                "dst_port": best_burst["dst_port"],
                "protocol": best_burst["protocol"],
                "min_packet_count": max(1, best_burst["packet_count"] - 1),
                "min_bytes": max(40, int(best_burst["total_bytes"] * 0.7)),
                "max_bytes": int(best_burst["total_bytes"] * 2.5),
                "last_calibrated": datetime.datetime.now().isoformat(),
                "sample_payload_sizes": best_burst["payload_sizes"],
                "signature_type": "CALIBRATED_SHAZAM_BURST",
            }
            self._save_signature(new_sig)
            logger.info("🎯 [Sniffer Auto-Calibrated] Correlated with cloud burst to %s:%d (%s) -> Signature Updated!",
                        best_burst["dst_ip"], best_burst["dst_port"], best_burst["protocol"])
        else:
            logger.info("ℹ️ [Sniffer Auto-Calibrate] No external cloud burst in +/-2.5s window to correlate.")

    def _dispatch_network_ring(self, burst: dict) -> None:
        """Fires doorbell ring via the Central Ring Coordinator with trigger_source = NETWORK_SNIFF."""
        now_t = time.time()
        self._last_sniff_trigger_time = now_t

        logger.info("🚨 [Network Sniffer] MATCHED CLOUD BURST! Registering ring signal (source=NETWORK_SNIFF, dst=%s:%d %s, bytes=%d)",
                    burst["dst_ip"], burst["dst_port"], burst["protocol"], burst["total_bytes"])

        try:
            from app.ring_coordinator import register_ring_signal
            register_ring_signal(source="NETWORK_SNIFF", details=burst)
        except Exception as e:
            logger.error("Failed dispatching network ring to coordinator: %s", e)
            if self._loop and self._loop.is_running():
                from app.state_machine import call_sm
                asyncio.run_coroutine_threadsafe(call_sm.ring(trigger_source="NETWORK_SNIFF"), self._loop)

    async def start(self) -> None:
        """Starts ARP redirect and raw packet sniffer."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()

        # Start ARP Redirect Thread
        self._arp_thread = threading.Thread(target=self._arp_redirect_loop, daemon=True, name="ARP-Redirect")
        self._arp_thread.start()

        # Start Raw Sniffer Thread
        self._sniff_thread = threading.Thread(target=self._sniff_loop, daemon=True, name="Raw-Packet-Sniffer")
        self._sniff_thread.start()

        logger.info("✅ Network Sniffer & ARP Redirector active on %s", self.iface)

    async def stop(self) -> None:
        """Stops background threads and restores ARP table."""
        self._running = False
        self._stop_event.set()
        await asyncio.to_thread(self._restore_arp_sync)
        logger.info("🛑 Network Sniffer stopped & ARP restored")

    def get_recent_bursts(self) -> List[dict]:
        return list(self.recent_bursts)


network_sniffer = NetworkSniffer()

