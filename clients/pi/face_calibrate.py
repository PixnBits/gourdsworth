#!/usr/bin/env python3
"""Find a face RGB that looks like the target once it has passed through the shell.

The ESP32 shows candidate colours as test swatches (exact RGB, brightness cap
not applied; the 4 A FastLED limit still is). This process grabs a BGR still,
averages each swatch ROI, and scores CIEDE2000 against the target. A coarse
grid runs first, then a small cube around the winner. ``--apply`` stores the
winner with ``set_color``, which the firmware keeps in NVS.

Camera, face link, and the v4l2 runner are arguments so a test can drive the
search without hardware.
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
DEFAULT_SWATCHES = 8
# Manual exposure_absolute in 100 µs units. The CLI default is "auto".
DEFAULT_EXPOSURE = 1000
DEFAULT_SETTLE_S = 0.2
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
# Frames dropped once after open, before the size is recorded.
WARMUP_FRAMES = 10
# Reads per grab. The last one is the frame after the LEDs have updated.
GRAB_READS = 5
# 0–255. At or above this, a lit ROI is real signal. Below it, the capture is black.
MIN_ROI_MEAN = 2.0
# exposure_absolute ladder used by --exposure auto. First hit inside the window wins.
EXPOSURE_LADDER = (250, 500, 1000, 2000)
EXPOSURE_MEAN_LOW = 40.0
EXPOSURE_MEAN_HIGH = 230.0
# One mid orange held in every slot while auto exposure is measured.
MID_SWATCH = (220, 120, 20)
# Bright fill used only to prove the camera can see the face before the search.
PROBE_RGB = (255, 180, 40)
POWER_LIMIT_MA = 4000
BLACK_FRAME_MSG = (
    "camera frames are black / ROI mean ~0; check --camera, --roi, --exposure"
)

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

# 5×7 glyphs, high bit on the left. Enough for "255,140,20" and "dE 1.23".
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
}

SHEET_COLS = 6
SHEET_MARGIN = 4
CELL_W = 156
CELL_H = 82
CELL_GAP = 6
SWATCH_H = 46


@dataclass(frozen=True)
class Sample:
    rgb: tuple[int, int, int]
    measured_rgb: tuple[float, float, float]
    delta_e: float
    stage: str


@dataclass(frozen=True)
class CalibResult:
    target_rgb: tuple[int, int, int]
    best_rgb: tuple[int, int, int]
    delta_e: float
    measured_rgb: tuple[float, float, float]
    samples: tuple[Sample, ...]
    applied: bool = False


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


def mean_rgb(image: np.ndarray, roi: Sequence[int]) -> tuple[float, float, float]:
    """Mean sRGB of an OpenCV BGR frame inside ``roi`` (x, y, w, h)."""
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
    crop = np.asarray(frame[y : y + h, x : x + w, :3], dtype=np.float64)
    mean_bgr = crop.reshape(-1, 3).mean(axis=0)
    return (float(mean_bgr[2]), float(mean_bgr[1]), float(mean_bgr[0]))


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


def roi_max_channel_mean(frame: np.ndarray, rois: Sequence[Sequence[int]]) -> float:
    """Largest per-channel mean inside any ROI. Exposure uses this level."""
    if not rois:
        raise ValueError("need at least one ROI")
    return max(max(mean_rgb(frame, roi)) for roi in rois)


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
    sleep: Callable[[float], None],
) -> None:
    if not batch:
        return
    # The ESP32 answers none of these lines. The write is fire-and-forget.
    if not link.test_swatches(batch):
        raise RuntimeError("face link did not accept swatches")
    if settle_s > 0:
        sleep(settle_s)
    frame = camera.grab()
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
        measured = mean_rgb(frame, roi)
        delta = delta_e_2000(srgb_to_lab(measured), target_lab)
        scored[key] = Sample(rgb=key, measured_rgb=measured, delta_e=delta, stage=stage)
        order.append(key)


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
    settle_s: float = 0.0,
    sleep: Callable[[float], None] | None = None,
) -> CalibResult:
    """Show candidates, score them, refine, then leave test mode.

    One ROI is split horizontally to match each frame. More than one ROI
    fixes the swatch count at that length; a short final frame repeats its
    last colour in the unused slots and does not score the repeats.
    """
    if sleep is None:
        sleep = time.sleep
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

    def run_pass(colours: Sequence[tuple[int, int, int]], stage: str) -> None:
        if len(regions) == 1:
            pending = [colour for colour in colours if colour not in scored]
            for chunk in _chunks(pending, frame_n):
                _evaluate_frame(
                    link, camera, chunk, regions, stage, target_lab, scored, order,
                    real_n=len(chunk), settle_s=settle_s, sleep=sleep,
                )
            return
        for chunk in _chunks(list(colours), frame_n):
            real = len(chunk)
            shown = chunk
            if real < frame_n:
                shown = chunk + [chunk[-1]] * (frame_n - real)
            _evaluate_frame(
                link, camera, shown, regions, stage, target_lab, scored, order,
                real_n=real, settle_s=settle_s, sleep=sleep,
            )

    try:
        # Light every slot, then look. A black grab here is the camera or the
        # ROI, and the coarse grid would only record zeros.
        if not link.test_swatches((PROBE_RGB,) * frame_n):
            raise RuntimeError("face link did not accept swatches")
        if settle_s > 0:
            sleep(settle_s)
        probe = camera.grab()
        ensure_rois_fit(probe, regions)
        check_signal(probe, regions)

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


def pick_exposure(
    device: str,
    camera,
    link,
    rois: Sequence[Sequence[int]],
    run,
    *,
    swatches: int = DEFAULT_SWATCHES,
    settle_s: float = 0.0,
    sleep: Callable[[float], None] | None = None,
    ladder: Sequence[int] = EXPOSURE_LADDER,
) -> int:
    """Lock a manual exposure by lighting one mid swatch and walking a short ladder.

    The first step whose ROI max-channel mean sits in
    ``EXPOSURE_MEAN_LOW`` .. ``EXPOSURE_MEAN_HIGH`` wins. Otherwise the
    largest step that did not saturate wins. The ESP32 sends no reply;
    the swatch write is not followed by a read.
    """
    if sleep is None:
        sleep = time.sleep
    steps = tuple(int(step) for step in ladder)
    if not steps or any(step < 1 for step in steps):
        raise ValueError("exposure ladder is empty")
    regions = [tuple(int(v) for v in roi) for roi in rois]
    if not regions:
        raise ValueError("need at least one ROI")
    count = _swatch_count(regions, swatches)
    try:
        if not link.test_swatches((MID_SWATCH,) * count):
            raise RuntimeError("face link did not accept swatches")
        chosen: int | None = None
        largest_nonsat: int | None = None
        applied: int | None = None
        for step in steps:
            lock_camera_controls(device, step, run)
            applied = step
            if settle_s > 0:
                sleep(settle_s)
            frame = camera.grab()
            ensure_rois_fit(frame, regions)
            level = roi_max_channel_mean(frame, regions)
            if EXPOSURE_MEAN_LOW <= level <= EXPOSURE_MEAN_HIGH:
                chosen = step
                break
            if level <= EXPOSURE_MEAN_HIGH:
                largest_nonsat = step
        if chosen is None:
            chosen = largest_nonsat if largest_nonsat is not None else steps[0]
        if chosen != applied:
            lock_camera_controls(device, chosen, run)
        return chosen
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
        _blit(image, x + 4, y + SWATCH_H + 2, commanded, ink)
        _blit(image, x + 4, y + SWATCH_H + 18, f"dE {sample.delta_e:.2f}", ink)
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


def result_payload(result: CalibResult) -> dict:
    def colour(triple) -> list[float]:
        return [round(float(channel), 4) for channel in triple]

    return {
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
                "delta_e": round(float(sample.delta_e), 4),
                "stage": sample.stage,
            }
            for sample in result.samples
        ],
    }


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
) -> int:
    parser = argparse.ArgumentParser(description="Calibrate the face colour through the shell")
    parser.add_argument("--port", default="", help="UART the ESP32 face is on, for example /dev/serial0")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--device", default="/dev/video0", help="v4l2 device for the exposure lock")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="capture width, default 640")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="capture height, default 480")
    parser.add_argument("--roi", action="append", required=True, help="x,y,w,h. Repeat once per swatch, or pass one ROI to split")
    parser.add_argument("--swatches", type=int, default=DEFAULT_SWATCHES, help="swatches per frame when one ROI is split")
    parser.add_argument("--target", default="255,120,0", help="sRGB the camera should see")
    parser.add_argument(
        "--exposure",
        default="auto",
        help="auto (default), or manual exposure_absolute in 100 µs units",
    )
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_S, help="seconds to wait after each swatch frame")
    parser.add_argument("--apply", action="store_true", help="store the winner on the ESP32")
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
        runner = subprocess.run if run is None else run

        # A fixed exposure is locked before the stream starts. Auto has to
        # see frames, so the camera opens first and the ladder runs below.
        if not args.skip_v4l2 and exposure is not None:
            lock_camera_controls(args.device, exposure, runner)

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
            pick_exposure(
                args.device,
                camera,
                link,
                rois,
                runner,
                swatches=args.swatches,
                settle_s=float(args.settle),
                sleep=sleep,
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
