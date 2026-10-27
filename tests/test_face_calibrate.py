"""Face colour protocol, swatch layout, CIEDE2000, and the calibration search."""

from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "clients" / "pi"))

import face_calibrate as cal  # noqa: E402
import face_link  # noqa: E402


# Sharma, Wu, Dalal supplementary CIEDE2000 pairs (Lab1, Lab2, ΔE00).
_SHARMA = (
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
)

_TRUE = (210, 80, 20)


class FakePort:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> int:
        self.buf += bytes(data)
        return len(data)

    def close(self) -> None:
        return None


def _lines(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def orange_shell(rgb) -> tuple[int, int, int]:
    """Integer stand-in for an orange shell. Even inputs spaced by 16 stay unique."""
    red, green, blue = (int(channel) for channel in rgb)
    return (red // 2 + 20, green // 2, blue // 2)


def _last_swatches(raw: bytes) -> list[tuple[int, int, int]]:
    found = None
    for line in raw.decode("utf-8").splitlines():
        decoded = face_link.decode_line(line)
        if decoded and decoded["op"] == "test_swatches":
            rgb = decoded["rgb"]
            found = [tuple(rgb[i : i + 3]) for i in range(0, len(rgb), 3)]
    if found is None:
        raise AssertionError("no swatch line was written")
    return found


class ShellCamera:
    """BGR frame whose ROI bands are orange_shell() of the last swatch command."""

    def __init__(self, port: FakePort, roi=(0, 0, 80, 20), shell=orange_shell) -> None:
        self.port = port
        self.roi = roi
        self.shell = shell
        self.grabs = 0

    def grab(self) -> np.ndarray:
        self.grabs += 1
        colours = _last_swatches(self.port.buf)
        x, y, w, h = self.roi
        image = np.zeros((y + h, x + w, 3), dtype=np.uint8)
        for region, rgb in zip(cal.split_roi_horizontal(self.roi, len(colours)), colours):
            shown = self.shell(rgb)
            rx, ry, rw, rh = region
            image[ry : ry + rh, rx : rx + rw, 0] = shown[2]
            image[ry : ry + rh, rx : rx + rw, 1] = shown[1]
            image[ry : ry + rh, rx : rx + rw, 2] = shown[0]
        return image


def test_encode_bytes_and_round_trip():
    cases = [
        face_link.set_color(255, 140, 20),
        face_link.set_brightness_cap(102),
        face_link.test_fill(1, 2, 3),
        face_link.test_fill(9, 8, 7, 4, 5),
        face_link.test_swatches([(255, 0, 0), (0, 255, 0)]),
        face_link.test_exit(),
    ]
    expected = [
        b'{"op":"set_color","r":255,"g":140,"b":20}\n',
        b'{"op":"set_brightness_cap","cap":102}\n',
        b'{"op":"test_fill","r":1,"g":2,"b":3}\n',
        b'{"op":"test_fill","r":9,"g":8,"b":7,"start":4,"count":5}\n',
        b'{"op":"test_swatches","n":2,"rgb":[255,0,0,0,255,0]}\n',
        b'{"op":"test_exit"}\n',
    ]
    for obj, raw in zip(cases, expected):
        assert face_link.encode_line(obj) == raw
        assert face_link.decode_line(raw) == obj


def test_decode_mirrors_loose_firmware_scan():
    loose = '{ "op" : "set_color" , "b" : 20, "g" : 140, "r" : 255 }\n'
    assert face_link.decode_line(loose) == {"op": "set_color", "r": 255, "g": 140, "b": 20}
    assert face_link.decode_line(b'{"op":"set_color","r":256,"g":0,"b":0}\n') is None
    assert face_link.decode_line(b'{"op":"test_swatches","n":2,"rgb":[1,2,3]}\n') is None
    assert face_link.decode_line(b'{"op":"test_swatches","n":0,"rgb":[]}\n') is None
    assert face_link.decode_line(b'{"op":"test_fill","r":1,"g":2,"b":3,"start":0}\n') is None
    assert face_link.decode_line(b'{"op":"test_fill","r":1,"g":2,"b":3,"start":280,"count":20}\n') is None
    assert face_link.decode_line(b'{"op":"test_fill","r":1,"g":2,"b":3,"start":-1,"count":4}\n') is None
    too_many = face_link.encode_line({"op": "test_swatches", "n": 17, "rgb": [0, 0, 0] * 17})
    assert face_link.decode_line(too_many) is None


def test_encoders_reject_bad_values():
    with pytest.raises(ValueError):
        face_link.set_color(True, 0, 0)
    with pytest.raises(ValueError):
        face_link.set_color(1.5, 0, 0)
    with pytest.raises(ValueError):
        face_link.set_brightness_cap(256)
    with pytest.raises(ValueError):
        face_link.test_fill(0, 0, 0, start=0)
    with pytest.raises(ValueError):
        face_link.test_fill(0, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        face_link.test_fill(0, 0, 0, 280, 20)
    with pytest.raises(ValueError):
        face_link.test_swatches([])
    with pytest.raises(ValueError):
        face_link.test_swatches([(0, 0, 0)] * (face_link.MAX_SWATCHES + 1))
    with pytest.raises(ValueError):
        face_link.test_swatches([(1, 2)])


def test_link_methods_write_the_same_bytes():
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=False)
    assert link.set_color(255, 140, 20) is True
    assert link.set_brightness_cap(40) is True
    assert link.test_fill(3, 4, 5, 1, 2) is True
    assert link.test_swatches([(8, 9, 10)]) is True
    assert link.test_exit() is True
    assert _lines(port.buf) == [
        {"op": "set_color", "r": 255, "g": 140, "b": 20},
        {"op": "set_brightness_cap", "cap": 40},
        {"op": "test_fill", "r": 3, "g": 4, "b": 5, "start": 1, "count": 2},
        {"op": "test_swatches", "n": 1, "rgb": [8, 9, 10]},
        {"op": "test_exit"},
    ]
    assert face_link.FaceLink(serial=None).set_color(1, 2, 3) is False


def test_longest_swatch_line_fits_the_firmware_buffer():
    raw = face_link.encode_line(face_link.test_swatches([(255, 255, 255)] * 16))
    assert len(raw) - 1 <= face_link.LINE_MAX
    assert face_link.decode_line(raw)["n"] == 16
    src = (_REPO / "firmware" / "esp32" / "src" / "main.cpp").read_text(encoding="utf-8")
    assert "lineBuf[512]" in src
    assert "MAX_SWATCHES = 16" in src


def test_swatch_layout_covers_and_puts_remainder_first():
    assert face_link.swatch_layout(10, 3) == [(0, 4), (4, 3), (7, 3)]
    assert face_link.swatch_layout(8, 1) == [(0, 8)]
    assert face_link.swatch_layout(5, 5) == [(0, 1), (1, 1), (2, 1), (3, 1), (4, 1)]
    spans = face_link.swatch_layout(face_link.FACE_LEDS, 7)
    assert spans[0][0] == 0
    assert spans[-1][0] + spans[-1][1] == face_link.FACE_LEDS
    for (start, count), (next_start, _) in zip(spans, spans[1:]):
        assert start + count == next_start
        assert count >= 1
    counts = [count for _, count in spans]
    assert max(counts) - min(counts) <= 1
    assert counts[0] >= counts[-1]
    with pytest.raises(ValueError):
        face_link.swatch_layout(0, 1)
    with pytest.raises(ValueError):
        face_link.swatch_layout(4, 5)
    with pytest.raises(ValueError):
        face_link.swatch_layout(4, 0)


def test_firmware_keeps_power_limit_and_new_ops():
    src = (_REPO / "firmware" / "esp32" / "src" / "main.cpp").read_text(encoding="utf-8")
    for token in (
        "set_color",
        "set_brightness_cap",
        "test_fill",
        "test_swatches",
        "test_exit",
        "Preferences",
        "testMode",
        "DEFAULT_BASE_G = 140",
        "DEFAULT_BASE_B = 20",
        "DEFAULT_BRIGHT_CAP = 102",
        "setMaxPowerInVoltsAndMilliamps(5, 4000)",
        "FACE_LEDS",
        "faceColor",
    ):
        assert token in src
    assert "mouthMask" not in src
    assert "if (testMode) return;" in src


@pytest.mark.parametrize("lab1,lab2,expected", _SHARMA)
def test_ciede2000_sharma(lab1, lab2, expected):
    got = cal.delta_e_2000(lab1, lab2)
    assert got == pytest.approx(expected, abs=1e-4)
    assert cal.delta_e_2000(lab1, lab1) == pytest.approx(0.0, abs=1e-9)


def test_srgb_white_and_red_land_on_known_lab():
    white = cal.srgb_to_lab((255, 255, 255))
    assert white[0] == pytest.approx(100.0, abs=0.02)
    assert white[1] == pytest.approx(0.0, abs=0.05)
    assert white[2] == pytest.approx(0.0, abs=0.05)
    black = cal.srgb_to_lab((0, 0, 0))
    assert black == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    red = cal.srgb_to_lab((255, 0, 0))
    assert red[0] == pytest.approx(53.24, abs=0.05)
    assert red[1] == pytest.approx(80.09, abs=0.15)
    assert red[2] == pytest.approx(67.20, abs=0.15)


def test_mean_rgb_reads_bgr_and_scores_against_target():
    image = np.zeros((10, 20, 3), dtype=np.uint8)
    image[:, :] = (10, 20, 30)  # B, G, R
    assert cal.mean_rgb(image, (0, 0, 20, 10)) == pytest.approx((30.0, 20.0, 10.0))
    measured = cal.mean_rgb(image, (2, 3, 4, 5))
    delta = cal.delta_e_2000(cal.srgb_to_lab(measured), cal.srgb_to_lab((255, 120, 0)))
    assert delta > 1.0
    with pytest.raises(ValueError):
        cal.mean_rgb(image, (18, 0, 4, 4))


def test_split_roi_matches_swatch_remainder():
    assert cal.split_roi_horizontal((10, 4, 10, 6), 3) == [
        (10, 4, 4, 6),
        (14, 4, 3, 6),
        (17, 4, 3, 6),
    ]


def test_search_finds_orange_shell_preimage_and_exits_test_mode():
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=False)
    camera = ShellCamera(port)
    target = orange_shell(_TRUE)
    corners = [
        (194, 64, 4),
        (194, 64, 36),
        (194, 96, 4),
        (194, 96, 36),
        (226, 64, 4),
        (226, 64, 36),
        (226, 96, 4),
        (226, 96, 36),
    ]
    result = cal.calibrate(
        link=link,
        camera=camera,
        rois=[(0, 0, 80, 20)],
        target_rgb=target,
        candidates=corners,
        swatches=4,
        refine_steps=(16,),
        settle_s=0.0,
    )
    assert _TRUE not in corners
    assert result.best_rgb == _TRUE
    assert result.delta_e == pytest.approx(0.0, abs=1e-6)
    assert result.measured_rgb == pytest.approx(target)
    assert all(sample.delta_e + 1e-9 >= result.delta_e for sample in result.samples)
    assert any(sample.stage == "coarse" for sample in result.samples)
    assert any(sample.stage == "refine" and sample.rgb == _TRUE for sample in result.samples)
    assert any(sample.measured_rgb != pytest.approx(sample.rgb) for sample in result.samples)
    ops = [obj["op"] for obj in _lines(port.buf)]
    assert "gesture" not in ops
    assert ops[-1] == "test_exit"
    assert "set_color" not in ops
    assert camera.grabs >= 2


def _swatch_frames(raw: bytes) -> list[list[tuple[int, int, int]]]:
    frames = []
    for line in raw.decode("utf-8").splitlines():
        decoded = face_link.decode_line(line)
        if decoded and decoded["op"] == "test_swatches":
            rgb = decoded["rgb"]
            frames.append([tuple(rgb[i : i + 3]) for i in range(0, len(rgb), 3)])
    return frames


def test_multi_roi_pads_a_short_frame_and_keeps_alignment():
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=False)

    class FixedCamera:
        def grab(self):
            colours = _last_swatches(port.buf)
            image = np.zeros((10, 40, 3), dtype=np.uint8)
            # Two explicit ROIs, not a horizontal split of one box.
            for slot, rgb in zip(((0, 0, 10, 10), (20, 0, 10, 10)), colours):
                x, y, w, h = slot
                image[y : y + h, x : x + w, 0] = rgb[2]
                image[y : y + h, x : x + w, 1] = rgb[1]
                image[y : y + h, x : x + w, 2] = rgb[0]
            return image

    result = cal.calibrate(
        link=link,
        camera=FixedCamera(),
        rois=[(0, 0, 10, 10), (20, 0, 10, 10)],
        target_rgb=(10, 20, 30),
        candidates=[(1, 2, 3), (10, 20, 30), (4, 5, 6)],
        swatches=8,
        refine_steps=(),
        settle_s=0.0,
    )
    assert result.best_rgb == (10, 20, 30)
    assert result.delta_e == pytest.approx(0.0, abs=1e-6)
    frames = _swatch_frames(port.buf)
    assert frames[0] == [cal.PROBE_RGB, cal.PROBE_RGB]
    assert [(1, 2, 3), (10, 20, 30)] in frames
    assert [(4, 5, 6), (4, 5, 6)] in frames
    assert all(len(frame) == 2 for frame in frames)
    assert len(result.samples) == 3
    assert result.sanity["rise"] >= 15


