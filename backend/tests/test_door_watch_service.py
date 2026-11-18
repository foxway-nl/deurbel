"""
Tests for door_watch.py and door_service.py integration.
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.door_service import DoorService
from app.door_watch import YoloOnnx, load_zones, poly_mask, Tracker, MotionGate
import numpy as np


def test_door_service_defaults():
    ds = DoorService()
    assert "deurbel_sub" in ds.rtsp_sub_url
    assert ds.fps == 3.0
    assert ds.imgsz == 320
    assert ds.threads == 2
    cmd = ds._build_command()
    assert "--rtsp" in cmd
    assert "--model" in cmd
    assert "--zones" in cmd
    assert "--webhook" in cmd
    assert "--snap-dir" in cmd


def test_poly_mask_and_zones():
    shape = (360, 640)
    zones_json = {
        "watch": [[[0.0, 0.20], [1.0, 0.20], [1.0, 1.0], [0.0, 1.0]]],
        "ignore": [[[0.75, 0.0], [1.0, 0.0], [1.0, 0.45], [0.75, 0.45]]],
    }
    tmp_path = "/tmp/test_zones.json"
    with open(tmp_path, "w") as f:
        json.dump(zones_json, f)

    mask = load_zones(tmp_path, shape)
    assert mask.shape == shape
    # Watch area should be 255
    assert mask[200, 320] == 255
    # Ignore area should be 0
    assert mask[50, 550] == 0
    os.unlink(tmp_path)


def test_motion_gate():
    mask = np.full((180, 320), 255, np.uint8)
    gate = MotionGate(mask, min_blob_frac=0.004)
    black1 = np.zeros((180, 320, 3), dtype=np.uint8)
    black2 = np.zeros((180, 320, 3), dtype=np.uint8)
    # Static frames should not trigger gate
    assert not gate.update(black1)
    assert not gate.update(black2)


def test_yolo_onnx_inference():
    model_path = "/mnt/opslag/deurbel/backend/app/assets/yolo11n.onnx"
    if not os.path.exists(model_path):
        pytest.skip("yolo11n.onnx not found")
    det = YoloOnnx(model_path, imgsz=320, threads=2)
    assert det.size == 320
    dummy = np.zeros((360, 640, 3), dtype=np.uint8)
    res = det.detect(dummy)
    assert isinstance(res, list)
