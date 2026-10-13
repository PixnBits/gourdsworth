#!/usr/bin/env python3
"""Find a face RGB that looks like the target once it has passed through the shell.

The ESP32 shows candidate colours as test swatches (exact RGB, brightness cap
not applied; the 4 A FastLED limit still is). This process grabs a BGR still,
averages each swatch ROI, and scores CIEDE2000 against the target. A coarse
grid runs first, then a small cube around the winner. ``--apply`` stores the
winner with ``set_color``, which the firmware keeps in NVS.

One ROI is one colour. Swatch groups follow LED wire order, so a left-to-right
split of that ROI measures the wrong LEDs. ``--swatches N`` opts into the split.
After each command the script waits, drains grabs that have finished past that
wait, and samples a per-pixel median. It never reads the serial port.

Camera, face link, the clock, sleep, and the v4l2 runner are arguments so a
test can drive the search without hardware.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Sequence

import numpy as np

import face_link

DEFAULT_TARGET = (255, 120, 0)
DEFAULT_GRID_R = (255, 220, 180)
DEFAULT_GRID_G = (40, 80, 120, 160, 200)
DEFAULT_GRID_B = (0, 20, 40)
DEFAULT_REFINE = (16, 8)
# One ROI shows one colour. Wire order is not left-to-right in the image,
# so the old default of 8 measured slot position instead of the swatch.
DEFAULT_SWATCHES = 1
# Manual exposure_absolute in 100 µs units. The CLI default is "auto".
DEFAULT_EXPOSURE = 1000
DEFAULT_SETTLE_S = 0.25
DEFAULT_DRAIN_FRAMES = 3
DEFAULT_MEDIAN_FRAMES = 3
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
# Frames dropped once after open, before the size is recorded.
WARMUP_FRAMES = 10
# Reads per grab. The last one is the frame after the LEDs have updated.
GRAB_READS = 5
# 0–255. At or above this, a lit ROI is real signal. Below it, the capture is black.
MIN_ROI_MEAN = 2.0
# exposure_absolute ladder used by --exposure auto. Every rung is measured.
EXPOSURE_LADDER = (250, 500, 1000, 2000)
# Highest ROI max-channel mean that is still a legal exposure pick.
EXPOSURE_MEAN_CAP = 200.0
# A channel clips when this fraction of ROI pixels is at or above CLIP_LEVEL.
CLIP_LEVEL = 250
CLIP_FRACTION_MAX = 0.02
# Off-versus-on and ramp-end rise, in summed channel means (0–765).
SANITY_RISE = 15.0
SANITY_TOLERANCE_ABS = 3.0
SANITY_TOLERANCE_FRAC = 0.05
SANITY_RAMP_SCALES = (0.0, 0.25, 0.5, 0.75, 1.0)
# One mid orange held in every slot while auto exposure is measured.
MID_SWATCH = (220, 120, 20)
# Bright fill used only to prove the camera can see the face before the search.
PROBE_RGB = (255, 180, 40)
POWER_LIMIT_MA = 4000
BLACK_FRAME_MSG = (
    "camera frames are black / ROI mean ~0; check --camera, --roi, --exposure"
)
SANITY_MSG = "frames look stale/lagging or the exposure is bad/clipped"

# sRGB (D65) linear-RGB to XYZ. Row sums are the D65 white point below.
_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)
_EPS = 216.0 / 24389.0
_KAPPA = 24389.0 / 27.0

# 5×7 glyphs, high bit on the left. Enough for "255,140,20", "dE 1.23", "P", "C", "%".
_FONT = {
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x0E, 0x11, 0x01, 0x06, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    ",": (0x00, 0x00, 0x00, 0x00, 0x06, 0x04, 0x08),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x06, 0x06),
    " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
    "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    "d": (0x01, 0x01, 0x0D, 0x13, 0x11, 0x11, 0x0F),
    "E": (0x1F, 0x10, 0x1E, 0x10, 0x10, 0x10, 0x1F),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "%": (0x19, 0x1A, 0x04, 0x04, 0x0B, 0x13, 0x13),
}

SHEET_COLS = 6
SHEET_MARGIN = 4
CELL_W = 172
CELL_H = 116
CELL_GAP = 6
SWATCH_H = 46


@dataclass(frozen=True)
class Sample:
    rgb: tuple[int, int, int]
    measured_rgb: tuple[float, float, float]
    delta_e: float
    stage: str
    peak_rgb: tuple[float, float, float] = (0.0, 0.0, 0.0)
    clip_fraction: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class ExposureRung:
    exposure: int
    max_channel_mean: float
    clip_fraction: tuple[float, float, float]


@dataclass(frozen=True)
class ExposurePick:
    chosen: int
    rungs: tuple[ExposureRung, ...] = ()
    all_clipped: bool = False


@dataclass(frozen=True)
class CalibResult:
    target_rgb: tuple[int, int, int]
    best_rgb: tuple[int, int, int]
    delta_e: float
    measured_rgb: tuple[float, float, float]
    samples: tuple[Sample, ...]
    applied: bool = False
    sanity: dict | None = None
    exposure: ExposurePick | None = None


def srgb_to_lab(rgb) -> np.ndarray:
    """sRGB 0–255 (float or int) → CIELAB under D65. Returns shape (3,)."""
    channel = np.asarray(rgb, dtype=np.float64).reshape(3) / 255.0
    linear = np.where(channel <= 0.04045, channel / 12.92, ((channel + 0.055) / 1.055) ** 2.4)
    ratio = (_RGB_TO_XYZ @ linear) / _WHITE
    curved = np.where(ratio > _EPS, np.cbrt(ratio), (_KAPPA * ratio + 16.0) / 116.0)
    return np.array(
        [
            116.0 * curved[1] - 16.0,
            500.0 * (curved[0] - curved[1]),
            200.0 * (curved[1] - curved[2]),
        ],
        dtype=np.float64,
    )


def delta_e_2000(lab1, lab2, *, k_l: float = 1.0, k_c: float = 1.0, k_h: float = 1.0) -> float:
    """CIEDE2000 colour difference. ``k_l``, ``k_c``, and ``k_h`` default to 1."""
    left = np.asarray(lab1, dtype=np.float64).reshape(3)
    right = np.asarray(lab2, dtype=np.float64).reshape(3)
    L1, a1, b1 = (float(v) for v in left)
    L2, a2, b2 = (float(v) for v in right)

    c1 = float(np.hypot(a1, b1))
    c2 = float(np.hypot(a2, b2))
    c_bar = 0.5 * (c1 + c2)
    c_bar7 = c_bar ** 7
    g = 0.5 * (1.0 - np.sqrt(c_bar7 / (c_bar7 + 25.0 ** 7)))

    a1p = (1.0 + g) * a1
    a2p = (1.0 + g) * a2
    c1p = float(np.hypot(a1p, b1))
    c2p = float(np.hypot(a2p, b2))

    def _hue(b_value: float, a_value: float) -> float:
        if a_value == 0.0 and b_value == 0.0:
            return 0.0
        return float(np.degrees(np.arctan2(b_value, a_value)) % 360.0)

    h1p = _hue(b1, a1p)
    h2p = _hue(b2, a2p)

    d_l = L2 - L1
    d_c = c2p - c1p
    if c1p == 0.0 or c2p == 0.0:
        d_h = 0.0
    else:
        d_h = h2p - h1p
        if d_h > 180.0:
            d_h -= 360.0
        elif d_h < -180.0:
            d_h += 360.0
    d_big_h = 2.0 * np.sqrt(c1p * c2p) * np.sin(np.radians(d_h) / 2.0)

    l_bar = 0.5 * (L1 + L2)
    c_bar_p = 0.5 * (c1p + c2p)
    if c1p * c2p == 0.0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        h_bar = 0.5 * (h1p + h2p)
    elif h1p + h2p < 360.0:
        h_bar = 0.5 * (h1p + h2p + 360.0)
    else:
        h_bar = 0.5 * (h1p + h2p - 360.0)

    t = (
        1.0
        - 0.17 * np.cos(np.radians(h_bar - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * h_bar))
        + 0.32 * np.cos(np.radians(3.0 * h_bar + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * h_bar - 63.0))
    )
    d_theta = 30.0 * np.exp(-(((h_bar - 275.0) / 25.0) ** 2))
    c_bar_p7 = c_bar_p ** 7
    r_c = 2.0 * np.sqrt(c_bar_p7 / (c_bar_p7 + 25.0 ** 7))
    s_l = 1.0 + (0.015 * (l_bar - 50.0) ** 2) / np.sqrt(20.0 + (l_bar - 50.0) ** 2)
    s_c = 1.0 + 0.045 * c_bar_p
    s_h = 1.0 + 0.015 * c_bar_p * t
    r_t = -np.sin(np.radians(2.0 * d_theta)) * r_c

    term_l = d_l / (k_l * s_l)
    term_c = d_c / (k_c * s_c)
    term_h = d_big_h / (k_h * s_h)
    return float(np.sqrt(term_l ** 2 + term_c ** 2 + term_h ** 2 + r_t * term_c * term_h))


def split_roi_horizontal(roi: Sequence[int], n: int) -> list[tuple[int, int, int, int]]:
    """Split one ``x,y,w,h`` ROI into ``n`` side-by-side regions.

    Width is divided with ``face_link.swatch_layout``, so the leftmost
    regions absorb the leftover pixels.
    """
    x, y, w, h = (int(v) for v in roi)
    if h < 1:
        raise ValueError("roi has no height")
    return [(x + start, y, count, h) for start, count in face_link.swatch_layout(w, n)]


def _crop_bgr(image: np.ndarray, roi: Sequence[int]) -> np.ndarray:
    """Float BGR crop of ``roi`` (x, y, w, h). Raises when the box misses the frame."""
    frame = np.asarray(image)
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError("frame must be HxWx3 BGR")
    x, y, w, h = (int(v) for v in roi)
    if w < 1 or h < 1:
        raise ValueError("roi has no area")
    height, width = frame.shape[:2]
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise ValueError(
            f"roi {(x, y, w, h)} falls outside the {width}x{height} frame; "
            "check --roi, --width, and --height"
        )
    return np.asarray(frame[y : y + h, x : x + w, :3], dtype=np.float64)


def mean_rgb(image: np.ndarray, roi: Sequence[int]) -> tuple[float, float, float]:
    """Mean sRGB of an OpenCV BGR frame inside ``roi`` (x, y, w, h)."""
    crop = _crop_bgr(image, roi)
    mean_bgr = crop.reshape(-1, 3).mean(axis=0)
    return (float(mean_bgr[2]), float(mean_bgr[1]), float(mean_bgr[0]))


def roi_measurement(image: np.ndarray, roi: Sequence[int]) -> dict:
    """Mean, per-channel peak, and clip fraction (pixels >= ``CLIP_LEVEL``) in sRGB order."""
    crop = _crop_bgr(image, roi)
    channels = (crop[:, :, 2], crop[:, :, 1], crop[:, :, 0])
    return {
        "mean": tuple(float(channel.mean()) for channel in channels),
        "peak": tuple(float(channel.max()) for channel in channels),
        "clip": tuple(float(np.mean(channel >= CLIP_LEVEL)) for channel in channels),
    }


def ensure_rois_fit(frame: np.ndarray, rois: Sequence[Sequence[int]]) -> None:
    """Raise when an ROI does not lie inside the grabbed frame.

    Coordinates are in the capture's pixel space. A box measured on a
    640×480 preview does not land on a 1920×1080 frame.
    """
    image = np.asarray(frame)
    if image.ndim < 2:
        raise ValueError("frame has no size")
    height, width = (int(image.shape[0]), int(image.shape[1]))
    for roi in rois:
        # mean_rgb raises the same out-of-frame error and checks the channels.
        mean_rgb(image, roi)


def _roi_pixel_mean(frame: np.ndarray, roi: Sequence[int]) -> float:
    red, green, blue = mean_rgb(frame, roi)
    return (red + green + blue) / 3.0


def _clock_of(clock: Callable[[], float] | None) -> Callable[[], float]:
    return time.monotonic if clock is None else clock


def _sleep_of(sleep: Callable[[float], None] | None) -> Callable[[float], None]:
    return time.sleep if sleep is None else sleep


def _as_count(value, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def capture_settled(
    camera,
    *,
    command_time: float,
    settle_s: float,
    drain_frames: int = DEFAULT_DRAIN_FRAMES,
    median_frames: int = DEFAULT_MEDIAN_FRAMES,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> np.ndarray:
    """Sleep, drop grabs that finished after the settle deadline, then take a median.

    ``command_time`` and ``clock`` are seconds from the same injectable clock.
    A grab counts toward ``drain_frames`` when ``clock()`` after it returns is
    at or after ``command_time + settle_s``. The sample is the per-pixel median
    of the next ``median_frames`` grabs. Only ``camera.grab`` is called.
    """
    now = _clock_of(clock)
    pause = _sleep_of(sleep)
    settle = float(settle_s)
    if settle < 0:
        raise ValueError("settle must be >= 0")
    drain_n = _as_count(drain_frames, "drain frames", minimum=0)
    median_n = _as_count(median_frames, "median frames", minimum=1)
    if settle > 0:
        pause(settle)
    ready = float(command_time) + settle
    drained = 0
    spins = 0
    # A frozen clock must fail instead of spinning. Real monotonic time moves.
    limit = drain_n + 10000
    while drained < drain_n:
        camera.grab()
        spins += 1
        if float(now()) >= ready:
            drained += 1
        elif spins > limit:
            raise RuntimeError(
                "settle clock did not advance; frames never finished after the command"
            )
    frames = [np.asarray(camera.grab()) for _ in range(median_n)]
    stacked = np.stack([np.asarray(frame, dtype=np.float64) for frame in frames], axis=0)
    median = np.median(stacked, axis=0)
    return np.clip(np.rint(median), 0, 255).astype(np.uint8)


def _scale_rgb(rgb: Sequence[int], scale: float) -> tuple[int, int, int]:
    scaled = []
    for channel in rgb:
        value = int(round(float(channel) * float(scale)))
        scaled.append(min(255, max(0, value)))
    return (scaled[0], scaled[1], scaled[2])


def _combine_rois(frame: np.ndarray, rois: Sequence[Sequence[int]]) -> dict:
    """Average means across ROIs. Peak and clip fraction take the hottest ROI."""
    if not rois:
        raise ValueError("need at least one ROI")
    measurements = [roi_measurement(frame, roi) for roi in rois]
    count = float(len(measurements))
    mean = tuple(
        sum(item["mean"][channel] for item in measurements) / count for channel in range(3)
    )
    peak = tuple(max(item["peak"][channel] for item in measurements) for channel in range(3))
    clip = tuple(max(item["clip"][channel] for item in measurements) for channel in range(3))
    brightness = sum(sum(item["mean"]) for item in measurements) / count
    return {
        "measured_rgb": mean,
        "peak_rgb": peak,
        "clip_fraction": clip,
        "brightness": float(brightness),
    }


def _exposure_stats(frame: np.ndarray, rois: Sequence[Sequence[int]]) -> tuple[float, tuple[float, float, float]]:
    measurements = [roi_measurement(frame, roi) for roi in rois]
    max_mean = max(max(item["mean"]) for item in measurements)
    clip = tuple(max(item["clip"][channel] for item in measurements) for channel in range(3))
    return float(max_mean), clip


def _clipped(clip: Sequence[float]) -> bool:
    return any(float(channel) > CLIP_FRACTION_MAX for channel in clip)


def _ramp_tolerance(previous: float) -> float:
    return max(SANITY_TOLERANCE_ABS, SANITY_TOLERANCE_FRAC * abs(float(previous)))


def _colour_list(triple) -> list[float]:
    return [round(float(channel), 4) for channel in triple]


def _reading_payload(rgb: Sequence[int], measurement: dict, *, scale: float | None = None) -> dict:
    payload = {
        "rgb": [int(channel) for channel in rgb],
        "measured_rgb": _colour_list(measurement["measured_rgb"]),
        "peak_rgb": _colour_list(measurement["peak_rgb"]),
        "clip_fraction": [round(float(channel), 6) for channel in measurement["clip_fraction"]],
        "brightness": round(float(measurement["brightness"]), 4),
    }
    if scale is not None:
        payload["scale"] = float(scale)
    return payload


def check_signal(frame: np.ndarray, rois: Sequence[Sequence[int]]) -> None:
    """Raise when the camera delivered black or an ROI missed the glow.

    A 140×30 swatch on a 640×480 frame averages under ``MIN_ROI_MEAN`` even
    when the LED is bright, so a dark surround is not a black frame. The
    frame is black when its mean and its brightest pixel are both under the
    floor. Each ROI is black when its pixel mean is under the floor.
    """
    image = np.asarray(frame)
    if image.size == 0 or image.ndim < 2:
        raise RuntimeError(BLACK_FRAME_MSG)
    frame_mean = float(image.mean())
    frame_peak = float(image.max())
    frame_black = frame_mean < MIN_ROI_MEAN and frame_peak < MIN_ROI_MEAN
    roi_mean = min(_roi_pixel_mean(image, roi) for roi in rois)
    if frame_black or roi_mean < MIN_ROI_MEAN:
        raise RuntimeError(
            f"{BLACK_FRAME_MSG} (frame mean {frame_mean:.2f}, roi mean {roi_mean:.2f})"
        )


def samples_lack_signal(samples: Sequence[Sample]) -> bool:
    """True when every swatch's brightest channel mean is under the floor."""
    if not samples:
        return True
    return all(max(sample.measured_rgb) < MIN_ROI_MEAN for sample in samples)


