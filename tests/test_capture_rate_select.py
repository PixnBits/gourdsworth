"""Capture-rate candidate order and probe (CM108 stays 48 kHz; 16 kHz native does not resample)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "clients" / "pi"))
sys.path.insert(0, str(_REPO / "src"))

import crate_client as cc  # noqa: E402
from gourdsworth.net import UPLINK_RATE  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_capture_rate(monkeypatch):
    monkeypatch.setattr(cc, "_CAPTURE_RATE", None)
    monkeypatch.delenv("CRATE_INPUT_RATE", raising=False)
    yield
    cc._CAPTURE_RATE = None


def test_candidates_native_48k_opens_at_48k():
    rates = cc._capture_rate_candidates(48000)
    assert rates[0] == 48000
    assert rates == [48000, 44100, UPLINK_RATE]


def test_candidates_native_16k_opens_at_16k():
    rates = cc._capture_rate_candidates(UPLINK_RATE)
    assert rates[0] == UPLINK_RATE
    assert rates == [UPLINK_RATE, 48000, 44100]


def test_candidates_cm108_advertised_44100_unchanged():
    # Same order the CM108 probe used before the native-16 kHz shortcut.
    assert cc._capture_rate_candidates(44100) == [48000, 44100, UPLINK_RATE]


def test_candidates_override_is_first_and_drops_non_positive():
    assert cc._capture_rate_candidates(48000, 32000) == [32000, 48000, 44100, UPLINK_RATE]
    assert cc._capture_rate_candidates(0) == [48000, 44100, UPLINK_RATE]
    assert cc._capture_rate_candidates(-48000, 0) == [48000, 44100, UPLINK_RATE]


def _install_input(monkeypatch, *, native: int, name: str, accepted: set[int]):
    sd = types.ModuleType("sounddevice")
    sd.default = types.SimpleNamespace(device=(1, 0))
    info = {"name": name, "default_samplerate": float(native)}
    seen: list[dict] = []
    sd.accepted = set(accepted)

    def query_devices(index=None):
        if index is None:
            return [info]
        return info

    def check_input_settings(**kwargs):
        seen.append(dict(kwargs))
        if int(kwargs["samplerate"]) not in sd.accepted:
            raise OSError(f"unsupported rate {kwargs['samplerate']}")

    sd.query_devices = query_devices
    sd.check_input_settings = check_input_settings
    sd.seen = seen
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


def test_probe_brio_48k_resamples_to_uplink(monkeypatch, capsys):
    sd = _install_input(
        monkeypatch,
        native=48000,
        name="BRIO Ultra HD Webcam",
        accepted={48000},
    )
    monkeypatch.setattr(cc, "_CAPTURE_RATE", None)
    rate = cc._probe_capture_rate()
    assert rate == 48000
    assert sd.seen[0]["device"] == 1
    assert cc._capture_chunk_samples(rate) == 4800
    arr = (np.sin(np.linspace(0, 8 * np.pi, 4800)) * 10000).astype(np.int16)
    out = cc._i16_to_uplink(arr, rate)
    assert abs(int(out.size) - 1600) <= 1
    printed = capsys.readouterr().out
    assert "BRIO Ultra HD Webcam" in printed
    assert "resample" in printed


def test_probe_native_16k_no_resample(monkeypatch, capsys):
    _install_input(
        monkeypatch,
        native=16000,
        name="generic mic",
        accepted={16000, 48000},
    )
    monkeypatch.setattr(cc, "_CAPTURE_RATE", None)
    assert cc._probe_capture_rate() == 16000
    assert "no resample" in capsys.readouterr().out


def test_probe_cm108_prefers_48000_over_advertised_44100(monkeypatch):
    _install_input(
        monkeypatch,
        native=44100,
        name="USB PnP Sound Device",
        accepted={44100, 48000},
    )
    monkeypatch.setattr(cc, "_CAPTURE_RATE", None)
    assert cc._probe_capture_rate() == 48000


def test_probe_crate_input_rate_override_and_fallback(monkeypatch, capsys):
    sd = _install_input(
        monkeypatch,
        native=48000,
        name="BRIO Ultra HD Webcam",
        accepted={16000, 48000},
    )
    monkeypatch.setenv("CRATE_INPUT_RATE", "16000")
    monkeypatch.setattr(cc, "_CAPTURE_RATE", None)
    assert cc._probe_capture_rate() == 16000

    sd.accepted.clear()
    sd.accepted.add(48000)
    monkeypatch.setenv("CRATE_INPUT_RATE", "32000")
    monkeypatch.setattr(cc, "_CAPTURE_RATE", None)
    assert cc._probe_capture_rate() == 48000
    out = capsys.readouterr().out
    assert "CRATE_INPUT_RATE=32000" in out
    assert "rejected" in out
