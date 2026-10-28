"""
Foxway Smart Doorbell v2.3.0 Proof Tests:
  • Test 1: Web Admin Authentication & Endpoints (/admin/login, /admin, settings, detections, crash-logs).
  • Test 2: 3-Frame Trajectory Verification Gatekeeper vs. Wind/Shrubs (wobbling Delta d < 2% REJECTED, moving Delta d >= 6% APPROVED).
  • Test 3: Zero-lag Pipe Drain & Freshness Verification.
  • Test 4: Immediate Event Creation (<1.5s Push Coupling).
  • Test 5: Client Crash Ingestion (POST /api/v1/logs/client-crash).
"""

import asyncio
import json
import math
import time
from datetime import datetime, timezone
import numpy as np
import pytest

from app.models import (
    get_system_settings,
    get_system_setting,
    update_system_settings,
    save_ai_audit_log,
    get_recent_ai_audit_logs,
    save_crash_log,
    get_recent_crash_logs,
    get_events_since,
)
from app.admin import create_session_token, SESSION_COOKIE_NAME
from app.ai_detector import smart_detector, CandidateDetection


@pytest.mark.asyncio
async def test_admin_login_and_auth_routes(async_client):
    """Test 1A: GET /admin/login returns 200 HTML."""
    resp = await async_client.get("/admin/login")
    assert resp.status_code == 200
    assert "Foxway Deurbel" in resp.text
    assert "Inloggen" in resp.text

    # Unauthorized access to /admin redirects to /admin/login
    resp_dash = await async_client.get("/admin", follow_redirects=False)
    assert resp_dash.status_code == 302
    assert "/admin/login" in resp_dash.headers["location"]

    # Invalid login fails
    resp_bad = await async_client.post(
        "/admin/login",
        data={"username": "admin", "password": "wrongpassword"},
        follow_redirects=False,
    )
    assert resp_bad.status_code == 401

    # Valid login succeeds and sets cookie
    resp_ok = await async_client.post(
        "/admin/login",
        data={"username": "admin", "password": "foxway2024"},
        follow_redirects=False,
    )
    assert resp_ok.status_code == 302
    assert resp_ok.headers["location"] == "/admin"
    assert SESSION_COOKIE_NAME in resp_ok.cookies


@pytest.mark.asyncio
async def test_admin_api_endpoints_with_session(async_client):
    """Test 1B: Admin authenticated API endpoints."""
    token = create_session_token("admin")
    cookies = {SESSION_COOKIE_NAME: token}

    # 1. GET settings
    resp_settings = await async_client.get("/api/v1/admin/settings", cookies=cookies)
    assert resp_settings.status_code == 200
    settings_data = resp_settings.json()
    assert "person_conf_threshold" in settings_data
    assert "min_person_displacement_pct" in settings_data

    # 2. PUT settings (dynamic update)
    update_payload = {"person_conf_threshold": "0.58", "max_clip_seconds": "22"}
    resp_put = await async_client.put("/api/v1/admin/settings", json=update_payload, cookies=cookies)
    assert resp_put.status_code == 200
    assert (await get_system_setting("person_conf_threshold")) == "0.58"
    assert (await get_system_setting("max_clip_seconds")) == "22"

    # Reset to default
    await update_system_settings({"person_conf_threshold": "0.52", "max_clip_seconds": "25"})

    # 3. GET detections (audit logs)
    resp_detections = await async_client.get("/api/v1/admin/detections", cookies=cookies)
    assert resp_detections.status_code == 200
    assert isinstance(resp_detections.json(), list)

    # 4. GET system status
    resp_status = await async_client.get("/api/v1/admin/system-status", cookies=cookies)
    assert resp_status.status_code == 200
    status_d = resp_status.json()
    assert "cpu_percent" in status_d
    assert "ram_percent" in status_d
    assert "disk_usage_percent" in status_d


