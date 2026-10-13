"""Camera size, black-frame guard, and exposure auto-pick. No hardware."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "clients" / "pi"))

import face_calibrate as cal  # noqa: E402
import face_link  # noqa: E402


class FakePort:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.reads = 0

    def write(self, data: bytes) -> int:
        self.buf += bytes(data)
        return len(data)

    def read(self, _n: int = 1):
        self.reads += 1
        raise AssertionError("calibration must not read a serial reply")

    def close(self) -> None:
        return None


def _ops(port: FakePort) -> list[str]:
    ops = []
    for line in bytes(port.buf).decode("utf-8").splitlines():
        if line.strip():
            ops.append(json.loads(line)["op"])
    return ops


def _link() -> tuple[FakePort, face_link.FaceLink]:
    port = FakePort()
    return port, face_link.FaceLink(serial=port, pace=False)


class BlackCamera:
    def grab(self) -> np.ndarray:
        return np.zeros((480, 640, 3), dtype=np.uint8)


class LitStripCamera:
    """640×480 frame, dark except the example swatch strip."""

    def grab(self) -> np.ndarray:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        image[300:330, 230:370] = (20, 80, 200)  # BGR → sRGB 200, 80, 20
        return image


def test_check_signal_accepts_a_lit_roi_on_a_dark_frame():
    frame = LitStripCamera().grab()
    # The 140×30 strip is ~1% of the frame, so the full-frame mean is under 2.
    assert float(frame.mean()) < cal.MIN_ROI_MEAN
    cal.check_signal(frame, [(230, 300, 140, 30)])


def test_check_signal_rejects_a_black_frame_and_a_black_roi():
    black = np.zeros((480, 640, 3), dtype=np.uint8)
    with pytest.raises(RuntimeError, match="camera frames are black / ROI mean ~0"):
        cal.check_signal(black, [(230, 300, 140, 30)])
    bright = np.full((480, 640, 3), 80, dtype=np.uint8)
    bright[300:330, 230:370] = 0
    with pytest.raises(RuntimeError, match="check --camera, --roi, --exposure"):
        cal.check_signal(bright, [(230, 300, 140, 30)])


def test_black_frames_exit_nonzero_and_do_not_apply(tmp_path, capsys):
    port, link = _link()
    code = cal.main(
        [
            "--roi", "230,300,140,30",
            "--swatches", "1",
            "--apply",
            "--skip-v4l2",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "120",
            "--grid-b", "0",
            "--refine", "none",
            "--settle", "0",
        ],
        link=link,
        camera=BlackCamera(),
        sleep=lambda _seconds: None,
    )
    assert code != 0
    err = capsys.readouterr().err
    assert "camera frames are black / ROI mean ~0" in err
    assert "check --camera, --roi, --exposure" in err
    ops = _ops(port)
    assert "set_color" not in ops
    assert "test_exit" in ops
    assert port.reads == 0
    assert list(tmp_path.glob("face-cal-*")) == []


def test_lit_frames_pass_the_guard_and_can_apply(tmp_path):
    port, link = _link()
    code = cal.main(
        [
            "--roi", "230,300,140,30",
            "--swatches", "1",
            "--target", "200,80,20",
            "--apply",
            "--skip-v4l2",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "120",
            "--grid-b", "0",
            "--refine", "none",
            "--settle", "0",
            "--skip-sanity",
        ],
        link=link,
        camera=LitStripCamera(),
        clock=lambda: __import__("time").gmtime(0),
        sleep=lambda _seconds: None,
    )
    assert code == 0
    ops = _ops(port)
    assert ops[-2] == "test_exit"
    assert ops[-1] == "set_color"
    saved = json.loads((tmp_path / "face-cal-19700101-000000.json").read_text(encoding="utf-8"))
    assert saved["applied"] is True
    assert port.reads == 0


def test_samples_that_measure_black_skip_apply(tmp_path, capsys):
    """Probe sees light, then every scored swatch is black."""
    port, link = _link()

    class ProbeThenBlack:
        def grab(self) -> np.ndarray:
            raw = bytes(port.buf).decode("utf-8")
            image = np.zeros((48, 64, 3), dtype=np.uint8)
            if f'"r":{cal.PROBE_RGB[0]}' in raw.split("test_swatches")[-1] or raw.rstrip().endswith(
                str(cal.PROBE_RGB[0])
            ):
                pass
            # The probe line is the latest test_swatches when the face is the probe colour.
            last = None
            for line in raw.splitlines():
                if '"op":"test_swatches"' in line:
                    last = line
            if last is not None and f"{cal.PROBE_RGB[0]},{cal.PROBE_RGB[1]},{cal.PROBE_RGB[2]}" in last.replace(" ", ""):
                image[:] = 90
            return image

    code = cal.main(
        [
            "--roi", "0,0,64,48",
            "--swatches", "1",
            "--apply",
            "--skip-v4l2",
            "--logs", str(tmp_path),
            "--grid-r", "10",
            "--grid-g", "20",
            "--grid-b", "30",
            "--refine", "none",
            "--settle", "0",
            "--skip-sanity",
        ],
        link=link,
        camera=ProbeThenBlack(),
        sleep=lambda _seconds: None,
    )
    assert code != 0
    assert "camera frames are black / ROI mean ~0" in capsys.readouterr().err
    ops = _ops(port)
    assert "set_color" not in ops
    assert ops[-1] == "test_exit"
    assert list(tmp_path.glob("face-cal-*")) == []


def test_cvcamera_requests_640x480_and_discards_warmup(monkeypatch):
    state = {"reads": 0, "sets": [], "index": None}

    class FakeCap:
        def __init__(self) -> None:
            self.width = 0
            self.height = 0

        def isOpened(self) -> bool:
            return True

        def set(self, prop, value) -> bool:
            state["sets"].append((prop, value))
            if prop == 3:
                self.width = int(value)
            elif prop == 4:
                self.height = int(value)
            return True

        def read(self):
            state["reads"] += 1
            # Deliver the size that was requested, which is what a cooperating
            # 640×480 stream does. A later instance below ignores the request.
            return True, np.zeros((self.height, self.width, 3), dtype=np.uint8)

        def get(self, _prop) -> float:
            return 0.0

        def release(self) -> None:
            return None

    class IgnoringCap(FakeCap):
        def read(self):
            state["reads"] += 1
            return True, np.zeros((240, 320, 3), dtype=np.uint8)

    caps = {"next": FakeCap}

    class FakeCV2:
        CAP_PROP_FRAME_WIDTH = 3
        CAP_PROP_FRAME_HEIGHT = 4
        CAP_PROP_BUFFERSIZE = 38

        def VideoCapture(self, index):
            state["index"] = index
            return caps["next"]()

    monkeypatch.setitem(sys.modules, "cv2", FakeCV2())
    camera = cal.CvCamera(0)
    try:
        assert state["index"] == 0
        assert state["reads"] == cal.WARMUP_FRAMES
        assert (3, 640) in state["sets"]
        assert (4, 480) in state["sets"]
        assert (38, 1) in state["sets"]
        assert camera.frame_size == (640, 480)
        before = state["reads"]
        frame = camera.grab()
        assert state["reads"] - before == cal.GRAB_READS
        assert frame.shape == (480, 640, 3)
        assert camera.frame_size == (640, 480)
    finally:
        camera.close()

    state["sets"].clear()
    wide = cal.CvCamera(2, width=800, height=600)
    try:
        assert (3, 800) in state["sets"]
        assert (4, 600) in state["sets"]
        assert wide.frame_size == (800, 600)
    finally:
        wide.close()

    caps["next"] = IgnoringCap
    stale = cal.CvCamera(0, width=1920, height=1080)
    try:
        assert (3, 1920) in state["sets"]
        assert stale.frame_size == (320, 240)
    finally:
        stale.close()


def test_roi_outside_the_frame_errors_and_exits_test_mode():
    port, link = _link()

    class Small:
        def grab(self) -> np.ndarray:
            return np.zeros((480, 640, 3), dtype=np.uint8)

    with pytest.raises(ValueError, match="outside"):
        cal.calibrate(
            link=link,
            camera=Small(),
            rois=[(600, 400, 80, 40)],
            target_rgb=(255, 120, 0),
            candidates=[(255, 120, 0)],
            swatches=1,
            refine_steps=(),
            settle_s=0.0,
        )
    ops = _ops(port)
    assert "test_exit" in ops
    assert "set_color" not in ops
    assert port.reads == 0


def test_main_roi_outside_recorded_frame_size_returns_nonzero(tmp_path, capsys):
    port, link = _link()

    class Sized:
        frame_size = (640, 480)

        def grab(self):
            raise AssertionError("geometry should fail before a grab")

    code = cal.main(
        [
            "--roi", "600,10,80,20",
            "--skip-v4l2",
            "--settle", "0",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "120",
            "--grid-b", "0",
            "--refine", "none",
            "--swatches", "1",
        ],
        link=link,
        camera=Sized(),
        sleep=lambda _seconds: None,
    )
    assert code != 0
    assert "outside" in capsys.readouterr().err
    assert "set_color" not in _ops(port)
    assert list(tmp_path.glob("face-cal-*")) == []


def _exposure_camera(levels: dict[int, int]):
    state = {"exp": None, "commands": []}

    class Cam:
        def grab(self) -> np.ndarray:
            level = levels[state["exp"]]
            image = np.zeros((30, 40, 3), dtype=np.uint8)
            image[:, :] = level
            return image

    def run(cmd, **kwargs):
        state["commands"].append(list(cmd))
        ctrl = cmd[-1]
        if "exposure_absolute=" in ctrl or "exposure_time_absolute=" in ctrl:
            state["exp"] = int(ctrl.rsplit("=", 1)[-1])
        return SimpleNamespace(returncode=0, stderr="")

    return Cam(), run, state


def test_exposure_auto_picks_the_brightest_unclipped_rung_under_the_cap():
    port, link = _link()
    camera, run, state = _exposure_camera({250: 10, 500: 80, 1000: 140, 2000: 250})
    chosen = cal.pick_exposure(
        "/dev/video0",
        camera,
        link,
        [(0, 0, 40, 30)],
        run,
        swatches=1,
        settle_s=0.0,
        sleep=lambda _seconds: None,
    )
    assert chosen.chosen == 1000
    assert chosen.all_clipped is False
    assert [rung.exposure for rung in chosen.rungs] == [250, 500, 1000, 2000]
    assert chosen.rungs[-1].clip_fraction[0] == pytest.approx(1.0)
    assert state["exp"] == 1000
    joined = [" ".join(cmd) for cmd in state["commands"]]
    assert any("exposure_time_absolute=500" in line for line in joined)
    assert any("exposure_absolute=500" in line for line in joined)
    assert any("exposure_absolute=1000" in line for line in joined)
    assert any("exposure_absolute=2000" in line for line in joined)
    assert any("auto_exposure=1" in line for line in joined)
    assert any("exposure_auto=1" in line for line in joined)
    ops = _ops(port)
    assert "test_swatches" in ops
    assert ops[-1] == "test_exit"
    assert port.reads == 0

    # 240 is under the clip level and over the mean cap, so 500 still wins.
    _port_b, link_b = _link()
    camera_b, run_b, state_b = _exposure_camera({250: 5, 500: 15, 1000: 240, 2000: 255})
    fallback = cal.pick_exposure(
        "/dev/video0",
        camera_b,
        link_b,
        [(0, 0, 40, 30)],
        run_b,
        swatches=1,
        settle_s=0.0,
        sleep=lambda _seconds: None,
    )
    assert fallback.chosen == 500
    assert state_b["exp"] == 500

    # The only unclipped rung is hotter than the cap. Keep it.
    _port_c, link_c = _link()
    _camera_c, run_c, state_c = _exposure_camera({250: 240, 500: 250, 1000: 255, 2000: 255})
    hot = cal.pick_exposure(
        "/dev/video0",
        _camera_c,
        link_c,
        [(0, 0, 40, 30)],
        run_c,
        swatches=1,
        settle_s=0.0,
        sleep=lambda _seconds: None,
    )
    assert hot.chosen == 250
    assert hot.all_clipped is False
    assert state_c["exp"] == 250


def test_main_auto_exposure_uses_the_ladder(tmp_path):
    port, link = _link()
    camera, run, state = _exposure_camera({250: 12, 500: 90, 1000: 160, 2000: 250})
    code = cal.main(
        [
            "--roi", "0,0,40,30",
            "--swatches", "1",
            "--exposure", "auto",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "100",
            "--grid-b", "0",
            "--refine", "none",
            "--settle", "0",
            "--skip-sanity",
            "--target", "90,90,90",
        ],
        link=link,
        camera=camera,
        run=run,
        clock=lambda: __import__("time").gmtime(0),
        sleep=lambda _seconds: None,
    )
    assert code == 0
    assert state["exp"] == 1000
    saved = json.loads((tmp_path / "face-cal-19700101-000000.json").read_text(encoding="utf-8"))
    assert saved["exposure"]["chosen"] == 1000
    assert [rung["exposure"] for rung in saved["exposure"]["rungs"]] == [250, 500, 1000, 2000]
    assert saved["exposure"]["rungs"][-1]["clipped"] is True
    assert saved["exposure"]["rungs"][2]["clipped"] is False
    assert "set_color" not in _ops(port)


def test_skip_v4l2_leaves_auto_exposure(tmp_path):
    port, link = _link()
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    code = cal.main(
        [
            "--roi", "230,300,140,30",
            "--swatches", "1",
            "--exposure", "auto",
            "--skip-v4l2",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "120",
            "--grid-b", "0",
            "--refine", "none",
            "--settle", "0",
            "--skip-sanity",
        ],
        link=link,
        camera=LitStripCamera(),
        run=run,
        sleep=lambda _seconds: None,
    )
    assert code == 0
    assert calls == []
    assert port.reads == 0


def _swatch_colours(port: FakePort) -> list[tuple[int, int, int]]:
    found = None
    for line in bytes(port.buf).decode("utf-8").splitlines():
        decoded = face_link.decode_line(line)
        if decoded and decoded["op"] == "test_swatches":
            rgb = decoded["rgb"]
            found = [tuple(rgb[i : i + 3]) for i in range(0, len(rgb), 3)]
    if found is None:
        raise AssertionError("no swatch line was written")
    return found


def _paint(rgb: tuple[int, int, int], width: int = 16, height: int = 16) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 2] = rgb[0]
    image[:, :, 1] = rgb[1]
    image[:, :, 0] = rgb[2]
    return image


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


class _EchoCamera:
    """The ROI shows the latest swatch command."""

    def __init__(self, port: FakePort, width: int = 16, height: int = 16) -> None:
        self.port = port
        self.width = width
        self.height = height

    def grab(self) -> np.ndarray:
        return _paint(_swatch_colours(self.port)[0], self.width, self.height)


class _LagCamera:
    """The previous command stays up for ``lag`` grabs after each new command."""

    def __init__(self, port: FakePort, lag: int) -> None:
        self.port = port
        self.lag = lag
        self.current: tuple | None = None
        self.previous: tuple[int, int, int] = (80, 80, 80)
        self.left = 0
        self.stale_grabs = 0

    def grab(self) -> np.ndarray:
        colours = tuple(tuple(colour) for colour in _swatch_colours(self.port))
        if colours != self.current:
            if self.current is not None:
                self.previous = self.current[0]
            self.current = colours
            self.left = self.lag
        if self.left > 0:
            self.left -= 1
            show = self.previous
            self.stale_grabs += 1
        else:
            show = self.current[0]
        return _paint(show)


def test_clip_fraction_counts_pixels_at_or_above_250():
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    image[0, :, 2] = 250  # 10 of 100 red pixels
    image[1, :3, 1] = 255  # 3 green pixels
    stats = cal.roi_measurement(image, (0, 0, 10, 10))
    assert stats["clip"][0] == pytest.approx(0.1)
    assert stats["clip"][1] == pytest.approx(0.03)
    assert stats["clip"][2] == pytest.approx(0.0)
    assert stats["peak"] == pytest.approx((250.0, 255.0, 0.0))
    assert stats["mean"][0] == pytest.approx(25.0)


def test_capture_drains_frames_that_finish_before_settle_then_medians():
    clock = _Clock()
    seen = []

    class Cam:
        def __init__(self) -> None:
            self.n = 0

        def grab(self):
            clock.t += 0.1
            self.n += 1
            seen.append(clock.t)
            level = {6: 10, 7: 100, 8: 11}.get(self.n, 1)
            return np.full((2, 2, 3), level, dtype=np.uint8)

    frame = cal.capture_settled(
        Cam(),
        command_time=0.0,
        settle_s=0.25,
        drain_frames=3,
        median_frames=3,
        clock=clock,
        sleep=lambda _seconds: None,
    )
    # 0.1 and 0.2 finish before the deadline and do not count as drain frames.
    assert seen == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
    assert int(frame[0, 0, 0]) == 11


def test_one_command_lag_is_fixed_by_the_drain(tmp_path):
    assert cal.DEFAULT_DRAIN_FRAMES == 3
    assert cal.DEFAULT_MEDIAN_FRAMES == 3
    assert cal.DEFAULT_SETTLE_S == pytest.approx(0.25)
    assert cal.DEFAULT_SWATCHES == 1
    port, link = _link()
    timer = _Clock()
    camera = _LagCamera(port, lag=cal.DEFAULT_DRAIN_FRAMES)
    code = cal.main(
        [
            "--roi", "0,0,16,16",
            "--target", "255,120,0",
            "--apply",
            "--skip-v4l2",
            "--logs", str(tmp_path),
            "--grid-r", "255,10",
            "--grid-g", "120,20",
            "--grid-b", "0,30",
            "--refine", "none",
            "--settle", "0.25",
        ],
        link=link,
        camera=camera,
        clock=lambda: __import__("time").gmtime(0),
        now=timer,
        sleep=timer.sleep,
    )
    assert code == 0
    assert camera.stale_grabs > 0
    ops = _ops(port)
    assert ops[-2] == "test_exit"
    assert ops[-1] == "set_color"
    saved = json.loads((tmp_path / "face-cal-19700101-000000.json").read_text(encoding="utf-8"))
    assert saved["best_rgb"] == [255, 120, 0]
    assert saved["measured_rgb"] == pytest.approx([255.0, 120.0, 0.0])
    assert saved["samples"][0]["peak_rgb"][0] == pytest.approx(255.0)
    assert saved["samples"][0]["clip_fraction"][0] == pytest.approx(1.0)
    winner = next(sample for sample in saved["samples"] if sample["rgb"] == [255, 120, 0])
    assert winner["clip_fraction"][0] == pytest.approx(1.0)
    assert port.reads == 0
    frames = []
    for line in bytes(port.buf).decode("utf-8").splitlines():
        decoded = face_link.decode_line(line)
        if decoded and decoded["op"] == "test_swatches":
            frames.append(decoded["n"])
    assert frames[0] == 1
    assert all(count == 1 for count in frames)


def test_stale_camera_fails_sanity_without_set_color(tmp_path, capsys):
    port, link = _link()

    class Stale:
        def grab(self) -> np.ndarray:
            return np.full((16, 16, 3), 40, dtype=np.uint8)

    code = cal.main(
        [
            "--roi", "0,0,16,16",
            "--apply",
            "--skip-v4l2",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "120",
            "--grid-b", "0",
            "--refine", "none",
            "--settle", "0",
            "--target", "255,120,0",
        ],
        link=link,
        camera=Stale(),
        sleep=lambda _seconds: None,
    )
    assert code != 0
    assert cal.SANITY_MSG in capsys.readouterr().err
    ops = _ops(port)
    assert "set_color" not in ops
    assert ops[-1] == "test_exit"
    assert port.reads == 0
    assert list(tmp_path.glob("face-cal-*")) == []


def test_green_filter_passes_sanity_on_total_brightness(tmp_path):
    """Orange plastic leaves G near 1. Brightness still rises with the red channel."""
    port, link = _link()

    class GreenFilter:
        def __init__(self, serial: FakePort) -> None:
            self.serial = serial

        def grab(self) -> np.ndarray:
            red, _green, blue = _swatch_colours(self.serial)[0]
            green = 1 if red + blue > 0 else 0
            return _paint((red, green, blue))

    code = cal.main(
        [
            "--roi", "0,0,16,16",
            "--apply",
            "--skip-v4l2",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "120",
            "--grid-b", "0",
            "--refine", "none",
            "--settle", "0",
            "--target", "255,120,0",
        ],
        link=link,
        camera=GreenFilter(port),
        clock=lambda: __import__("time").gmtime(0),
        sleep=lambda _seconds: None,
    )
    assert code == 0
    saved = json.loads((tmp_path / "face-cal-19700101-000000.json").read_text(encoding="utf-8"))
    assert saved["applied"] is True
    assert saved["sanity"]["on"]["measured_rgb"][1] == pytest.approx(1.0)
    assert saved["sanity"]["rise"] >= 15
    ramp = [step["brightness"] for step in saved["sanity"]["ramp"]]
    assert ramp[-1] - ramp[0] >= 15
    assert all(current + 1e-6 >= previous for previous, current in zip(ramp, ramp[1:]))
    assert all(step["measured_rgb"][1] <= 1.0 for step in saved["sanity"]["ramp"])
    assert _ops(port)[-1] == "set_color"
    assert port.reads == 0


def test_one_roi_defaults_to_a_single_swatch_and_the_flag_opts_in(tmp_path):
    port, link = _link()
    code = cal.main(
        [
            "--roi", "0,0,16,16",
            "--skip-v4l2",
            "--skip-sanity",
            "--settle", "0",
            "--logs", str(tmp_path),
            "--grid-r", "255",
            "--grid-g", "120",
            "--grid-b", "0",
            "--refine", "none",
        ],
        link=link,
        camera=_EchoCamera(port),
        sleep=lambda _seconds: None,
    )
    assert code == 0
    counts = []
    for line in bytes(port.buf).decode("utf-8").splitlines():
        decoded = face_link.decode_line(line)
        if decoded and decoded["op"] == "test_swatches":
            counts.append(decoded["n"])
    assert counts[0] == 1
    assert all(count == 1 for count in counts)

    port_n, link_n = _link()
    code_n = cal.main(
        [
            "--roi", "0,0,40,10",
            "--swatches", "4",
            "--skip-v4l2",
            "--skip-sanity",
            "--settle", "0",
            "--logs", str(tmp_path),
            "--grid-r", "10",
            "--grid-g", "20",
            "--grid-b", "30",
            "--refine", "none",
        ],
        link=link_n,
        camera=_EchoCamera(port_n, width=40, height=10),
        sleep=lambda _seconds: None,
    )
    assert code_n == 0
    probe = None
    for line in bytes(port_n.buf).decode("utf-8").splitlines():
        decoded = face_link.decode_line(line)
        if decoded and decoded["op"] == "test_swatches":
            probe = decoded
            break
    assert probe is not None and probe["n"] == 4


def test_exposure_rejects_a_clipped_rung_and_skips_the_first(capsys):
    """The old rule would have kept 250, the first mean inside 40..230."""
    port, link = _link()
    state = {"exp": None, "commands": []}

    class Cam:
        def grab(self) -> np.ndarray:
            image = np.full((20, 20, 3), 40, dtype=np.uint8)
            if state["exp"] == 500:
                image[:] = 100
                image.reshape(-1, 3)[:8] = 255  # 8/400 = 0.02, still legal
            elif state["exp"] == 1000:
                image[:] = 150
                image.reshape(-1, 3)[:9] = 255  # just over 0.02
            elif state["exp"] == 2000:
                image[:] = 255
            return image

    def run(cmd, **kwargs):
        state["commands"].append(list(cmd))
        ctrl = cmd[-1]
        if "exposure_absolute=" in ctrl or "exposure_time_absolute=" in ctrl:
            state["exp"] = int(ctrl.rsplit("=", 1)[-1])
        return SimpleNamespace(returncode=0, stderr="")

    chosen = cal.pick_exposure(
        "/dev/video0",
        Cam(),
        link,
        [(0, 0, 20, 20)],
        run,
        swatches=1,
        settle_s=0.0,
        sleep=lambda _seconds: None,
    )
    assert chosen.chosen == 500
    by_exp = {rung.exposure: rung for rung in chosen.rungs}
    assert by_exp[250].max_channel_mean == pytest.approx(40.0)
    assert by_exp[500].clip_fraction[0] == pytest.approx(0.02)
    assert by_exp[1000].clip_fraction[0] > 0.02
    assert _clipped_rung(by_exp[1000])
    assert state["exp"] == 500
    joined = [" ".join(cmd) for cmd in state["commands"]]
    assert any("exposure_absolute=2000" in line for line in joined)
    assert port.reads == 0

    port_all, link_all = _link()
    seen = {"exp": None}

    class Hot:
        def grab(self) -> np.ndarray:
            return np.full((8, 8, 3), 255, dtype=np.uint8)

    def run_all(cmd, **kwargs):
        ctrl = cmd[-1]
        if "exposure_absolute=" in ctrl or "exposure_time_absolute=" in ctrl:
            seen["exp"] = int(ctrl.rsplit("=", 1)[-1])
        return SimpleNamespace(returncode=0, stderr="")

    clipped = cal.pick_exposure(
        "/dev/video0",
        Hot(),
        link_all,
        [(0, 0, 8, 8)],
        run_all,
        swatches=1,
        settle_s=0.0,
        sleep=lambda _seconds: None,
        ladder=(2000, 500),
    )
    assert clipped.all_clipped is True
    assert clipped.chosen == 500
    assert seen["exp"] == 500
    assert "clipped on every rung" in capsys.readouterr().err


def _clipped_rung(rung) -> bool:
    return any(channel > 0.02 for channel in rung.clip_fraction)
