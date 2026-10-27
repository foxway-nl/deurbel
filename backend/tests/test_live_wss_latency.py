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


async def measure_first_frame_latency(url: str, timeout: float = 8.0) -> dict:
    t0 = time.perf_counter()
    async with websockets.connect(url, open_timeout=timeout) as ws:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            remaining = max(0.5, deadline - time.perf_counter())
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            assert len(msg) > 1, "Received empty message"
            if msg[0] == 1:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                payload = msg[1:]
                # Validate JPEG header (SOI 0xFFD8) and trailer (EOI 0xFFD9)
                assert payload.startswith(b"\xff\xd8"), "Invalid JPEG SOI header"
                assert payload.endswith(b"\xff\xd9"), "Invalid JPEG EOI trailer"
                return {
                    "url": url,
                    "latency_ms": elapsed_ms,
                    "frame_size_bytes": len(payload),
                }
        raise TimeoutError(f"No JPEG frame received from {url} within {timeout}s")


async def ensure_stream_warm(timeout: float = 45.0):
    import httpx
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get("http://127.0.0.1:8090/api/v1/live/instant.jpg", timeout=2.0)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


@pytest.mark.asyncio
async def test_lan_ws_latency():
    warm = await ensure_stream_warm()
    assert warm, "Stream was not warm"
    result = await measure_first_frame_latency(LAN_WS_URL)
    print(f"\n[LAN WS TEST] Latency: {result['latency_ms']:.2f}ms, Frame: {result['frame_size_bytes']} bytes")
    assert result["latency_ms"] < 300.0, f"LAN latency {result['latency_ms']}ms exceeded 300ms limit"


@pytest.mark.asyncio
async def test_wan_wss_latency():
    warm = await ensure_stream_warm()
    assert warm, "Stream was not warm"
    result = await measure_first_frame_latency(WAN_WSS_URL)
    print(f"\n[WAN WSS TEST] Latency: {result['latency_ms']:.2f}ms, Frame: {result['frame_size_bytes']} bytes")
    assert result["latency_ms"] < 300.0, f"WAN latency {result['latency_ms']}ms exceeded 300ms limit"


@pytest.mark.asyncio
async def test_live_hub_anti_freeze_architecture(async_client):
    from app.live_hub import live_hub
    import zlib

    # 1. Fresh frame validation
    test_frame = b"\xff\xd8\xff\xe0test_fresh_frame_bytes\xff\xd9"
    live_hub.latest_jpeg = test_frame
    live_hub.last_fresh_frame_time = time.monotonic()
    assert live_hub.is_fresh(max_age_sec=2.5) is True
    assert live_hub.get_fresh_jpeg(max_age_sec=2.5) == test_frame

    # 2. Stale frame (> 2.5s) MUST NOT be returned (prevents sending 21-min old frozen frames)
    live_hub.last_fresh_frame_time = time.monotonic() - 3.0
    assert live_hub.is_fresh(max_age_sec=2.5) is False
    assert live_hub.get_fresh_jpeg(max_age_sec=2.5) is None

    # 3. Restart endpoint triggers clean supervisor restart
    resp = await async_client.post("/api/v1/live/restart")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


if __name__ == "__main__":
    lan_res = asyncio.run(measure_first_frame_latency(LAN_WS_URL))
    print(f"LAN WS Latency: {lan_res['latency_ms']:.2f}ms ({lan_res['frame_size_bytes']} bytes)")

    wan_res = asyncio.run(measure_first_frame_latency(WAN_WSS_URL))
    print(f"WAN WSS Latency: {wan_res['latency_ms']:.2f}ms ({wan_res['frame_size_bytes']} bytes)")