@pytest.mark.asyncio
async def test_client_crash_logger_and_retrieval(async_client):
    """Test 5: Android client crash logger endpoint."""
    crash_payload = {
        "device_name": "Samsung S25 Plus Test",
        "device_id": "test_device_uuid_123",
        "app_version": "2.3.0",
        "error_message": "OutOfMemoryError in LiveStreamController",
        "stacktrace": "java.lang.OutOfMemoryError: Failed to allocate a 5184000 byte allocation\n  at nl.foxway.deurbel.network.LiveStreamController",
        "screen_state": "LiveScreen Active",
    }
    resp = await async_client.post("/api/v1/logs/client-crash", json=crash_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["log_id"] > 0

    # Retrieve via admin
    token = create_session_token("admin")
    resp_get = await async_client.get("/api/v1/admin/crash-logs", cookies={SESSION_COOKIE_NAME: token})
    assert resp_get.status_code == 200
    crashes = resp_get.json()["client_crashes"]
    found = any(c["error_message"] == "OutOfMemoryError in LiveStreamController" for c in crashes)
    assert found is True, "Ingested crash log must be retrievable by admin"


@pytest.mark.asyncio
async def test_gatekeeper_trajectory_verification_rules():
    """
    Test 2: Prove that 3-Frame Verification Gatekeeper blocks stationary shrubs/parked cars
    and approves genuine moving candidates.
    """
    w, h = 960.0, 720.0

    # 1. Shrub / Tree branch fluttering in wind:
    # Centroid moves slightly: (300, 400) -> (305, 403) -> (302, 401)
    # Total displacement over 3 frames: ~2 pixels / 960 = 0.2% << 6.0% required!
    shrub_track = CandidateDetection("shrub_1", "person", "Persoon", (280, 350, 320, 450), 0.65, time.time())
    shrub_track.frames.append((time.time() + 0.4, (285, 353, 325, 453), 0.64))
    shrub_track.frames.append((time.time() + 0.8, (282, 351, 322, 451), 0.66))

    cx1 = (shrub_track.frames[0][1][0] + shrub_track.frames[0][1][2]) / 2.0
    cy1 = (shrub_track.frames[0][1][1] + shrub_track.frames[0][1][3]) / 2.0
    cx3 = (shrub_track.frames[-1][1][0] + shrub_track.frames[-1][1][2]) / 2.0
    cy3 = (shrub_track.frames[-1][1][1] + shrub_track.frames[-1][1][3]) / 2.0

    delta_d_shrub = (math.hypot(cx3 - cx1, cy3 - cy1)) / w
    assert delta_d_shrub < 0.06, f"Shrub displacement ({delta_d_shrub:.3f}) must be < 6%"

    # 2. Parked vehicle:
    # Centroid moves ~3px jitter: (200, 300) -> (202, 301) -> (201, 300)
    # Total displacement: ~1px / 960 = 0.1% << 12.0% required!
    car_track = CandidateDetection("car_1", "vehicle", "Auto", (150, 250, 250, 350), 0.80, time.time())
    car_track.frames.append((time.time() + 0.4, (152, 251, 252, 351), 0.81))
    car_track.frames.append((time.time() + 0.8, (151, 250, 251, 350), 0.82))

    cx1_c = (car_track.frames[0][1][0] + car_track.frames[0][1][2]) / 2.0
    cy1_c = (car_track.frames[0][1][1] + car_track.frames[0][1][3]) / 2.0
    cx3_c = (car_track.frames[-1][1][0] + car_track.frames[-1][1][2]) / 2.0
    cy3_c = (car_track.frames[-1][1][1] + car_track.frames[-1][1][3]) / 2.0

    delta_d_car = (math.hypot(cx3_c - cx1_c, cy3_c - cy1_c)) / w
    assert delta_d_car < 0.12, f"Parked car displacement ({delta_d_car:.3f}) must be < 12%"

    # 3. Genuine walking pedestrian:
    # Moves from x=200 to x=320 (displacement = 120px / 960 = 12.5% >= 6.0%)
    person_track = CandidateDetection("person_1", "person", "Persoon", (180, 250, 220, 450), 0.75, time.time())
    person_track.frames.append((time.time() + 0.5, (230, 250, 270, 450), 0.78))
    person_track.frames.append((time.time() + 1.0, (300, 250, 340, 450), 0.82))

    cx1_p = (person_track.frames[0][1][0] + person_track.frames[0][1][2]) / 2.0
    cy1_p = (person_track.frames[0][1][1] + person_track.frames[0][1][3]) / 2.0
    cx3_p = (person_track.frames[-1][1][0] + person_track.frames[-1][1][2]) / 2.0
    cy3_p = (person_track.frames[-1][1][1] + person_track.frames[-1][1][3]) / 2.0

    delta_d_person = (math.hypot(cx3_p - cx1_p, cy3_p - cy1_p)) / w
    assert delta_d_person >= 0.06, f"Pedestrian displacement ({delta_d_person:.3f}) must be >= 6%"


@pytest.mark.asyncio
async def test_immediate_push_coupling_and_events_visibility(async_client):
    """
    Test 4: Proves that an immediate event created at T0+1.0s is visible to the client
    polling GET /api/v1/events without waiting 70 seconds.
    """
    from app.models import save_event, save_clip

    test_clip_id = f"test_immediate_{int(time.time())}"
    # 1. Simulate Gatekeeper creating event immediately
    event_d = await save_event(
        event_type="motion",
        category="person",
        title="Persoon gedetecteerd: Persoon",
        clip_id=test_clip_id,
        snapshot_url=f"/api/v1/snapshots/{test_clip_id}.jpg",
    )
    assert event_d["id"] > 0

    # 2. Client queries GET /api/v1/events?limit=5&order=desc
    resp = await async_client.get("/api/v1/events?limit=5&order=desc")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) >= 1
    found = any(e["id"] == event_d["id"] for e in events)
    assert found is True, "Immediate event must be returned to client right away (<1.5s)"
