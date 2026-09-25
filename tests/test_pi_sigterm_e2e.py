"""End-to-end: SIGTERM/SIGHUP to a running --continuous client closes the mic stream.

Runs the real crate_client.py in a subprocess against a fake desktop socket and a
fake ``sounddevice`` module (no audio hardware). The fake BRIO only accepts 48 kHz.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CLIENT = _REPO / "clients" / "pi" / "crate_client.py"
sys.path.insert(0, str(_REPO / "src"))

from gourdsworth.net import recv_msg, send_msg  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals only")

_FAKE_SD = textwrap.dedent(
    '''
    """Fake sounddevice for crate_client subprocess tests. Logs calls to FAKE_SD_LOG."""
    import os
    import time

    import numpy as np

    _LOG = os.environ["FAKE_SD_LOG"]


    def _log(msg):
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\\n")


    class _Default:
        device = (1, 0)


    default = _Default()
    _DEV = {
        "name": "Fake BRIO",
        "default_samplerate": 48000.0,
        "max_input_channels": 2,
        "max_output_channels": 2,
    }


    def query_devices(index=None):
        return [_DEV, _DEV] if index is None else dict(_DEV)


    def check_input_settings(**kwargs):
        if int(kwargs["samplerate"]) != 48000:
            raise ValueError("Invalid sample rate")


    def rec(frames, samplerate=None, channels=1, dtype="int16"):
        _log(f"rec {samplerate}")
        time.sleep(float(frames) / float(samplerate))
        return np.zeros((frames, channels), dtype=dtype)


    def wait():
        return None


    def play(data, samplerate=None, blocking=False):
        _log("play")


    def stop(ignore_errors=True):
        _log("stop")
    '''
)


class _FakeDesktop:
    """Accept one crate, answer hello with ready, record event names."""

    def __init__(self) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.events: list[str] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        self.srv.settimeout(20)
        try:
            sock, _ = self.srv.accept()
        except OSError:
            return
        sock.settimeout(20)
        buf = bytearray()
        try:
            while True:
                header, _payload = recv_msg(sock, buf)
                ev = str(header.get("event"))
                self.events.append(ev)
                if ev == "hello":
                    send_msg(sock, {"event": "ready"})
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            sock.close()

    def close(self) -> None:
        self.srv.close()


def _wait_for(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_signal_stops_continuous_client_and_closes_stream(tmp_path, signum):
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    (fake_dir / "sounddevice.py").write_text(_FAKE_SD, encoding="utf-8")
    sd_log = tmp_path / "sd.log"
    client_log = tmp_path / "crate-pi.log"
    desk = _FakeDesktop()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(fake_dir), str(_REPO / "src")])
    env["FAKE_SD_LOG"] = str(sd_log)
    env["CRATE_PLAYBACK"] = "sd"
    for key in ("CRATE_INPUT", "CRATE_OUTPUT", "CRATE_INPUT_RATE", "CRATE_HOST", "CRATE_PORT"):
        env.pop(key, None)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(_CLIENT),
            "--continuous",
            "--no-camera",
            "--host",
            "127.0.0.1",
            "--port",
            str(desk.port),
            "--input",
            "1",
            "--log-file",
            str(client_log),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )
    try:
        recording = _wait_for(
            lambda: sd_log.is_file() and "rec 48000" in sd_log.read_text(), 20.0
        )
        assert recording, client_log.read_text() if client_log.is_file() else ""
        t0 = time.monotonic()
        proc.send_signal(signum)
        out, _ = proc.communicate(timeout=10)
        elapsed = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        desk.close()
    sd_calls = sd_log.read_text()
    assert proc.returncode == 0, out
    assert elapsed < 6.0, out
    # 48 kHz native capture, never 16 kHz on this device
    assert "rec 16000" not in sd_calls
    assert "capture sample rate: 48000 Hz" in out
    # stream stopped/closed before exit, cleanup ran, goodbye WAV skipped
    assert "stop" in sd_calls.splitlines()
    assert "devices released" in out
    assert "skipping canned goodbye audio" in out
    assert f"caught {signal.Signals(signum).name}" in out
    assert _wait_for(lambda: "bye" in desk.events, 2.0), desk.events
