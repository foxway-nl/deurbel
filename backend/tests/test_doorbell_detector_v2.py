"""
Tests for doorbell_detector.py v2 rule-based detector.
"""
import os
import pytest
import numpy as np

from app.doorbell_detector import (
    SR,
    Params,
    build_template,
    Detector,
    scan_array,
    load_audio,
)
from app.bell_service import bell_service


def test_build_template_v2():
    t = build_template()
    assert isinstance(t, Params)
    assert t.ta == 22.0
    assert t.tb == 15.0


def test_bell_service_template_initialization():
    bell_service.template = None
    bell_service._ensure_template()
    assert isinstance(bell_service.template, Params)


def test_doorbell_detection_on_bel_wav():
    ref_path = "/mnt/opslag/deurbel/backend/app/assets/bel.wav"
    if not os.path.exists(ref_path):
        pytest.skip("bel.wav not found")

    audio = load_audio(ref_path)
    events, best_score = scan_array(audio, threshold=0.30, cooldown=8.0)
    assert len(events) >= 1, f"Expected at least 1 detection, got {len(events)}"
    t_det, score = events[0]
    assert score >= 0.90, f"Expected score >= 0.90, got {score}"
    assert best_score >= 0.90
