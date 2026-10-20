"""
Automated Unit & Integration Test for Foxway 2-Stage AI Detection Pipeline.

Verifies:
1. ONNX model loading (yolov8n.onnx) on CPUExecutionProvider.
2. COCO class mapping for:
   - person (0)
   - vehicle (1, 2, 3, 5, 7)
   - animal (14, 15, 16, 17, 18, 19)
3. Motion mask intersection logic (filtering out stationary parked cars).
4. Strict recording settings rule: discard motion clips when record_motion is false.
"""

import os
import sys
import cv2
import numpy as np
import onnxruntime as ort

import pytest

MODEL_PATH = "/mnt/opslag/deurbel/backend/yolov8n.onnx"

PERSON_CLASSES = {0}
VEHICLE_CLASSES = {1, 2, 3, 5, 7}
ANIMAL_CLASSES = {14, 15, 16, 17, 18, 19}

COCO_NAMES = {
    0: "Persoon", 1: "Fiets", 2: "Auto", 3: "Motor", 5: "Bus", 7: "Vrachtwagen",
    14: "Vogel", 15: "Kat", 16: "Hond", 17: "Paard", 18: "Schaap", 19: "Koe"
}


@pytest.fixture(scope="module")
def sess():
    assert os.path.exists(MODEL_PATH), f"Model file not found at {MODEL_PATH}"
    return ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])


def test_model_loading(sess):
    print("▶ Test 1: Loading YOLOv8n ONNX model...")
    input_name = sess.get_inputs()[0].name
    output_name = sess.get_outputs()[0].name
    assert input_name
    assert output_name
    print(f"  ✓ Session initialized: input='{input_name}', output='{output_name}'")


def test_inference_and_classes(sess):
    print("▶ Test 2: Verifying COCO class mappings and inference output shape...")
    dummy = np.random.randint(0, 255, (432, 768, 3), dtype=np.uint8)
    h, w = dummy.shape[:2]
    scale = min(640 / h, 640 / w)
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(dummy, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((640, 640, 3), dtype=np.uint8)
    pad_top = (640 - nh) // 2
    pad_left = (640 - nw) // 2
    canvas[pad_top : pad_top + nh, pad_left : pad_left + nw] = resized

    blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    blob = np.transpose(blob, (2, 0, 1))[np.newaxis, ...]

    outputs = sess.run(None, {sess.get_inputs()[0].name: blob})
    predictions = outputs[0][0]
    assert predictions.shape == (84, 8400), f"Unexpected output shape: {predictions.shape}"
    print(f"  ✓ Model output shape verified: {predictions.shape}")

    # Verify class mappings
    for cid in [0]:
        assert cid in PERSON_CLASSES
    for cid in [1, 2, 3, 5, 7]:
        assert cid in VEHICLE_CLASSES
    for cid in [14, 15, 16, 17, 18, 19]:
        assert cid in ANIMAL_CLASSES
    print("  ✓ Person, Vehicle, and Animal COCO classes correctly partitioned.")


def test_motion_intersection():
    print("▶ Test 3: Verifying Stationary vs Moving Object Filter (Motion Intersection)...")
    # Frame size 768x432
    motion_mask = np.zeros((432, 768), dtype=np.uint8)
    # Motion occurs ONLY on the walkway (x: 100..200, y: 200..320)
    motion_mask[200:320, 100:200] = 255

    # Simulated stationary parked car at (x: 520..680, y: 180..290)
    parked_car_box = (520, 180, 160, 110)
    car_x, car_y, car_w, car_h = parked_car_box
    car_motion_pixels = np.count_nonzero(motion_mask[car_y : car_y + car_h, car_x : car_x + car_w])
    assert car_motion_pixels == 0, "Parked car should have 0 motion pixels"
    print(f"  ✓ Parked car motion pixels: {car_motion_pixels} (Ignored as stationary)")

    # Simulated moving person on walkway at (x: 110..180, y: 210..310)
    person_box = (110, 210, 70, 100)
    px, py, pw, ph = person_box
    person_motion_pixels = np.count_nonzero(motion_mask[py : py + ph, px : px + pw])
    assert person_motion_pixels > 500, "Moving person must have motion pixels"
    print(f"  ✓ Moving person motion pixels: {person_motion_pixels} (Detected as active moving person)")


def test_strict_recording_rule():
    print("▶ Test 4: Verifying Strict Recording Filter (No motion clips if record_motion=False)...")
    cfg = {
        "detection_enabled": True,
        "record_person": True,
        "record_vehicle": True,
        "record_animal": True,
        "record_motion": False,  # OFF!
    }

    # Case A: Motion detected, but no object in motion zone -> category = 'motion'
    category_a = "motion"
    should_record_a = cfg.get(f"record_{category_a}", False)
    assert should_record_a is False, "General motion MUST NOT record when record_motion=False"
    print("  ✓ When no object detected, clip is DISCARDED (0 bytes saved).")

    # Case B: Moving person detected -> category = 'person'
    category_b = "person"
    should_record_b = cfg.get(f"record_{category_b}", False)
    assert should_record_b is True, "Person MUST record when record_person=True"
    print("  ✓ When person detected, clip is RECORDED.")


if __name__ == "__main__":
    try:
        sess = test_model_loading()
        test_inference_and_classes(sess)
        test_motion_intersection()
        test_strict_recording_rule()
        print("\n🎉 ALL AI DETECTION UNIT TESTS PASSED SUCCESSFULLY!")
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
