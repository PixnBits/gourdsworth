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


def test_exposure_auto_picks_the_first_usable_step_and_falls_back():
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
    assert chosen == 500
    assert state["exp"] == 500
    joined = [" ".join(cmd) for cmd in state["commands"]]
    assert any("exposure_time_absolute=500" in line for line in joined)
    assert any("exposure_absolute=500" in line for line in joined)
    assert any("auto_exposure=1" in line for line in joined)
    assert any("exposure_auto=1" in line for line in joined)
    assert not any("exposure_absolute=1000" in line for line in joined)
    ops = _ops(port)
    assert "test_swatches" in ops
    assert ops[-1] == "test_exit"
    assert port.reads == 0

    port_b, link_b = _link()
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
    assert fallback == 500
    assert state_b["exp"] == 500

    port_c, link_c = _link()
    camera_c, run_c, state_c = _exposure_camera({250: 240, 500: 250, 1000: 255, 2000: 255})
    saturated = cal.pick_exposure(
        "/dev/video0",
        camera_c,
        link_c,
        [(0, 0, 40, 30)],
        run_c,
        swatches=1,
        settle_s=0.0,
        sleep=lambda _seconds: None,
    )
    assert saturated == 250
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
            "--target", "90,90,90",
        ],
        link=link,
        camera=camera,
        run=run,
        clock=lambda: __import__("time").gmtime(0),
        sleep=lambda _seconds: None,
    )
    assert code == 0
    assert state["exp"] == 500
    assert (tmp_path / "face-cal-19700101-000000.json").is_file()
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
        ],
        link=link,
        camera=LitStripCamera(),
        run=run,
        sleep=lambda _seconds: None,
    )
    assert code == 0
    assert calls == []
    assert port.reads == 0
