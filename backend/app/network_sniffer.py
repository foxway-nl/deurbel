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
from zoneinfo import ZoneInfo

logger = logging.getLogger("foxway.network_sniffer")
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

DOORBELL_IP = "192.168.2.31"
DOORBELL_MAC = "d4:e8:53:18:78:9b"
GATEWAY_IP = "192.168.2.254"
GATEWAY_MAC = "00:f8:cc:d0:9e:00"
SERVER_IP = "192.168.2.1"
SERVER_MAC = "80:e8:2c:1b:7a:46"
IFACE = "eno1"

SIGNATURE_FILE = "/mnt/opslag/deurbel/backend/app/assets/network_ring_signature.json"
SNIFF_LOG_FILE = "/mnt/opslag/deurbel/logs/network_sniff.jsonl"
LEARN_LOG_FILE = "/mnt/opslag/deurbel/logs/sniffer_learning.jsonl"
TIMING_LOG_FILE = "/mnt/opslag/deurbel/logs/dual_trigger_timing.jsonl"


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

        # Passive Differential Byte Learner (v3.5.1)
        self._payload_buffer: Deque[dict] = collections.deque(maxlen=2000)
        self.background_noise_hashes: set[str] = set()
        self.learned_chime_candidates: List[dict] = []

        # Dual-Trigger Precision Timing Monitor (v3.5.5)
        self.recent_timing_records: Deque[dict] = collections.deque(maxlen=50)
        self._load_persisted_timing_and_learner()

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

    def _load_persisted_timing_and_learner(self) -> None:
        """Load persisted timing and learner candidates on startup."""
        try:
            if os.path.exists(LEARN_LOG_FILE):
                with open(LEARN_LOG_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                self.learned_chime_candidates.append(json.loads(line))
                            except Exception:
                                pass
        except Exception as e:
            logger.debug("Could not preload learner candidates: %s", e)

        try:
            if os.path.exists(TIMING_LOG_FILE):
                with open(TIMING_LOG_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                self.recent_timing_records.append(json.loads(line))
                            except Exception:
                                pass
        except Exception as e:
            logger.debug("Could not preload timing records: %s", e)

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
            raw_payload = b""

            transport_offset = 14 + ihl
            if proto_num == 6 and len(raw_data) >= (transport_offset + 20):  # TCP
                proto_name = "TCP"
                tcp_hdr = raw_data[transport_offset:transport_offset + 20]
                sport, dport = struct.unpack("!HH", tcp_hdr[:4])
                flags = tcp_hdr[13]
                is_syn = bool(flags & 0x02)
                tcp_hl = ((tcp_hdr[12] >> 4) & 0x0F) * 4
                raw_payload = raw_data[transport_offset + tcp_hl:]
                payload_len = len(raw_payload)
            elif proto_num == 17 and len(raw_data) >= (transport_offset + 8):  # UDP
                proto_name = "UDP"
                udp_hdr = raw_data[transport_offset:transport_offset + 8]
                sport, dport = struct.unpack("!HH", udp_hdr[:4])
                raw_payload = raw_data[transport_offset + 8:]
                payload_len = len(raw_payload)

            payload_hex = raw_payload.hex() if raw_payload else ""
            pkt_now_t = time.time()

            # Direct Real-Time Verified Ring Interception (v3.5.5 Hybrid Dual-Trigger)
            # Checks for verified EZVIZ Doorbell Call Request:
            # Event code /2000/4675 over TCP 8820
            is_verified_ring_packet = False
            ring_reason = ""
            if "2f323030302f34363735" in payload_hex:
                is_verified_ring_packet = True
                ring_reason = "EZVIZ_CLOUD_INTERCOM_CALL (/2000/4675)"

            if is_verified_ring_packet:
                now_t = time.time()
                if (now_t - self._last_sniff_trigger_time) >= self._cooldown_seconds:
                    self._last_sniff_trigger_time = now_t
                    logger.info("⚡ [NETWORK_SNIFF TRIGGER] Verified Doorbell Intercom Packet intercepted! Reason: %s, Dst: %s:%d (%s) -> Dispatching immediate ring!",
                                ring_reason, dst_ip, dport, proto_name)
                    try:
                        from app.ring_coordinator import register_ring_signal
                        register_ring_signal(source="NETWORK_SNIFF", details={
                            "reason": ring_reason,
                            "dst_ip": dst_ip,
                            "dst_port": dport,
                            "protocol": proto_name,
                            "payload_len": payload_len,
                            "hex_preview": payload_hex[:32]
                        })
                    except Exception as re:
                        logger.error("Failed triggering ring from network sniffer: %s", re)

            pkt_record = {
                "timestamp": datetime.datetime.now(AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "ts_float": pkt_now_t,
                "src_ip": src_ip,
                "src_port": sport,
                "dst_ip": dst_ip,
                "dst_port": dport,
                "protocol": proto_name,
                "payload_len": payload_len,
                "total_len": total_ip_len,
                "is_syn": is_syn,
                "payload_hex": payload_hex,
            }

            with self._window_lock:
                self._current_window_packets.append(pkt_record)
                self.recent_packets.append(pkt_record)
                self._payload_buffer.append(pkt_record)
                # If no chime occurred in last 5 seconds, register hex into background baseline noise
                if (pkt_now_t - self._last_shazam_ring_time) > 5.0 and payload_hex:
                    self.background_noise_hashes.add(payload_hex)

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

            first_payload_hex = ""
            for p in group_pkts:
                if p.get("payload_hex"):
                    first_payload_hex = p.get("payload_hex")
                    break

            burst_dict = {
                "timestamp": datetime.datetime.now(AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "dst_ip": dst_ip,
                "dst_port": dst_port,
                "protocol": proto,
                "packet_count": len(group_pkts),
                "payload_sizes": payload_sizes,
                "total_bytes": total_bytes,
                "is_new_tcp_syn": has_syn,
                "is_p2p_burst": is_p2p,
                "signature_matched": False,
                "payload_hex": first_payload_hex,
            }

            # Check if this matches known doorbell ring signature (Shadow Evaluation only)
            is_ring = self._check_ring_signature(burst_dict)
            if is_ring:
                burst_dict["signature_matched"] = True
                logger.debug("🧪 [Sniffer Shadow Mode] Signature matched but doorbell ring is suppressed (shadow mode).")

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
        Differential Byte Learner (v3.5.1):
        1. Compares all packets in [-2.0s, +2.0s] against background_noise_hashes baseline.
        2. Isolates unique hex sequences that NEVER appeared in normal background traffic.
        3. Persists candidates to /mnt/opslag/deurbel/logs/sniffer_learning.jsonl.
        4. Auto-calibrates burst signature for diagnostic telemetry (Shadow Mode - NO ring).
        """
        self._last_shazam_ring_time = chime_timestamp
        logger.info("🔔 [Sniffer Differential Learner] Shazam chime detected at %.3f — isolating candidate ring packets...", chime_timestamp)

        # 1. Differential Byte Learning (Pre-Chime Window: -4.5s to +1.5s)
        # Because the hardware button press sends network packets ~2.5s-3.5s BEFORE the audio-chime completes
        with self._window_lock:
            buf_packets = list(self._payload_buffer)

        candidates_found = 0
        for p in buf_packets:
            p_ts = p.get("ts_float", 0.0)
            delta = p_ts - chime_timestamp
            if -4.5 <= delta <= 1.5:
                hex_p = p.get("payload_hex", "")
                if not hex_p:
                    continue

                # Filter out routine cloud keepalives that fire 1000+ times/day
                is_routine_heartbeat = (
                    hex_p.startswith("31bc01000a2f323030302f34313033")
                    or hex_p.startswith("31cc01000a2f323030302f34313033")
                    or hex_p == "c00000000000"
                )

                # High priority match: EZVIZ P2P Intercom packet containing camera serial (SjIyNTE4ODcz / J22518873)
                is_p2p_intercom = ("536a49794e5445344f44" in hex_p) or (p.get("protocol") == "UDP" and p.get("payload_len") == 49)

                # Check if this exact hex appeared frequently in long-term background (>5s before chime)
                seen_in_older_baseline = any(
                    (other_p.get("payload_hex") == hex_p and (chime_timestamp - other_p.get("ts_float", 0.0)) > 5.0)
                    for other_p in buf_packets
                )

                if is_p2p_intercom or (not is_routine_heartbeat and not seen_in_older_baseline):
                    cand = {
                        "timestamp": p["timestamp"],
                        "chime_timestamp": chime_timestamp,
                        "chime_time_local": datetime.datetime.fromtimestamp(chime_timestamp, tz=AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                        "dst_ip": p["dst_ip"],
                        "dst_port": p["dst_port"],
                        "protocol": p["protocol"],
                        "payload_len": p["payload_len"],
                        "hex_payload": hex_p,
                        "delta_seconds": round(delta, 3),
                        "reason": "P2P_INTERCOM_ALERT" if is_p2p_intercom else "DIFFERENTIAL_CHIME_MATCH",
                    }
                    with self._window_lock:
                        self.learned_chime_candidates.append(cand)
                    candidates_found += 1
                    try:
                        os.makedirs(os.path.dirname(LEARN_LOG_FILE), exist_ok=True)
                        with open(LEARN_LOG_FILE, "a", encoding="utf-8") as f:
                            f.write(json.dumps(cand, ensure_ascii=False) + "\n")
                    except Exception as le:
                        logger.debug("Failed saving sniffer learn log: %s", le)
                    logger.info("🧪 [Sniffer Learner] Candidate differential payload isolated: dst=%s:%s, delta=%.2fs, hex=%s",
                                cand["dst_ip"], cand["dst_port"], cand["delta_seconds"], cand["hex_payload"][:32])

        if candidates_found == 0:
            logger.info("ℹ️ [Sniffer Learner] No unique differential packets in [-4.5s, +1.5s] window beyond baseline noise.")

        # 2. Look at bursts in recent history within [-4.5s, +1.5s] of chime for shadow diagnostics
        candidates = []
        for b in list(self.recent_bursts):
            try:
                b_ts = datetime.datetime.strptime(b["timestamp"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
                delta_b = b_ts - chime_timestamp
                if -4.5 <= delta_b <= 1.5:
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

            # Notify ring coordinator of sniffer confirmation (for VERIFIED dual-source logging)
            try:
                from app.ring_coordinator import ring_coordinator
                # Mark coordinator window as verified (shadow mode: no ring, just confirmation)
                with ring_coordinator._lock:
                    w = ring_coordinator._active_window
                    if w is not None and not w.get("verified", False):
                        elapsed = chime_timestamp - w["start_time"]
                        if 0 <= elapsed <= ring_coordinator.verification_window_sec:
                            delta_ms = round(elapsed * 1000, 1)
                            w["verified"] = True
                            w["second_source"] = "NETWORK_SNIFF"
                            ring_coordinator.recent_verifications.append({
                                "timestamp": datetime.datetime.now().isoformat(),
                                "status": "VERIFIED",
                                "first_source": w["first_source"],
                                "second_source": "NETWORK_SNIFF",
                                "delta_ms": delta_ms,
                                "details": f"Dubbele aanbel-bevestiging: {w['first_source']} + NETWORK_SNIFF (shadow learner)",
                            })
                            logger.info(
                                "🎉 [RING VERIFIED] Shazam + Sniffer cloud-burst correlatie bevestigd! "
                                "delta=%.1fms, dst=%s:%d",
                                delta_ms, best_burst["dst_ip"], best_burst["dst_port"],
                            )
            except Exception as ve:
                logger.debug("[Sniffer] Could not notify ring_coordinator of verified burst: %s", ve)
        else:
            logger.info("ℹ️ [Sniffer Auto-Calibrate] No external cloud burst in +/-2.5s window to correlate.")

        # 3. Dual-Trigger Precision Timing Registration (v3.5.5)
        chime_cet_str = datetime.datetime.fromtimestamp(chime_timestamp, tz=AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        session_candidates = [
            c for c in list(self.learned_chime_candidates)
            if abs(c.get("chime_timestamp", 0.0) - chime_timestamp) < 0.05
        ]

        timing_target = None
        timing_proto = None
        timing_len = None
        timing_hex = ""
        timing_reason = "GEEN_NETWERK_SIGNAAL"
        sniffer_cet = None
        delta_val = None
        in_win = False
        timing_status = "CHIME_ONLY"

        if session_candidates:
            # Pick earliest matching packet (closest to button press)
            best_cand = min(session_candidates, key=lambda c: c.get("delta_seconds", 0.0))
            sniffer_cet = best_cand.get("timestamp")
            delta_val = best_cand.get("delta_seconds")
            in_win = (-4.5 <= delta_val <= 1.5) if delta_val is not None else False
            timing_target = f"{best_cand.get('dst_ip')}:{best_cand.get('dst_port')}"
            timing_proto = best_cand.get("protocol")
            timing_len = best_cand.get("payload_len")
            timing_hex = best_cand.get("hex_payload", "")[:32]
            timing_reason = best_cand.get("reason", "DIFFERENTIAL_MATCH")
            timing_status = "PERFECT_MATCH" if in_win else "OUT_OF_WINDOW"
        elif candidates:
            best_burst = max(candidates, key=lambda x: (x["packet_count"], x["total_bytes"]))
            sniffer_cet = best_burst.get("timestamp")
            try:
                b_ts = datetime.datetime.strptime(best_burst["timestamp"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
                delta_val = round(b_ts - chime_timestamp, 3)
            except Exception:
                delta_val = None
            in_win = (-4.5 <= delta_val <= 1.5) if delta_val is not None else False
            timing_target = f"{best_burst.get('dst_ip')}:{best_burst.get('dst_port')}"
            timing_proto = best_burst.get("protocol")
            timing_len = best_burst.get("total_bytes")
            timing_hex = best_burst.get("payload_hex", "")[:32]
            timing_reason = "BURST_CORRELATION"
            timing_status = "PERFECT_MATCH" if in_win else "OUT_OF_WINDOW"

        timing_record = {
            "id": f"timing-{int(chime_timestamp * 1000)}",
            "timestamp_cet": chime_cet_str,
            "t_shazam_float": chime_timestamp,
            "t_shazam_cet": chime_cet_str,
            "t_sniffer_cet": sniffer_cet,
            "delta_seconds": delta_val,
            "in_window": in_win,
            "window_min": -4.5,
            "window_max": 1.5,
            "target": timing_target,
            "protocol": timing_proto,
            "payload_len": timing_len,
            "hex_preview": timing_hex,
            "reason": timing_reason,
            "status": timing_status,
        }

        with self._window_lock:
            self.recent_timing_records.append(timing_record)

        try:
            os.makedirs(os.path.dirname(TIMING_LOG_FILE), exist_ok=True)
            with open(TIMING_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(timing_record, ensure_ascii=False) + "\n")
        except Exception as te:
            logger.debug("Failed saving timing log: %s", te)

        delta_disp = f"{delta_val:+.3f}s" if delta_val is not None else "N/A"
        logger.info(
            "⏱️ [Dual-Trigger Timing] Shazam: %s | Sniffer: %s | ΔT: %s | Window [-4.5s, +1.5s]: %s | Status: %s",
            chime_cet_str,
            sniffer_cet or "Geen",
            delta_disp,
            "JA" if in_win else "NEE",
            timing_status,
        )

    def _dispatch_network_ring(self, burst: dict) -> None:
        """Dispatches verified network ring to coordinator."""
        try:
            from app.ring_coordinator import register_ring_signal
            register_ring_signal(source="NETWORK_SNIFF", details=burst)
        except Exception as e:
            logger.error("Failed dispatching network ring: %s", e)

    def get_learner_status(self) -> dict:
        """Returns the current status of the Differential Byte Learner."""
        with self._window_lock:
            return {
                "enabled": True,
                "mode": "Active Hybrid Trigger (0.1s HEX Match + Shazam Vangnet)",
                "background_noise_count": len(self.background_noise_hashes),
                "buffered_packets_count": len(self._payload_buffer),
                "learned_candidates_count": len(self.learned_chime_candidates),
                "recent_candidates": list(self.learned_chime_candidates[-10:]),
                "last_chime_ts": self._last_shazam_ring_time,
            }

    def get_timing_analysis(self) -> dict:
        """Returns Dual-Trigger Precision Timing metrics and events (v3.5.5)."""
        with self._window_lock:
            records = list(self.recent_timing_records)

        matched = [r for r in records if r.get("delta_seconds") is not None]
        deltas = [r["delta_seconds"] for r in matched]
        avg_delta = round(sum(deltas) / len(deltas), 3) if deltas else None
        min_delta = min(deltas) if deltas else None
        max_delta = max(deltas) if deltas else None

        return {
            "status": "ok",
            "configured_window": {
                "min": -4.5,
                "max": 1.5,
                "description": "Netwerk verwacht tussen 4.5s vóór en 1.5s na Shazam chime",
            },
            "total_events": len(records),
            "matched_count": len(matched),
            "stats": {
                "avg_delta_seconds": avg_delta,
                "min_delta_seconds": min_delta,
                "max_delta_seconds": max_delta,
            },
            "events": list(reversed(records)),
        }

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