def candidate_grid(
    reds: Sequence[int] = DEFAULT_GRID_R,
    greens: Sequence[int] = DEFAULT_GRID_G,
    blues: Sequence[int] = DEFAULT_GRID_B,
) -> list[tuple[int, int, int]]:
    """Nested RGB product, blue changing fastest. Duplicates are dropped."""
    found: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    for red in reds:
        for green in greens:
            for blue in blues:
                colour = (face_link.as_u8(red), face_link.as_u8(green), face_link.as_u8(blue))
                if colour not in seen:
                    seen.add(colour)
                    found.append(colour)
    if not found:
        raise ValueError("candidate grid is empty")
    return found


def refine_around(rgb: Sequence[int], step: int) -> list[tuple[int, int, int]]:
    """26 neighbours at ±step on each channel, clamped to 0..255. Centre omitted."""
    step_i = int(step)
    if step_i < 1:
        return []
    red, green, blue = (int(channel) for channel in rgb)
    centre = (red, green, blue)
    found: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    for d_red in (-step_i, 0, step_i):
        for d_green in (-step_i, 0, step_i):
            for d_blue in (-step_i, 0, step_i):
                if d_red == d_green == d_blue == 0:
                    continue
                colour = (
                    min(255, max(0, red + d_red)),
                    min(255, max(0, green + d_green)),
                    min(255, max(0, blue + d_blue)),
                )
                # Clamping at 0 or 255 can fold an offset back onto the centre.
                if colour == centre or colour in seen:
                    continue
                seen.add(colour)
                found.append(colour)
    return found


