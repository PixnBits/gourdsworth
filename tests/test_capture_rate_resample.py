"""Capture-rate helpers for CM108 (native 48k → 16 kHz uplink)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "clients" / "pi"))
sys.path.insert(0, str(_REPO / "src"))

import crate_client as cc  # noqa: E402
from gourdsworth.net import UPLINK_CHUNK_SAMPLES, UPLINK_RATE  # noqa: E402


def test_capture_chunk_samples_48k() -> None:
    assert cc._capture_chunk_samples(48000) == int(
        round(UPLINK_CHUNK_SAMPLES * 48000 / UPLINK_RATE)
    )
    assert cc._capture_chunk_samples(48000) == 4800


def test_capture_chunk_samples_identity() -> None:
    assert cc._capture_chunk_samples(UPLINK_RATE) == UPLINK_CHUNK_SAMPLES


def test_i16_to_uplink_identity() -> None:
    arr = np.arange(100, dtype=np.int16)
    out = cc._i16_to_uplink(arr, UPLINK_RATE)
    assert out.dtype == np.dtype("<i2")
    assert out.shape == (100,)
    np.testing.assert_array_equal(out, arr)


def test_i16_to_uplink_downsample_length() -> None:
    arr = (np.sin(np.linspace(0, 8 * np.pi, 4800)) * 10000).astype(np.int16)
    out = cc._i16_to_uplink(arr, 48000)
    assert out.dtype == np.dtype("<i2")
    assert abs(out.size - 1600) <= 1
