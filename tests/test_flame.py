"""Host checks for the idle flame maths. No serial, no LEDs, no servos."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HEADER = _REPO / "firmware" / "esp32" / "src" / "flame.h"
_MAIN = _REPO / "firmware" / "esp32" / "src" / "main.cpp"

_HARNESS = r"""
#include "flame.h"
#include <stdio.h>

static int violations = 0;

static void expect_band(int h, int cap, int lo, int hi) {
  FlameBand band = flameBand((uint8_t)h, (uint8_t)cap);
  if (band.lo != lo || band.hi != hi) violations++;
  printf("band %d %d %u %u\n", h, cap, band.lo, band.hi);
}

static void expect_level(int h, int noise, int event, int cap) {
  uint8_t level = flameLevel((uint8_t)h, (uint8_t)noise, (uint8_t)event, (uint8_t)cap);
  printf("level %d %d %d %d %u\n", h, noise, event, cap, level);
}

static void expect_color(int r, int g, int b, int level, int h, int cap) {
  FlameRgb c = flameColor((uint8_t)r, (uint8_t)g, (uint8_t)b,
                           (uint8_t)level, (uint8_t)h, (uint8_t)cap);
  printf("color %d %d %d %d %d %d %u %u %u\n", r, g, b, level, h, cap, c.r, c.g, c.b);
}

static void expect_pixel(int r, int g, int b, int level, int h, int cap) {
  FlameRgb c = flamePixel((uint8_t)r, (uint8_t)g, (uint8_t)b,
                           (uint8_t)level, (uint8_t)h, (uint8_t)cap);
  printf("pixel %d %d %d %d %d %d %u %u %u\n", r, g, b, level, h, cap, c.r, c.g, c.b);
}

static void check_equal_level() {
  int fails = 0;
  int min_delta = 255;
  for (int level = 1; level <= 255; level++) {
    FlameRgb bot = flameColor(255, 140, 20, (uint8_t)level, 0, 102);
    FlameRgb top = flameColor(255, 140, 20, (uint8_t)level, 255, 102);
    int delta = (int)bot.g - (int)top.g;
    if (delta < min_delta) min_delta = delta;
    if (delta <= 0) fails++;
    printf("equal %d %u %u\n", level, bot.g, top.g);
  }
  printf("equal_fail %d %d\n", fails, min_delta);
}

static void check_quiet_and_tails() {
  int inside = 0;
  int tail = 0;
  const int heights[] = {0, 64, 128, 192, 255};
  for (int hi = 0; hi < 5; hi++) {
    int h = heights[hi];
    FlameBand band = flameBand((uint8_t)h, 102);
    for (int noise = 0; noise < 256; noise++) {
      uint8_t mid = flameLevel((uint8_t)h, (uint8_t)noise, 128, 102);
      if (mid < band.lo || mid > band.hi) inside++;
      for (int event = 0; event < 256; event++) {
        uint8_t level = flameLevel((uint8_t)h, (uint8_t)noise, (uint8_t)event, 102);
        if (level > band.hi && event < 192) tail++;
        if (level < band.lo && event > 64) tail++;
        if (level < 1) inside++;
      }
    }
  }
  printf("inside %d\n", inside);
  printf("tail %d\n", tail);
}

static void check_rare(int h, int noise, int cap) {
  FlameBand band = flameBand((uint8_t)h, (uint8_t)cap);
  int flare = 0;
  int gutter = 0;
  int quiet = 0;
  int flare_min = 255;
  int gutter_max = 0;
  for (int event = 0; event < 256; event++) {
    uint8_t level = flameLevel((uint8_t)h, (uint8_t)noise, (uint8_t)event, (uint8_t)cap);
    if (level > band.hi) {
      flare++;
      if (event < flare_min) flare_min = event;
    } else if (level < band.lo) {
      gutter++;
      if (event > gutter_max) gutter_max = event;
    } else {
      quiet++;
    }
  }
  printf("rare %d %d %d %d %d %d %d %d\n",
         h, noise, cap, flare, gutter, quiet, flare_min, gutter_max);
}

static void check_sweep() {
  const int caps[] = {1, 30, 60, 102, 255};
  for (int ci = 0; ci < 5; ci++) {
    int cap = caps[ci];
    int mn = 255;
    int mx = 0;
    int over = 0;
    int under = 0;
    for (int h = 0; h < 256; h++) {
      for (int noise = 0; noise < 256; noise++) {
        for (int event = 0; event < 256; event++) {
          int level = flameLevel((uint8_t)h, (uint8_t)noise, (uint8_t)event, (uint8_t)cap);
          if (level < mn) mn = level;
          if (level > mx) mx = level;
          if (level < 1) under++;
          if (level > cap) over++;
        }
      }
    }
    printf("sweep %d %d %d %d %d\n", cap, mn, mx, over, under);
  }
}