def _chunks(items: Sequence[tuple[int, int, int]], size: int):
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _show(
    link,
    colours: Sequence[Sequence[int]],
    camera,
    *,
    settle_s: float,
    drain_frames: int,
    median_frames: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> np.ndarray:
    """Paint ``colours`` and return the settled median frame. Never reads serial."""
    command_time = float(clock())
    if not link.test_swatches(colours):
        raise RuntimeError("face link did not accept swatches")
    return capture_settled(
        camera,
        command_time=command_time,
        settle_s=settle_s,
        drain_frames=drain_frames,
        median_frames=median_frames,
        clock=clock,
        sleep=sleep,
    )


def _evaluate_frame(
    link,
    camera,
    batch: Sequence[tuple[int, int, int]],
    rois: Sequence[tuple[int, int, int, int]],
    stage: str,
    target_lab: np.ndarray,
    scored: dict[tuple[int, int, int], Sample],
    order: list[tuple[int, int, int]],
    *,
    real_n: int,
    settle_s: float,
    drain_frames: int,
    median_frames: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    if not batch:
        return
    # The ESP32 answers none of these lines. The write is fire-and-forget.
    frame = _show(
        link,
        batch,
        camera,
        settle_s=settle_s,
        drain_frames=drain_frames,
        median_frames=median_frames,
        clock=clock,
        sleep=sleep,
    )
    ensure_rois_fit(frame, rois)
    if len(rois) == 1:
        regions = split_roi_horizontal(rois[0], len(batch))
    elif len(rois) == len(batch):
        regions = list(rois)
    else:
        raise ValueError(f"{len(rois)} ROIs for a frame of {len(batch)} swatches")
    for colour, roi in zip(list(batch)[:real_n], regions[:real_n]):
        key = (int(colour[0]), int(colour[1]), int(colour[2]))
        if key in scored:
            continue
        stats = roi_measurement(frame, roi)
        measured = stats["mean"]
        delta = delta_e_2000(srgb_to_lab(measured), target_lab)
        scored[key] = Sample(
            rgb=key,
            measured_rgb=measured,
            delta_e=delta,
            stage=stage,
            peak_rgb=stats["peak"],
            clip_fraction=stats["clip"],
        )
        order.append(key)


def _sanity_sweep(
    link,
    camera,
    rois: Sequence[tuple[int, int, int, int]],
    target: tuple[int, int, int],
    frame_n: int,
    *,
    settle_s: float,
    drain_frames: int,
    median_frames: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict:
    """Off versus the target, then a ramp. Brightness is the sum of channel means.

    Green through the orange shell sits near 1, so a green check false-fails a
    lit face. A failed sweep means the frames are stale or the exposure is bad.
    """

    def measure(colour: tuple[int, int, int]) -> dict:
        frame = _show(
            link,
            (colour,) * frame_n,
            camera,
            settle_s=settle_s,
            drain_frames=drain_frames,
            median_frames=median_frames,
            clock=clock,
            sleep=sleep,
        )
        ensure_rois_fit(frame, rois)
        return _combine_rois(frame, rois)

    off_colour = (0, 0, 0)
    off = measure(off_colour)
    on = measure(target)
    rise = float(on["brightness"]) - float(off["brightness"])
    if rise < SANITY_RISE:
        raise RuntimeError(
            f"{SANITY_MSG} (off brightness {off['brightness']:.2f}, "
            f"on brightness {on['brightness']:.2f}, rise {rise:.2f})"
        )
    ramp_steps = []
    for scale in SANITY_RAMP_SCALES:
        colour = _scale_rgb(target, scale)
        ramp_steps.append((scale, colour, measure(colour)))
    brightnesses = [float(step[2]["brightness"]) for step in ramp_steps]
    for previous, current in zip(brightnesses, brightnesses[1:]):
        tolerance = _ramp_tolerance(previous)
        if current < previous - tolerance:
            rendered = ", ".join(f"{value:.2f}" for value in brightnesses)
            raise RuntimeError(
                f"{SANITY_MSG} (ramp brightness {rendered} is not non-decreasing "
                f"within {tolerance:.2f})"
            )
    span = brightnesses[-1] - brightnesses[0]
    if span < SANITY_RISE:
        raise RuntimeError(
            f"{SANITY_MSG} (ramp rose {span:.2f}, need at least {SANITY_RISE:.0f})"
        )
    return {
        "threshold": SANITY_RISE,
        "tolerance_abs": SANITY_TOLERANCE_ABS,
        "tolerance_fraction": SANITY_TOLERANCE_FRAC,
        "off": _reading_payload(off_colour, off),
        "on": _reading_payload(target, on),
        "rise": round(rise, 4),
        "ramp": [
            _reading_payload(colour, measurement, scale=scale)
            for scale, colour, measurement in ramp_steps
        ],
    }


def _best(scored: dict[tuple[int, int, int], Sample], order: Sequence[tuple[int, int, int]]):
    rank = {key: index for index, key in enumerate(order)}
    return min(scored, key=lambda key: (scored[key].delta_e, rank[key]))


def calibrate(
    *,
    link,
    camera,
    rois: Sequence[Sequence[int]],
    target_rgb: Sequence[int] = DEFAULT_TARGET,
    candidates: Sequence[Sequence[int]] | None = None,
    swatches: int = DEFAULT_SWATCHES,
    refine_steps: Sequence[int] = DEFAULT_REFINE,
    settle_s: float = DEFAULT_SETTLE_S,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    drain_frames: int = DEFAULT_DRAIN_FRAMES,
    median_frames: int = DEFAULT_MEDIAN_FRAMES,
    skip_sanity: bool = False,
) -> CalibResult:
    """Show candidates, score them, refine, then leave test mode.

    One ROI is one swatch unless ``swatches`` says otherwise. More than one
    ROI fixes the swatch count at that length; a short final frame repeats
    its last colour in the unused slots and does not score the repeats.
    Samples are medians of grabs that finished after the settle wait.
    """
    pause = _sleep_of(sleep)
    now = _clock_of(clock)
    drain_n = _as_count(drain_frames, "drain frames", minimum=0)
    median_n = _as_count(median_frames, "median frames", minimum=1)
    settle = float(settle_s)
    if settle < 0:
        raise ValueError("settle must be >= 0")
    regions = [tuple(int(v) for v in roi) for roi in rois]
    if not regions:
        raise ValueError("need at least one ROI")
    for roi in regions:
        if len(roi) != 4 or roi[2] < 1 or roi[3] < 1 or roi[0] < 0 or roi[1] < 0:
            raise ValueError(f"bad roi {roi}")
    channels = tuple(target_rgb)
    if len(channels) != 3:
        raise ValueError("target needs three channels")
    target = (face_link.as_u8(channels[0]), face_link.as_u8(channels[1]), face_link.as_u8(channels[2]))
    pool = candidate_grid() if candidates is None else []
    if candidates is not None:
        seen: set[tuple[int, int, int]] = set()
        for colour in candidates:
            if len(tuple(colour)) != 3:
                raise ValueError("each candidate needs three channels")
            key = (face_link.as_u8(colour[0]), face_link.as_u8(colour[1]), face_link.as_u8(colour[2]))
            if key not in seen:
                seen.add(key)
                pool.append(key)
    if not pool:
        raise ValueError("no candidate colours")
    frame_n = len(regions) if len(regions) > 1 else int(swatches)
    if frame_n < 1 or frame_n > face_link.MAX_SWATCHES:
        raise ValueError(f"swatch count must be 1..{face_link.MAX_SWATCHES}")
    if len(regions) == 1 and regions[0][2] < min(frame_n, len(pool)):
        raise ValueError("ROI is narrower than the number of swatches in a frame")

    target_lab = srgb_to_lab(target)
    scored: dict[tuple[int, int, int], Sample] = {}
    order: list[tuple[int, int, int]] = []

    timing = dict(
        settle_s=settle,
        drain_frames=drain_n,
        median_frames=median_n,
        clock=now,
        sleep=pause,
    )

    def run_pass(colours: Sequence[tuple[int, int, int]], stage: str) -> None:
        if len(regions) == 1:
            pending = [colour for colour in colours if colour not in scored]
            for chunk in _chunks(pending, frame_n):
                _evaluate_frame(
                    link, camera, chunk, regions, stage, target_lab, scored, order,
                    real_n=len(chunk), **timing,
                )
            return
        for chunk in _chunks(list(colours), frame_n):
            real = len(chunk)
            shown = chunk
            if real < frame_n:
                shown = chunk + [chunk[-1]] * (frame_n - real)
            _evaluate_frame(
                link, camera, shown, regions, stage, target_lab, scored, order,
                real_n=real, **timing,
            )

    try:
        # Light every slot, then look. A black grab here is the camera or the
        # ROI, and the coarse grid would only record zeros.
        probe = _show(link, (PROBE_RGB,) * frame_n, camera, **timing)
        ensure_rois_fit(probe, regions)
        check_signal(probe, regions)
        sanity = None
        if not skip_sanity:
            sanity = _sanity_sweep(
                link, camera, regions, target, frame_n, **timing,
            )

        run_pass(pool, "coarse")
        if not scored:
            raise RuntimeError("calibration scored no swatches")
        best = _best(scored, order)
        for step in refine_steps:
            step_i = int(step)
            if step_i < 1:
                continue
            run_pass(refine_around(best, step_i), "refine")
            best = _best(scored, order)
        winner = scored[best]
        samples = tuple(scored[key] for key in order)
        if samples_lack_signal(samples):
            raise RuntimeError(BLACK_FRAME_MSG)
        return CalibResult(
            target_rgb=target,
            best_rgb=winner.rgb,
            delta_e=winner.delta_e,
            measured_rgb=winner.measured_rgb,
            samples=samples,
            sanity=sanity,
        )
    finally:
        try:
            if not link.test_exit():
                print("  test_exit was not written", file=sys.stderr)
        except Exception:
            print("  test_exit failed", file=sys.stderr)


def lock_camera_controls(device: str, exposure: int, run) -> list[str]:
    """Lock white balance and exposure. Try the old and new control names.

    A missing control is logged and skipped. Returns the names that were set.
    ``exposure`` is in v4l2 absolute units (100 µs). Manual exposure is 1.
    """
    device = (device or "").strip()
    if not device:
        raise ValueError("v4l2 device path is empty")
    exposure_i = int(exposure)
    if isinstance(exposure, bool) or exposure_i < 1:
        raise ValueError("exposure must be a positive integer")
    controls = (
        ("white_balance_automatic", "0"),
        ("white_balance_temperature_auto", "0"),
        ("white_balance_temperature", "4000"),
        ("auto_exposure", "1"),
        ("exposure_auto", "1"),
        ("exposure_time_absolute", str(exposure_i)),
        ("exposure_absolute", str(exposure_i)),
    )
    applied: list[str] = []
    for name, value in controls:
        command = ["v4l2-ctl", "-d", device, f"--set-ctrl={name}={value}"]
        try:
            result = run(command, capture_output=True, text=True, check=False)
        except Exception as exc:
            print(f"  v4l2 {name}={value} failed ({exc})", file=sys.stderr)
            continue
        code = getattr(result, "returncode", 0)
        if code not in (0, None):
            err = (getattr(result, "stderr", "") or "").strip()
            detail = f": {err}" if err else ""
            print(f"  v4l2 {name}={value} failed ({code}){detail}", file=sys.stderr)
            continue
        applied.append(name)
        print(f"  v4l2 {name}={value}", file=sys.stderr)
    return applied


def _swatch_count(rois: Sequence[Sequence[int]], swatches: int) -> int:
    count = len(rois) if len(rois) > 1 else int(swatches)
    if count < 1:
        count = 1
    if count > face_link.MAX_SWATCHES:
        count = face_link.MAX_SWATCHES
    return count


def _choose_exposure(rungs: Sequence[ExposureRung]) -> tuple[int, bool]:
    """Highest legal mean wins. Every clipped rung falls back to the lowest."""
    if not rungs:
        raise ValueError("exposure ladder is empty")
    eligible = [
        rung
        for rung in rungs
        if not _clipped(rung.clip_fraction) and rung.max_channel_mean <= EXPOSURE_MEAN_CAP
    ]
    if eligible:
        best = max(eligible, key=lambda rung: (rung.max_channel_mean, -rung.exposure))
        return best.exposure, False
    if all(_clipped(rung.clip_fraction) for rung in rungs):
        lowest = min(rungs, key=lambda rung: rung.exposure)
        return lowest.exposure, True
    # Unclipped but hotter than the cap: keep the dimmest of those.
    open_rungs = [rung for rung in rungs if not _clipped(rung.clip_fraction)]
    best = min(open_rungs, key=lambda rung: (rung.max_channel_mean, rung.exposure))
    return best.exposure, False


def pick_exposure(
    device: str,
    camera,
    link,
    rois: Sequence[Sequence[int]],
    run,
    *,
    swatches: int = DEFAULT_SWATCHES,
    settle_s: float = DEFAULT_SETTLE_S,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    drain_frames: int = DEFAULT_DRAIN_FRAMES,
    median_frames: int = DEFAULT_MEDIAN_FRAMES,
    ladder: Sequence[int] = EXPOSURE_LADDER,
) -> ExposurePick:
    """Measure every exposure rung and keep the brightest one that is not clipped.

    A rung whose clip fraction is over ``CLIP_FRACTION_MAX`` on any channel
    is out. Among the rest, the highest ROI max-channel mean that is still
    at most ``EXPOSURE_MEAN_CAP`` wins. If every rung is clipped, the lowest
    exposure is kept and a warning is printed. Each rung uses the same settle
    and drain as a swatch sample. The ESP32 sends no reply.
    """
    pause = _sleep_of(sleep)
    now = _clock_of(clock)
    steps = tuple(int(step) for step in ladder)
    if not steps or any(isinstance(step, bool) or step < 1 for step in steps):
        raise ValueError("exposure ladder is empty")
    regions = [tuple(int(v) for v in roi) for roi in rois]
    if not regions:
        raise ValueError("need at least one ROI")
    settle = float(settle_s)
    if settle < 0:
        raise ValueError("settle must be >= 0")
    drain_n = _as_count(drain_frames, "drain frames", minimum=0)
    median_n = _as_count(median_frames, "median frames", minimum=1)
    count = _swatch_count(regions, swatches)
    try:
        if not link.test_swatches((MID_SWATCH,) * count):
            raise RuntimeError("face link did not accept swatches")
        rungs: list[ExposureRung] = []
        applied: int | None = None
        for step in steps:
            lock_camera_controls(device, step, run)
            applied = step
            command_time = float(now())
            frame = capture_settled(
                camera,
                command_time=command_time,
                settle_s=settle,
                drain_frames=drain_n,
                median_frames=median_n,
                clock=now,
                sleep=pause,
            )
            ensure_rois_fit(frame, regions)
            level, clip = _exposure_stats(frame, regions)
            rungs.append(ExposureRung(exposure=step, max_channel_mean=level, clip_fraction=clip))
        chosen, all_clipped = _choose_exposure(rungs)
        if all_clipped:
            print(
                f"exposure ladder is clipped on every rung; using {chosen}",
                file=sys.stderr,
            )
        if chosen != applied:
            lock_camera_controls(device, chosen, run)
        return ExposurePick(chosen=chosen, rungs=tuple(rungs), all_clipped=all_clipped)
    finally:
        try:
            if not link.test_exit():
                print("  test_exit was not written", file=sys.stderr)
        except Exception:
            print("  test_exit failed", file=sys.stderr)


class CvCamera:
    """One OpenCV capture at 640×480, warmed up, with a flushed grab.

    The BRIO at 1920×1080 on the Pi returns black or stale frames, and the
    ROI was measured on a 640×480 picture. ``frame_size`` is the real
    ``(width, height)`` after the warm-up reads.
    """

    def __init__(
        self,
        index: int = 0,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ) -> None:
        width_i = int(width)
        height_i = int(height)
        if isinstance(width, bool) or isinstance(height, bool) or width_i < 1 or height_i < 1:
            raise ValueError("width and height must be positive")
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV is not installed") from exc
        self._cap = cv2.VideoCapture(int(index))
        if not self._cap.isOpened():
            self._cap.release()
            self._cap = None
            raise RuntimeError(f"camera {index} could not be opened")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width_i)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height_i)
        try:
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        seen = None
        for _ in range(WARMUP_FRAMES):
            ok, frame = self._cap.read()
            if ok and frame is not None:
                seen = frame
        self.frame_size = _frame_size(seen, self._cap, cv2, width_i, height_i)

    def grab(self) -> np.ndarray:
        if self._cap is None:
            raise RuntimeError("camera is closed")
        ok = False
        frame = None
        for _ in range(GRAB_READS):
            ok, frame = self._cap.read()
        if not ok or frame is None:
            raise RuntimeError("camera produced no frame")
        self.frame_size = (int(frame.shape[1]), int(frame.shape[0]))
        return frame

    def close(self) -> None:
        cap = self._cap
        self._cap = None
        if cap is not None:
            cap.release()


def _frame_size(frame, cap, cv2, width: int, height: int) -> tuple[int, int]:
    if frame is not None and getattr(frame, "ndim", 0) >= 2:
        return (int(frame.shape[1]), int(frame.shape[0]))
    try:
        got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    except Exception:
        got_w, got_h = 0, 0
    return (got_w or width, got_h or height)


def _blit(image: np.ndarray, x: int, y: int, text: str, colour: tuple[int, int, int], scale: int = 2) -> None:
    height, width = image.shape[:2]
    cursor = x
    for char in text:
        glyph = _FONT.get(char)
        if glyph is None:
            cursor += 6 * scale
            continue
        for row, bits in enumerate(glyph):
            for col in range(5):
                if not bits & (1 << (4 - col)):
                    continue
                x0 = cursor + col * scale
                y0 = y + row * scale
                if x0 >= width or y0 >= height:
                    continue
                image[y0 : min(height, y0 + scale), x0 : min(width, x0 + scale)] = colour
        cursor += 6 * scale


def cell_origin(index: int, columns: int = SHEET_COLS) -> tuple[int, int]:
    column = index % columns
    row = index // columns
    return (
        SHEET_MARGIN + column * (CELL_W + CELL_GAP),
        SHEET_MARGIN + row * (CELL_H + CELL_GAP),
    )


def render_contact_sheet(samples: Sequence[Sample], best_rgb: Sequence[int]) -> np.ndarray:
    """RGB image. Each cell is the measured colour, labelled with the commanded RGB."""
    count = max(1, len(samples))
    columns = SHEET_COLS
    rows = (count + columns - 1) // columns
    width = SHEET_MARGIN * 2 + columns * CELL_W + (columns - 1) * CELL_GAP
    height = SHEET_MARGIN * 2 + rows * CELL_H + (rows - 1) * CELL_GAP
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = (24, 18, 12)
    winner = tuple(int(channel) for channel in best_rgb)
    ink = (240, 230, 210)
    for index, sample in enumerate(samples):
        x, y = cell_origin(index, columns)
        red, green, blue = (int(round(channel)) for channel in sample.measured_rgb)
        red = min(255, max(0, red))
        green = min(255, max(0, green))
        blue = min(255, max(0, blue))
        image[y : y + SWATCH_H, x : x + CELL_W] = (red, green, blue)
        if sample.rgb == winner:
            image[y : y + 2, x : x + CELL_W] = (255, 255, 255)
            image[y + SWATCH_H - 2 : y + SWATCH_H, x : x + CELL_W] = (255, 255, 255)
            image[y : y + SWATCH_H, x : x + 2] = (255, 255, 255)
            image[y : y + SWATCH_H, x + CELL_W - 2 : x + CELL_W] = (255, 255, 255)
        commanded = f"{sample.rgb[0]},{sample.rgb[1]},{sample.rgb[2]}"
        peak = tuple(int(round(channel)) for channel in sample.peak_rgb)
        clip_pct = 100.0 * max(float(channel) for channel in sample.clip_fraction)
        _blit(image, x + 4, y + SWATCH_H + 2, commanded, ink)
        _blit(image, x + 4, y + SWATCH_H + 18, f"dE {sample.delta_e:.2f}", ink)
        _blit(image, x + 4, y + SWATCH_H + 34, f"P {peak[0]},{peak[1]},{peak[2]}", ink)
        _blit(image, x + 4, y + SWATCH_H + 50, f"C {clip_pct:.1f}%", ink)
    return image


def write_png(path: Path, image: np.ndarray) -> None:
    """Write an 8-bit RGB PNG. No extra dependency."""
    rgb = np.asarray(image, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("png image must be HxWx3")
    height, width = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return len(data).to_bytes(4, "big") + tag + data + crc.to_bytes(4, "big")

    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _exposure_payload(pick: ExposurePick | None) -> dict | None:
    if pick is None:
        return None
    return {
        "chosen": int(pick.chosen),
        "all_clipped": bool(pick.all_clipped),
        "rungs": [
            {
                "exposure": int(rung.exposure),
                "max_channel_mean": round(float(rung.max_channel_mean), 4),
                "clip_fraction": [round(float(channel), 6) for channel in rung.clip_fraction],
                "clipped": _clipped(rung.clip_fraction),
            }
            for rung in pick.rungs
        ],
    }


def result_payload(result: CalibResult) -> dict:
    def colour(triple) -> list[float]:
        return [round(float(channel), 4) for channel in triple]

    payload = {
        "target_rgb": list(result.target_rgb),
        "best_rgb": list(result.best_rgb),
        "delta_e": round(float(result.delta_e), 4),
        "measured_rgb": colour(result.measured_rgb),
        "applied": bool(result.applied),
        "test_mode_bypasses_brightness_cap": True,
        "power_limit_milliamps": POWER_LIMIT_MA,
        "samples": [
            {
                "rgb": list(sample.rgb),
                "measured_rgb": colour(sample.measured_rgb),
                "peak_rgb": colour(sample.peak_rgb),
                "clip_fraction": [round(float(channel), 6) for channel in sample.clip_fraction],
                "delta_e": round(float(sample.delta_e), 4),
                "stage": sample.stage,
            }
            for sample in result.samples
        ],
    }
    if result.sanity is not None:
        payload["sanity"] = result.sanity
    exposure = _exposure_payload(result.exposure)
    if exposure is not None:
        payload["exposure"] = exposure
    return payload


def save_result(directory: Path, result: CalibResult, *, clock=None) -> tuple[Path, Path]:
    moment = time.localtime() if clock is None else clock()
    stamp = time.strftime("%Y%m%d-%H%M%S", moment)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"face-cal-{stamp}.json"
    image_path = directory / f"face-cal-{stamp}.png"
    payload = result_payload(result)
    payload["contact_sheet"] = image_path.name
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    write_png(image_path, render_contact_sheet(result.samples, result.best_rgb))
    return json_path, image_path


def _parse_u8_csv(text: str, label: str) -> tuple[int, ...]:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise ValueError(f"{label} is empty")
    return tuple(face_link.as_u8(int(part)) for part in parts)


def _parse_roi(text: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise ValueError(f"roi must be x,y,w,h, got {text!r}")
    x, y, w, h = (int(part) for part in parts)
    if x < 0 or y < 0 or w < 1 or h < 1:
        raise ValueError(f"roi must be a positive area, got {text!r}")
    return x, y, w, h


def _parse_exposure(text: str) -> int | None:
    """``None`` means auto. An integer is a manual exposure_absolute value."""
    raw = str(text).strip().lower()
    if raw == "auto":
        return None
    if not raw.isdigit():
        raise ValueError(f"exposure must be a positive integer or 'auto', got {text!r}")
    value = int(raw)
    if value < 1:
        raise ValueError(f"exposure must be a positive integer or 'auto', got {text!r}")
    return value


def _parse_refine(text: str) -> tuple[int, ...]:
    stripped = text.strip().lower()
    if stripped in ("", "none", "0"):
        return ()
    steps = []
    for part in stripped.split(","):
        part = part.strip()
        if not part:
            continue
        step = int(part)
        if step < 1:
            raise ValueError(f"refine step must be >= 1, got {part}")
        steps.append(step)
    return tuple(steps)


def main(
    argv: list[str] | None = None,
    *,
    link=None,
    camera=None,
    run=None,
    clock=None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Calibrate the face colour through the shell")
    parser.add_argument("--port", default="", help="UART the ESP32 face is on, for example /dev/serial0")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--device", default="/dev/video0", help="v4l2 device for the exposure lock")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="capture width, default 640")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="capture height, default 480")
    parser.add_argument("--roi", action="append", required=True, help="x,y,w,h. Repeat once per swatch, or pass one ROI to split")
    parser.add_argument(
        "--swatches",
        type=int,
        default=DEFAULT_SWATCHES,
        help="with one ROI, left-to-right swatch count (default 1; groups follow LED wire order)",
    )
    parser.add_argument("--target", default="255,120,0", help="sRGB the camera should see")
    parser.add_argument(
        "--exposure",
        default="auto",
        help="auto (default), or manual exposure_absolute in 100 µs units",
    )
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_S, help="seconds to wait after each swatch command")
    parser.add_argument(
        "--drain-frames",
        type=int,
        default=DEFAULT_DRAIN_FRAMES,
        help="grabs to drop after settle once they finish past the command time",
    )
    parser.add_argument(
        "--median-frames",
        type=int,
        default=DEFAULT_MEDIAN_FRAMES,
        help="grabs combined as a per-pixel median for each sample",
    )
    parser.add_argument("--apply", action="store_true", help="store the winner on the ESP32")
    parser.add_argument("--skip-sanity", action="store_true", help="skip the off/on and ramp brightness sweep")
    parser.add_argument("--skip-v4l2", action="store_true", help="do not try to lock exposure and white balance")
    parser.add_argument("--logs", default="logs", help="directory for the JSON result and contact sheet")
    parser.add_argument("--grid-r", default="", help="comma-separated red candidates")
    parser.add_argument("--grid-g", default="", help="comma-separated green candidates")
    parser.add_argument("--grid-b", default="", help="comma-separated blue candidates")
    parser.add_argument("--refine", default="16,8", help="channel steps around the winner, or 'none'")
    args = parser.parse_args(argv)

    owned_link = False
    owned_camera = False
    try:
        rois = [_parse_roi(item) for item in args.roi]
        target = _parse_u8_csv(args.target, "target")
        if len(target) != 3:
            raise ValueError("target must be r,g,b")
        grids = (args.grid_r.strip(), args.grid_g.strip(), args.grid_b.strip())
        if any(grids) and not all(grids):
            raise ValueError("pass grid-r, grid-g, and grid-b together")
        if all(grids):
            candidates = candidate_grid(
                _parse_u8_csv(args.grid_r, "grid-r"),
                _parse_u8_csv(args.grid_g, "grid-g"),
                _parse_u8_csv(args.grid_b, "grid-b"),
            )
        else:
            candidates = candidate_grid()
        steps = _parse_refine(args.refine)
        exposure = _parse_exposure(args.exposure)
        if args.width < 1 or args.height < 1:
            raise ValueError("width and height must be positive")
        _as_count(args.drain_frames, "drain frames", minimum=0)
        _as_count(args.median_frames, "median frames", minimum=1)
        if args.settle < 0:
            raise ValueError("settle must be >= 0")
        if args.swatches < 1 or args.swatches > face_link.MAX_SWATCHES:
            raise ValueError(f"swatch count must be 1..{face_link.MAX_SWATCHES}")
        runner = subprocess.run if run is None else run
        timing_clock = _clock_of(now)

        # A fixed exposure is locked before the stream starts. Auto has to
        # see frames, so the camera opens first and the ladder runs below.
        exposure_pick = None
        if not args.skip_v4l2 and exposure is not None:
            lock_camera_controls(args.device, exposure, runner)
            exposure_pick = ExposurePick(chosen=int(exposure))

        if camera is None:
            camera = CvCamera(args.camera, width=args.width, height=args.height)
            owned_camera = True
        frame_size = getattr(camera, "frame_size", None)
        if frame_size is not None:
            frame_w, frame_h = (int(frame_size[0]), int(frame_size[1]))
            blank = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)
            ensure_rois_fit(blank, rois)

        if link is None:
            link = face_link.open_face(args.port)
            owned_link = True
            if link is None:
                print("face UART did not open", file=sys.stderr)
                return 2

        if not args.skip_v4l2 and exposure is None:
            exposure_pick = pick_exposure(
                args.device,
                camera,
                link,
                rois,
                runner,
                swatches=args.swatches,
                settle_s=float(args.settle),
                sleep=sleep,
                clock=timing_clock,
                drain_frames=args.drain_frames,
                median_frames=args.median_frames,
            )

        result = calibrate(
            link=link,
            camera=camera,
            rois=rois,
            target_rgb=target,
            candidates=candidates,
            swatches=args.swatches,
            refine_steps=steps,
            settle_s=float(args.settle),
            sleep=sleep,
            clock=timing_clock,
            drain_frames=args.drain_frames,
            median_frames=args.median_frames,
            skip_sanity=bool(args.skip_sanity),
        )
        applied = False
        if args.apply:
            if not link.set_color(*result.best_rgb):
                print("face link did not store the colour", file=sys.stderr)
                return 1
            applied = True
        result = CalibResult(
            target_rgb=result.target_rgb,
            best_rgb=result.best_rgb,
            delta_e=result.delta_e,
            measured_rgb=result.measured_rgb,
            samples=result.samples,
            applied=applied,
            sanity=result.sanity,
            exposure=exposure_pick,
        )
        json_path, image_path = save_result(Path(args.logs), result, clock=clock)
        red, green, blue = result.best_rgb
        print(f"best_rgb {red},{green},{blue} delta_e {result.delta_e:.4f}")
        print(f"json {json_path}")
        print(f"sheet {image_path}")
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if owned_camera and camera is not None:
            camera.close()
        if owned_link and link is not None:
            link.close()


if __name__ == "__main__":
    raise SystemExit(main())
