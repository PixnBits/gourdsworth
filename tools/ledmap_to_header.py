#!/usr/bin/env python3
"""Build firmware/esp32/src/led_height.h from a camera LED map.

The map is one face strip, photographed with a single LED lit at a time.
Camera y grows downward. Null hits and reflection outliers (y > 340) are
filled by linear interpolation along the strip index. Height 0 is the
bottom of the face and 255 is the top, normalized over the valid y range.

Pure stdlib. ``load``, ``fill``, ``heights``, and ``render`` are importable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXPECTED_N = 231
# Camera y above this is a reflection, not the LED on the shell.
OUTLIER_Y = 340.0
PER_LINE = 12

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "firmware" / "esp32" / "src" / "led_height.h"
DEFAULT_SOURCE = "hardware/ledmap231.json"


def _require_indices(points: list) -> None:
    for i, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"ledmap point {i} must be an object")
        index = point.get("i")
        if not isinstance(index, int) or isinstance(index, bool) or index != i:
            raise ValueError(f"ledmap indices must be 0..n-1 in order, bad at {i}")


def load(path: Path | str) -> dict:
    """Load a ledmap and require n == len(points) == 231, indices 0..n-1."""
    path = Path(path)
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("ledmap must be a JSON object")
    n = data.get("n")
    points = data.get("points")
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError("ledmap n must be an integer")
    if not isinstance(points, list):
        raise ValueError("ledmap points must be a list")
    if n != EXPECTED_N or len(points) != EXPECTED_N:
        raise ValueError(
            f"ledmap n ({n}) and points ({len(points)}) must both be {EXPECTED_N}"
        )
    _require_indices(points)
    return data


def measured_y(point: dict) -> float | None:
    """Camera y, or None when the hit is missing or a reflection outlier."""
    x = point.get("x")
    y = point.get("y")
    if x is None or y is None:
        return None
    if isinstance(y, bool) or not isinstance(y, (int, float)):
        raise ValueError(f"ledmap y at {point.get('i')} must be a number or null")
    value = float(y)
    if value > OUTLIER_Y:
        return None
    return value


def fill(points: list[dict]) -> list[float]:
    """Y for every index. Nulls and outliers are interpolated along the strip.

    Between the nearest valid neighbours, y is linear in the index. At either
    end of the strip the nearest valid y is copied.
    """
    _require_indices(points)
    ys: list[float | None] = [measured_y(point) for point in points]
    n = len(ys)
    left = [-1] * n
    last = -1
    for i, y in enumerate(ys):
        left[i] = last
        if y is not None:
            last = i
    right = [-1] * n
    last = -1
    for i in range(n - 1, -1, -1):
        right[i] = last
        if ys[i] is not None:
            last = i
    if last < 0 and not any(y is not None for y in ys):
        raise ValueError("ledmap has no valid y values")

    out: list[float] = []
    for i, y in enumerate(ys):
        if y is not None:
            out.append(y)
            continue
        lo = left[i]
        hi = right[i]
        if lo < 0 and hi < 0:
            raise ValueError("ledmap has no valid y values")
        if lo < 0:
            out.append(float(ys[hi]))
        elif hi < 0:
            out.append(float(ys[lo]))
        else:
            span = hi - lo
            t = (i - lo) / span
            y_lo = float(ys[lo])
            y_hi = float(ys[hi])
            out.append(y_lo + (y_hi - y_lo) * t)
    return out


def valid_ys(points: list[dict]) -> list[float]:
    """Measured y values that are kept (not null, not outliers)."""
    return [y for point in points if (y := measured_y(point)) is not None]


def heights(ys: list[float], *, y_min: float, y_max: float) -> list[int]:
    """Map camera y onto 0 (bottom) .. 255 (top). Half rounds away from 0.

    ``y_min`` and ``y_max`` are the valid measured range, not the filled one.
    """
    span = y_max - y_min
    if span <= 0:
        raise ValueError("valid y range must be non-empty")
    out: list[int] = []
    for y in ys:
        unit = (y_max - y) / span
        value = int(unit * 255.0 + 0.5)
        if value < 0:
            value = 0
        elif value > 255:
            value = 255
        out.append(value)
    return out


def filled_indices(points: list[dict]) -> list[int]:
    """Indices whose y was missing or an outlier."""
    return [i for i, point in enumerate(points) if measured_y(point) is None]


def _ranges(indices: list[int]) -> str:
    if not indices:
        return "none"
    parts: list[str] = []
    start = prev = indices[0]
    for i in indices[1:]:
        if i == prev + 1:
            prev = i
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = i
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(parts)


def source_label(path: Path | str) -> str:
    """Stable comment path. Prefer a repo-relative posix path."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO).as_posix()
    except ValueError:
        return Path(path).as_posix()


def render(
    values: list[int],
    filled: list[int],
    *,
    source: str = DEFAULT_SOURCE,
) -> str:
    """C++ header text. Deterministic: no timestamps, 12 values per line."""
    if len(values) != EXPECTED_N:
        raise ValueError(f"LED_HEIGHT needs {EXPECTED_N} values, got {len(values)}")
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
            raise ValueError(f"LED height {value!r} is outside 0..255")
    lines = []
    for offset in range(0, len(values), PER_LINE):
        chunk = ", ".join(str(value) for value in values[offset : offset + PER_LINE])
        lines.append(f"    {chunk},")
    body = "\n".join(lines)
    filled_text = _ranges(list(filled))
    return (
        f"// Generated by tools/ledmap_to_header.py from {source}.\n"
        "// Do not edit. 0 is the bottom of the face and 255 is the top,\n"
        "// normalized over valid camera y (y grows downward).\n"
        "// Filled indices (null measurement or camera y > 340), linear\n"
        f"// interpolation along the strip: {filled_text}.\n"
        "#ifndef LED_HEIGHT_H\n"
        "#define LED_HEIGHT_H\n"
        "\n"
        "#ifndef FACE_LEDS\n"
        "#define FACE_LEDS 231\n"
        "#endif\n"
        'static_assert(FACE_LEDS == 231, "LED_HEIGHT is generated for 231 face LEDs");\n'
        "\n"
        "static const uint8_t LED_HEIGHT[FACE_LEDS] = {\n"
        f"{body}\n"
        "};\n"
        "\n"
        'static_assert(sizeof(LED_HEIGHT) == FACE_LEDS, "LED_HEIGHT length must match FACE_LEDS");\n'
        "\n"
        "#endif\n"
    )


def build_header(path: Path | str, *, source: str | None = None) -> str:
    data = load(path)
    points = data["points"]
    ys = fill(points)
    kept = valid_ys(points)
    values = heights(ys, y_min=min(kept), y_max=max(kept))
    label = source if source is not None else source_label(path)
    return render(values, filled_indices(points), source=label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate led_height.h from a camera LED map.")
    parser.add_argument("ledmap", type=Path, help="ledmap JSON (n, points with i, x, y)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"header path (default {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)
    try:
        text = build_header(args.ledmap)
    except (OSError, ValueError) as exc:
        print(f"ledmap_to_header: {exc}", file=sys.stderr)
        return 1
    out = args.output if args.output is not None else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