def test_v4l2_tries_both_names_and_continues_after_failure():
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        ctrl = cmd[-1]
        if "white_balance_automatic=" in ctrl or "exposure_time_absolute=" in ctrl:
            return SimpleNamespace(returncode=1, stderr="unknown control")
        if "exposure_absolute=" in ctrl:
            raise OSError("v4l2-ctl missing")
        return SimpleNamespace(returncode=0, stderr="")

    applied = cal.lock_camera_controls("/dev/video0", 250, run)
    joined = [" ".join(cmd) for cmd in calls]
    assert any("--set-ctrl=white_balance_automatic=0" in line for line in joined)
    assert any("--set-ctrl=white_balance_temperature_auto=0" in line for line in joined)
    assert any("--set-ctrl=white_balance_temperature=4000" in line for line in joined)
    assert any("--set-ctrl=auto_exposure=1" in line for line in joined)
    assert any("--set-ctrl=exposure_auto=1" in line for line in joined)
    assert any("--set-ctrl=exposure_time_absolute=250" in line for line in joined)
    assert any("--set-ctrl=exposure_absolute=250" in line for line in joined)
    assert "white_balance_temperature" in applied
    assert "white_balance_automatic" not in applied
    assert "exposure_absolute" not in applied
    assert all(cmd[0] == "v4l2-ctl" and cmd[1:3] == ["-d", "/dev/video0"] for cmd in calls)