static void check_pixels() {
  int black = 0;
  const int caps[] = {1, 30, 60, 102};
  const int heights[] = {0, 128, 255};
  for (int ci = 0; ci < 4; ci++) {
    for (int hi = 0; hi < 3; hi++) {
      for (int level = 1; level <= 255; level++) {
        FlameRgb px = flamePixel(255, 140, 20, (uint8_t)level, (uint8_t)heights[hi], (uint8_t)caps[ci]);
        if (px.r == 0 && px.g == 0 && px.b == 0) black++;
      }
    }
  }
  int cap0 = 0;
  for (int level = 0; level <= 255; level++) {
    FlameRgb px = flamePixel(255, 140, 20, (uint8_t)level, 128, 0);
    if (px.r != 0 || px.g != 0 || px.b != 0) cap0++;
  }
  printf("black %d\n", black);
  printf("cap0 %d\n", cap0);
}

static void check_mono() {
  int bad = 0;
  FlameBand prev = flameBand(0, 102);
  for (int h = 1; h < 256; h++) {
    FlameBand band = flameBand((uint8_t)h, 102);
    if (band.lo > prev.lo || band.hi < prev.hi) bad++;
    if (band.lo < 1 || band.hi < band.lo || band.hi > 102) bad++;
    prev = band;
  }
  printf("mono %d\n", bad);
}

