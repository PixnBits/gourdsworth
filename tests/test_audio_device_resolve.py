"""Name-based sounddevice resolve for Pi crate (USB mic + PipeWire/JBL out)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "clients" / "pi" / "audio_devices.py"


def _load():
    spec = importlib.util.spec_from_file_location("audio_devices_under_test", _MOD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _devs():
    return [
        {"name": "bcm2835 Headphones: - (hw:0,0)", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "USB PnP Sound Device: Audio (hw:1,0)", "max_input_channels": 1, "max_output_channels": 2},
        {"name": "BRIO Ultra HD Pro: USB Audio (hw:2,0)", "max_input_channels": 2, "max_output_channels": 0},
        {"name": "pipewire", "max_input_channels": 64, "max_output_channels": 64},
        {"name": "default", "max_input_channels": 64, "max_output_channels": 64},
    ]


def test_match_prefers_default_before_pipewire():
    mod = _load()
    assert mod.match_sounddevice_index(_devs(), role="output", substrings=("default", "pipewire")) == 4
    assert mod.match_sounddevice_index(_devs(), role="output", substrings=("pipewire",)) == 3


def test_match_usb_input_skips_output_only():
    mod = _load()
    assert mod.match_sounddevice_index(_devs(), role="input", substrings=("USB PnP", "CM108")) == 1


def test_resolve_ints_win_over_names():
    mod = _load()
    assert mod.resolve_audio_device_ids(2, 0, input_name="USB PnP", output_name="default", devices=_devs()) == (2, 0)


def test_resolve_auto_hints_when_unset():
    mod = _load()
    assert mod.resolve_audio_device_ids(None, None, devices=_devs()) == (1, 4)


def test_resolve_explicit_output_name():
    mod = _load()
    assert mod.resolve_audio_device_ids(None, None, output_name="pipewire", devices=_devs()) == (1, 3)
