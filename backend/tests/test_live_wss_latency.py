"""
Latency test verifying instant first-frame delivery on both LAN and WAN/5G WebSocket routes.
Hard Requirement: < 300ms to first valid 4:3 JPEG frame.
"""

import asyncio
import time
import pytest
import websockets

LAN_WS_URL = "ws://127.0.0.1:8090/api/v1/ws/live"
WAN_WSS_URL = "wss://deurbel.foxway.nl/api/v1/ws/live"


async def measure_first_frame_latency(url: str, timeout: float = 3.0) -> dict:
    t0 = time.perf_counter()
    async with websockets.connect(url, open_timeout=timeout) as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        assert len(msg) > 1, "Received empty message"
        msg_type = msg[0]
        payload = msg[1:]

        # Validate that byte 0 is 0x01 (JPEG frame)
        assert msg_type == 1, f"Expected 0x01 (JPEG), got {msg_type}"
        # Validate JPEG header (SOI 0xFFD8) and trailer (EOI 0xFFD9)
        assert payload.startswith(b"\xff\xd8"), "Invalid JPEG SOI header"
        assert payload.endswith(b"\xff\xd9"), "Invalid JPEG EOI trailer"

        return {
            "url": url,
            "latency_ms": elapsed_ms,
            "frame_size_bytes": len(payload),
        }


@pytest.mark.asyncio
async def test_lan_ws_latency():
    result = await measure_first_frame_latency(LAN_WS_URL)
    print(f"\n[LAN WS TEST] Latency: {result['latency_ms']:.2f}ms, Frame: {result['frame_size_bytes']} bytes")
    assert result["latency_ms"] < 300.0, f"LAN latency {result['latency_ms']}ms exceeded 300ms limit"


@pytest.mark.asyncio
async def test_wan_wss_latency():
    result = await measure_first_frame_latency(WAN_WSS_URL)
    print(f"\n[WAN WSS TEST] Latency: {result['latency_ms']:.2f}ms, Frame: {result['frame_size_bytes']} bytes")
    assert result["latency_ms"] < 300.0, f"WAN latency {result['latency_ms']}ms exceeded 300ms limit"


if __name__ == "__main__":
    lan_res = asyncio.run(measure_first_frame_latency(LAN_WS_URL))
    print(f"LAN WS Latency: {lan_res['latency_ms']:.2f}ms ({lan_res['frame_size_bytes']} bytes)")

    wan_res = asyncio.run(measure_first_frame_latency(WAN_WSS_URL))
    print(f"WAN WSS Latency: {wan_res['latency_ms']:.2f}ms ({wan_res['frame_size_bytes']} bytes)")
