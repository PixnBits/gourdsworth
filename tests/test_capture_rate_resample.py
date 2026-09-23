"""Unit tests for CM108 capture-rate chunk sizing and uplink resample."""

from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "pi"))
sys.path.insert(0, str(ROOT / "src"))

import crate_client as cc  # noqa: E402
from gourdsworth.net import UPLINK_CHUNK_SAMPLES, UPLINK_RATE  # noqa: E402


def test_capture_chunk_samples_keeps_wall_clock():
    assert cc._capture_chunk_samples(UPLINK_RATE) == UPLINK_CHUNK_SAMPLES
    assert cc._capture_chunk_samples(48000) == UPLINK_CHUNK_SAMPLES * 48000 // UPLINK_RATE
    assert cc._capture_chunk_samples(44100) == int(
        round(UPLINK_CHUNK_SAMPLES * 44100 / UPLINK_RATE)
    )
    # 100 ms at 48 kHz
    assert cc._capture_chunk_samples(48000) == 4800


def test_resample_f32_down_to_uplink():
    n = 4800  # 100 ms @ 48 kHz
    t = np.linspace(0.0, 0.1, num=n, endpoint=False)
    x = np.sin(2.0 * np.pi * 440.0 * t).astype(np.float32)
    y, rate = cc._resample_f32(x, 48000, UPLINK_RATE)
    assert rate == UPLINK_RATE
    assert abs(y.size - UPLINK_CHUNK_SAMPLES) <= 1


def test_i16_to_uplink_passthrough_and_down():
    src = np.arange(100, dtype="<i2")
    out = cc._i16_to_uplink(src, UPLINK_RATE)
    assert out.dtype == np.dtype("<i2")
    assert out.shape == (100,)
    assert np.array_equal(out, src)

    # 4800 samples @ 48k -> ~1600 @ 16k
    big = (np.sin(np.linspace(0, 20, 4800)) * 10000).astype("<i2")
    down = cc._i16_to_uplink(big, 48000)
    assert down.dtype == np.dtype("<i2")
    assert abs(down.size - UPLINK_CHUNK_SAMPLES) <= 1
