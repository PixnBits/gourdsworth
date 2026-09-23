"""WAV write helper for PipeWire pw-play playback path."""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "clients" / "pi"))
sys.path.insert(0, str(_REPO / "src"))

import crate_client as cc  # noqa: E402


def test_write_pcm_f32_wav_mono(tmp_path: Path) -> None:
    rate = 22050
    samples = (np.sin(np.linspace(0, 2 * np.pi, rate // 10)) * 0.5).astype(np.float32)
    out = tmp_path / "tone.wav"
    cc._write_pcm_f32_wav(out, samples, rate)
    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == rate
        assert wf.getnframes() == samples.size
    raw = out.read_bytes()
    assert len(raw) > 44


def test_write_pcm_f32_wav_stereo(tmp_path: Path) -> None:
    rate = 16000
    t = np.linspace(0, 1, rate, endpoint=False, dtype=np.float32)
    stereo = np.stack([0.25 * t, -0.25 * t], axis=1)
    out = tmp_path / "stereo.wav"
    cc._write_pcm_f32_wav(out, stereo, rate)
    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getnframes() == rate
