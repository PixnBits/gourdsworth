"""Camera LED map → height header. No serial port, no LEDs, no servos."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tools"))

import ledmap_to_header as ledmap  # noqa: E402

_JSON = _REPO / "hardware" / "ledmap231.json"
_HEADER = _REPO / "firmware" / "esp32" / "src" / "led_height.h"
_MAIN = _REPO / "firmware" / "esp32" / "src" / "main.cpp"
_PY = Path(sys.executable)


def _table(points: list[dict]) -> tuple[list[float], list[int], float, float]:
    kept = [y for point in points if (y := ledmap.measured_y(point)) is not None]
    y_min, y_max = min(kept), max(kept)
    ys = ledmap.fill(points)
    return ys, ledmap.heights(ys, y_min=y_min, y_max=y_max), y_min, y_max


def test_fill_copies_ends_and_interpolates_between_neighbours():
    points = [
        {"i": 0, "x": None, "y": None},
        {"i": 1, "x": 1.0, "y": 10.0},
        {"i": 2, "x": 1.0, "y": 400.0},
        {"i": 3, "x": 1.0, "y": 30.0},
        {"i": 4, "x": None, "y": None},
    ]
    ys = ledmap.fill(points)
    assert ys[0] == pytest.approx(10.0)
    assert ys[1] == pytest.approx(10.0)
    assert ys[2] == pytest.approx(20.0)
    assert ys[3] == pytest.approx(30.0)
    assert ys[4] == pytest.approx(30.0)
    # y == 340 is still on the shell. Just above it is a reflection.
    kept = [
        {"i": 0, "x": 1.0, "y": 340.0},
        {"i": 1, "x": 1.0, "y": 340.1},
    ]
    assert ledmap.measured_y(kept[0]) == pytest.approx(340.0)
    assert ledmap.measured_y(kept[1]) is None
    assert ledmap.heights([10.0, 0.0, 5.0], y_min=0.0, y_max=10.0) == [0, 255, 128]


def test_load_rejects_a_map_that_is_not_231_in_order(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"n": 2, "points": [{"i": 0, "x": 1, "y": 2}, {"i": 1, "x": 1, "y": 3}]}))
    with pytest.raises(ValueError, match="231"):
        ledmap.load(path)
    swapped = [{"i": 1, "x": 1, "y": 2}, {"i": 0, "x": 1, "y": 3}]
    with pytest.raises(ValueError, match="indices"):
        ledmap.fill(swapped)


def test_committed_map_fills_and_normalizes():
    data = ledmap.load(_JSON)
    points = data["points"]
    assert data["n"] == len(points) == 231
    ys, values, y_min, y_max = _table(points)
    assert len(values) == 231
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in values)
    assert all(0 <= value <= 255 for value in values)
    assert min(values) == 0
    assert max(values) == 255
    # The capture's valid shell is about 109.2..276.1. Lock the real ends.
    assert y_min == pytest.approx(109.2)
    assert y_max == pytest.approx(276.1)

    valid: dict[int, float] = {}
    for i, point in enumerate(points):
        y = ledmap.measured_y(point)
        if y is not None:
            valid[i] = y
            assert ys[i] == pytest.approx(y)
    assert valid
    for i, y in enumerate(ys):
        assert y_min - 1e-6 <= y <= y_max + 1e-6
        if i in valid:
            continue
        left = max((j for j in valid if j < i), default=None)
        right = min((j for j in valid if j > i), default=None)
        if left is None:
            assert y == pytest.approx(valid[right])
        elif right is None:
            assert y == pytest.approx(valid[left])
        else:
            t = (i - left) / (right - left)
            expected = valid[left] + (valid[right] - valid[left]) * t
            assert y == pytest.approx(expected)
            lo, hi = sorted((valid[left], valid[right]))
            assert lo - 1e-6 <= y <= hi + 1e-6

    for i, point in enumerate(points):
        y = point.get("y")
        if y == y_max:
            assert values[i] <= 10
        if y == y_min:
            assert values[i] >= 245


def test_committed_header_matches_render_byte_for_byte(tmp_path: Path):
    data = ledmap.load(_JSON)
    points = data["points"]
    _ys, values, _y_min, _y_max = _table(points)
    text = ledmap.render(values, ledmap.filled_indices(points), source="hardware/ledmap231.json")
    assert _HEADER.read_bytes() == text.encode("utf-8")
    assert "static const uint8_t LED_HEIGHT[FACE_LEDS]" in text
    assert 'static_assert(sizeof(LED_HEIGHT) == FACE_LEDS, "LED_HEIGHT length must match FACE_LEDS")' in text
    assert 'static_assert(FACE_LEDS == 231, "LED_HEIGHT is generated for 231 face LEDs")' in text
    assert "#ifndef FACE_LEDS" in text
    assert "#define FACE_LEDS 231" in text
    assert "0, 19-21, 39-41, 60-65, 83, 101-104, 122-129, 145, 164-168, 187-188, 208-212" in text

    body = text.split("LED_HEIGHT[FACE_LEDS] = {", 1)[1].split("};", 1)[0]
    nums = [int(token) for token in re.findall(r"\b\d+\b", body)]
    assert nums == values
    lines = [line for line in body.splitlines() if line.strip()]
    for line in lines[:-1]:
        assert line.count(",") == 12

    out = tmp_path / "led_height.h"
    subprocess.check_call([str(_PY), str(_REPO / "tools" / "ledmap_to_header.py"), str(_JSON), "-o", str(out)])
    assert out.read_bytes() == text.encode("utf-8")


def test_main_idle_uses_led_height_and_speech_is_unchanged():
    src = _MAIN.read_text(encoding="utf-8")
    assert '#include "led_height.h"' in src
    idle = src.split("static void advanceIdle", 1)[1].split("static uint8_t levelToScale", 1)[0]
    assert "LED_HEIGHT" in idle
    speech = src.split("static void advanceSpeech", 1)[1].split("// Rising noise", 1)[0]
    assert "LED_HEIGHT" not in speech
    assert "GLOW_ATTACK = 0.55f" in src
    assert "GLOW_DECAY = 0.12f" in src
    assert "SPEECH_FLOOR = 50" in src
    assert "SPEECH_SPAN = 52" in src
    assert "RMS_REF = 0.30f" in src
    assert "fill_solid(faceLeds, FACE_LEDS, color)" in src
    assert "inoise8" in idle or "candleNoise" in idle