int main() {
  expect_band(0, 102, 40, 90);
  expect_band(128, 102, 30, 96);
  expect_band(255, 102, 20, 102);
  expect_band(0, 60, 40, 60);
  expect_band(255, 60, 20, 60);
  expect_band(0, 30, 30, 30);
  expect_band(255, 30, 20, 30);
  expect_band(0, 1, 1, 1);
  expect_band(255, 1, 1, 1);
  expect_band(128, 0, 1, 1);

  expect_level(0, 0, 128, 102);
  expect_level(0, 255, 128, 102);
  expect_level(255, 0, 128, 102);
  expect_level(255, 255, 128, 102);
  expect_level(0, 0, 255, 102);
  expect_level(0, 255, 200, 102);
  expect_level(0, 128, 216, 102);
  expect_level(0, 255, 0, 102);
  expect_level(0, 0, 56, 102);
  expect_level(255, 255, 0, 102);
  expect_level(255, 0, 40, 102);
  expect_level(0, 255, 128, 60);
  expect_level(0, 0, 255, 60);
  expect_level(0, 255, 0, 30);
  expect_level(128, 255, 255, 1);

  expect_color(255, 140, 20, 30, 128, 102);
  expect_color(255, 140, 20, 96, 128, 102);
  expect_color(255, 140, 20, 40, 0, 102);
  expect_color(255, 140, 20, 20, 255, 102);
  expect_color(255, 140, 20, 90, 0, 102);
  expect_color(255, 140, 20, 102, 255, 102);
  expect_color(8, 80, 40, 30, 128, 102);
  expect_color(8, 80, 40, 96, 128, 102);
  expect_color(255, 255, 255, 255, 0, 102);
  expect_color(0, 0, 0, 96, 128, 102);

  expect_pixel(255, 140, 20, 1, 128, 102);
  expect_pixel(255, 140, 20, 102, 128, 102);
  expect_pixel(255, 140, 20, 40, 0, 0);
  expect_pixel(255, 140, 20, 255, 255, 60);

  check_equal_level();
  check_rare(0, 128, 102);
  check_rare(255, 128, 102);
  check_quiet_and_tails();
  check_mono();
  check_pixels();
  check_sweep();
  printf("violations %d\n", violations);
  return violations == 0 ? 0 : 1;
}
"""


def _compiler() -> str | None:
    return shutil.which("g++") or shutil.which("c++")


def _run_harness(tmp_path: Path) -> dict[str, list[list[str]]]:
    compiler = _compiler()
    if compiler is None:
        pytest.skip("no C++ compiler")
    source = tmp_path / "flame_harness.cpp"
    source.write_text(_HARNESS, encoding="utf-8")
    binary = tmp_path / "flame_harness"
    include = _HEADER.parent
    built = subprocess.run(
        [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
         "-I", str(include), str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, check=False)
    assert ran.returncode == 0, ran.stderr + ran.stdout
    grouped: dict[str, list[list[str]]] = {}
    for line in ran.stdout.splitlines():
        parts = line.split()
        assert parts, line
        grouped.setdefault(parts[0], []).append(parts[1:])
    return grouped


def _one(grouped: dict[str, list[list[str]]], key: str) -> list[str]:
    rows = grouped[key]
    assert len(rows) == 1, rows
    return rows[0]


def _row(grouped: dict[str, list[list[str]]], key: str, prefix: tuple[str, ...]) -> list[str]:
    hits = [row for row in grouped[key] if tuple(row[: len(prefix)]) == prefix]
    assert len(hits) == 1, (prefix, hits)
    return hits[0]


def test_flame_header_has_no_arduino_dependency():
    text = _HEADER.read_text(encoding="utf-8")
    includes = re.findall(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', text, re.M)
    assert includes == ["stdint.h"]
    assert "Arduino" not in text
    assert "FastLED" not in text


def test_main_idle_calls_flame_and_keeps_speech_paint():
    src = _MAIN.read_text(encoding="utf-8")
    assert '#include "flame.h"' in src
    idle = src.split("static void advanceIdle", 1)[1].split("static uint8_t levelToScale", 1)[0]
    assert "LED_HEIGHT" in idle
    assert "candleNoise(" in idle
    assert "candleEventNoise(" in idle
    assert "flameLevel(" in idle
    assert "NOISE_EVOLVE_SHIFT = 4" in src
    assert "EVENT_EVOLVE_SHIFT = 3" in src
    speech = src.split("static void advanceSpeech", 1)[1].split("// Rising noise", 1)[0]
    assert "flameLevel" not in speech
    assert "flamePixel" not in speech
    assert "LED_HEIGHT" not in speech
    assert "fill_solid(faceLeds, FACE_LEDS, color)" in src
    assert "faceColor(levelToScale(glowLevel))" in src


def test_flame_band_level_cap_and_never_off(tmp_path: Path):
    out = _run_harness(tmp_path)
    assert _one(out, "violations") == ["0"]
    assert _one(out, "mono") == ["0"]
    assert _one(out, "inside") == ["0"]
    assert _one(out, "tail") == ["0"]
    assert _one(out, "black") == ["0"]
    assert _one(out, "cap0") == ["0"]

    assert _row(out, "band", ("0", "102"))[2:] == ["40", "90"]
    assert _row(out, "band", ("128", "102"))[2:] == ["30", "96"]
    assert _row(out, "band", ("255", "102"))[2:] == ["20", "102"]
    assert _row(out, "band", ("0", "60"))[2:] == ["40", "60"]
    assert _row(out, "band", ("255", "60"))[2:] == ["20", "60"]
    assert _row(out, "band", ("0", "30"))[2:] == ["30", "30"]
    assert _row(out, "band", ("255", "30"))[2:] == ["20", "30"]
    assert _row(out, "band", ("0", "1"))[2:] == ["1", "1"]
    assert _row(out, "band", ("255", "1"))[2:] == ["1", "1"]
    assert _row(out, "band", ("128", "0"))[2:] == ["1", "1"]

    # Quiet event stays on the band ends. Bottom 40–90, top 20–102.
    assert _row(out, "level", ("0", "0", "128", "102"))[-1] == "40"
    assert _row(out, "level", ("0", "255", "128", "102"))[-1] == "90"
    assert _row(out, "level", ("255", "0", "128", "102"))[-1] == "20"
    assert _row(out, "level", ("255", "255", "128", "102"))[-1] == "102"

    flare_full = int(_row(out, "level", ("0", "0", "255", "102"))[-1])
    flare_onset = int(_row(out, "level", ("0", "255", "200", "102"))[-1])
    flare_mid = int(_row(out, "level", ("0", "128", "216", "102"))[-1])
    assert 90 < flare_onset < flare_mid <= 102
    assert flare_full == 102

    gutter_full = int(_row(out, "level", ("0", "255", "0", "102"))[-1])
    gutter_onset = int(_row(out, "level", ("0", "0", "56", "102"))[-1])
    assert 1 <= gutter_full < gutter_onset < 40
    top_gutter = int(_row(out, "level", ("255", "255", "0", "102"))[-1])
    top_gutter_full = int(_row(out, "level", ("255", "0", "40", "102"))[-1])
    assert 1 <= top_gutter < 20
    assert top_gutter_full == top_gutter

    # Lower caps are never exceeded, including when a flare asks for more.
    assert int(_row(out, "level", ("0", "255", "128", "60"))[-1]) == 60
    assert int(_row(out, "level", ("0", "0", "255", "60"))[-1]) <= 60
    assert 1 <= int(_row(out, "level", ("0", "255", "0", "30"))[-1]) <= 30
    assert int(_row(out, "level", ("128", "255", "255", "1"))[-1]) == 1

    for cap, row in ((1, _row(out, "sweep", ("1",))),
                     (30, _row(out, "sweep", ("30",))),
                     (60, _row(out, "sweep", ("60",))),
                     (102, _row(out, "sweep", ("102",))),
                     (255, _row(out, "sweep", ("255",)))):
        mn, mx, over, under = (int(part) for part in row[1:])
        assert under == 0 and over == 0
        assert 1 <= mn <= mx <= cap
        if cap >= 102:
            assert mx == 102
            assert mn < 20
        elif cap == 1:
            assert mn == mx == 1

    bottom = _row(out, "rare", ("0", "128", "102"))
    flare, gutter, quiet = (int(part) for part in bottom[3:6])
    flare_min, gutter_max = (int(part) for part in bottom[6:8])
    assert flare + gutter + quiet == 256
    assert 0 < flare < 64
    assert 0 < gutter < 64
    assert quiet > 128
    assert flare_min >= 192
    assert gutter_max <= 64

    top = _row(out, "rare", ("255", "128", "102"))
    top_gutter_n, top_quiet = int(top[4]), int(top[5])
    assert 0 < top_gutter_n < 64
    assert top_quiet > 128
    assert int(top[7]) <= 64


def test_flame_colour_tracks_level_height_and_base(tmp_path: Path):
    out = _run_harness(tmp_path)

    dim = _row(out, "color", ("255", "140", "20", "30", "128", "102"))
    bright = _row(out, "color", ("255", "140", "20", "96", "128", "102"))
    assert dim[-3:] == ["255", "70", "0"]
    assert bright[-3:] == ["255", "180", "50"]

    # Dim moves G and B down from the base; bright moves them up.
    assert int(dim[-2]) < 140 and int(dim[-1]) < 20
    assert int(bright[-2]) > 140 and int(bright[-1]) > 20

    bot_dim = _row(out, "color", ("255", "140", "20", "40", "0", "102"))
    top_dim = _row(out, "color", ("255", "140", "20", "20", "255", "102"))
    bot_bright = _row(out, "color", ("255", "140", "20", "90", "0", "102"))
    top_bright = _row(out, "color", ("255", "140", "20", "102", "255", "102"))
    assert bot_dim[-3] == "255" and top_dim[-3] == "255"
    assert int(bot_dim[-2]) < 140 and int(top_dim[-2]) < 140
    assert int(bot_dim[-1]) <= 20 and int(top_dim[-1]) <= 20
    assert int(bot_bright[-2]) > 140 and int(top_bright[-2]) > 140
    assert int(bot_bright[-1]) > 20 and int(top_bright[-1]) > 20

    fails, min_delta = (int(part) for part in _one(out, "equal_fail"))
    assert fails == 0
    assert min_delta >= 1
    for level, g_bot, g_top in out["equal"]:
        assert int(g_bot) > int(g_top), level

    alt_dim = _row(out, "color", ("8", "80", "40", "30", "128", "102"))
    alt_bright = _row(out, "color", ("8", "80", "40", "96", "128", "102"))
    assert alt_dim[-3:] == ["8", "40", "0"]
    assert alt_bright[-3:] == ["8", "102", "100"]
    assert alt_dim[-3:] != dim[-3:]
    assert alt_bright[-3:] != bright[-3:]

    clamped = _row(out, "color", ("255", "255", "255", "255", "0", "102"))
    assert clamped[-3:] == ["255", "255", "255"]
    black_base = _row(out, "color", ("0", "0", "0", "96", "128", "102"))
    assert black_base[-3:] == ["0", "0", "0"]

    # Brightness scale matches faceColor, and a lit default pixel is not black.
    assert _row(out, "pixel", ("255", "140", "20", "1", "128", "102"))[-3:] == ["1", "0", "0"]
    assert _row(out, "pixel", ("255", "140", "20", "102", "128", "102"))[-3:] == ["102", "72", "20"]
    assert _row(out, "pixel", ("255", "140", "20", "40", "0", "0"))[-3:] == ["0", "0", "0"]
    capped = _row(out, "pixel", ("255", "140", "20", "255", "255", "60"))
    assert all(int(channel) <= 60 for channel in capped[-3:])
    assert any(int(channel) > 0 for channel in capped[-3:])