def test_main_apply_writes_winner_and_saves_sheet(tmp_path):
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=False)
    camera = ShellCamera(port)
    target = orange_shell(_TRUE)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    code = cal.main(
        [
            "--roi", "0,0,80,20",
            "--swatches", "4",
            "--target", f"{target[0]},{target[1]},{target[2]}",
            "--grid-r", "194,226",
            "--grid-g", "64,96",
            "--grid-b", "4,36",
            "--refine", "16",
            "--settle", "0",
            "--apply",
            "--logs", str(tmp_path),
            "--exposure", "250",
        ],
        link=link,
        camera=camera,
        run=run,
        clock=lambda: time.gmtime(0),
        sleep=lambda _seconds: None,
    )
    assert code == 0
    assert calls, "v4l2 runner was not used"
    objs = _lines(port.buf)
    assert objs[-2]["op"] == "test_exit"
    assert objs[-1] == {"op": "set_color", "r": _TRUE[0], "g": _TRUE[1], "b": _TRUE[2]}
    saved = json.loads((tmp_path / "face-cal-19700101-000000.json").read_text(encoding="utf-8"))
    assert saved["best_rgb"] == list(_TRUE)
    assert saved["applied"] is True
    assert saved["test_mode_bypasses_brightness_cap"] is True
    assert saved["power_limit_milliamps"] == 4000
    assert saved["sanity"]["rise"] >= 15
    assert saved["exposure"]["chosen"] == 250
    assert "peak_rgb" in saved["samples"][0]
    assert "clip_fraction" in saved["samples"][0]
    sheet = (tmp_path / "face-cal-19700101-000000.png").read_bytes()
    assert sheet.startswith(b"\x89PNG\r\n\x1a\n")
    assert sheet[12:16] == b"IHDR"
    width, height = struct.unpack(">II", sheet[16:24])
    assert width > 100 and height > 40
    image = cal.render_contact_sheet(
        [cal.Sample(rgb=_TRUE, measured_rgb=tuple(float(c) for c in target), delta_e=0.0, stage="refine")],
        _TRUE,
    )
    x, y = cal.cell_origin(0)
    centre = image[y + cal.SWATCH_H // 2, x + cal.CELL_W // 2]
    assert tuple(int(v) for v in centre) == target
    labelled = cal.Sample(
        rgb=_TRUE,
        measured_rgb=tuple(float(c) for c in target),
        delta_e=0.0,
        stage="refine",
        peak_rgb=(250.0, 10.0, 4.0),
        clip_fraction=(0.25, 0.0, 0.0),
    )
    sheet = cal.render_contact_sheet([labelled], _TRUE)
    label = sheet[y + cal.SWATCH_H + 50 : y + cal.SWATCH_H + 64, x : x + 80]
    assert int(label.max()) > 200


def test_label_font_draws_peak_and_clip_glyphs():
    for char in "PC%":
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        cal._blit(image, 0, 0, char, (255, 255, 255), scale=1)
        assert int(image.sum()) > 0, char


def test_refine_around_clamps_and_skips_the_centre():
    colours = cal.refine_around((1, 255, 10), 16)
    assert (1, 255, 10) not in colours
    assert (0, 255, 10) in colours
    assert (1, 255, 26) in colours
    assert len(colours) == len(set(colours))
