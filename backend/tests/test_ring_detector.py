import numpy as np
import pytest
from app.ring_detector import DoorbellRingDetector


def generate_tone(freq: float, duration_samples: int = 2000, sample_rate: int = 16000, amplitude: float = 25000.0) -> np.ndarray:
    t = np.arange(duration_samples) / sample_rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def generate_white_noise(duration_samples: int = 2000, amplitude: float = 20000.0) -> np.ndarray:
    return np.random.randint(-int(amplitude), int(amplitude), size=duration_samples, dtype=np.int16)


def test_single_tone_scream_does_not_trigger():
    detector = DoorbellRingDetector()
    t0 = 1000.0

    # Shouting child or loud noise spike around 728 Hz
    tone1 = generate_tone(728.0)
    res1 = detector.process_samples(tone1, t0)
    assert not res1, "Tone 1 alone must not trigger a ring"

    # Followed by noise or silence
    silence = np.zeros(2000, dtype=np.int16)
    res2 = detector.process_samples(silence, t0 + 0.3)
    assert not res2, "Silence after scream must not trigger a ring"

    # Broad spectrum shout
    scream = generate_white_noise()
    res3 = detector.process_samples(scream, t0 + 0.5)
    assert not res3, "Broad spectrum shout must not trigger a ring"


def test_dual_tone_too_fast_or_too_slow_does_not_trigger():
    detector = DoorbellRingDetector()
    t0 = 1000.0

    tone1 = generate_tone(728.0)
    tone2 = generate_tone(580.0)

    # Tone 1
    assert not detector.process_samples(tone1, t0)

    # Tone 2 too fast (< 200ms)
    assert not detector.process_samples(tone2, t0 + 0.05)

    # Reset
    detector = DoorbellRingDetector()
    assert not detector.process_samples(tone1, t0)

    # Tone 2 too slow (> 900ms)
    assert not detector.process_samples(tone2, t0 + 1.2)


def test_dual_tone_ding_dong_triggers_ring():
    detector = DoorbellRingDetector()
    t0 = 1000.0

    tone1 = generate_tone(728.0)
    tone2 = generate_tone(580.0)

    # Step 1: Ding (728 Hz)
    assert not detector.process_samples(tone1, t0)

    # Step 2: Dong (580 Hz) at 400ms after Ding
    triggered = detector.process_samples(tone2, t0 + 0.40)
    assert triggered, "Ding followed by Dong at 400ms MUST trigger the doorbell ring"
