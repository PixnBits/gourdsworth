"""Uplink teardown and reconnect steps must not block the crate client forever."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "clients" / "pi"))
sys.path.insert(0, str(_REPO / "src"))

import crate_client as cc  # noqa: E402


class _HangStream:
    """stop() and close() never return."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")
        threading.Event().wait()

    def close(self) -> None:
        self.calls.append("close")
        threading.Event().wait()


class _FastStream:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")


class _HangPwPlay:
    """Stand-in for ``subprocess.Popen`` whose ``communicate`` waits out the timeout."""

    def __init__(self, args, **kwargs) -> None:
        self.args = list(args)
        self.pid = 4242
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.killed = False
        _HangPwPlay.live.append(self)

    live: list[_HangPwPlay] = []

    def communicate(self, input=None, timeout=None):
        if timeout is None:
            threading.Event().wait()
            return (None, None)
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if self.killed or self.returncode is not None:
                return (None, None)
            time.sleep(0.01)
        raise subprocess.TimeoutExpired(self.args, timeout)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.wait()
        return False


@pytest.fixture(autouse=True)
def fake_sd(monkeypatch):
    sd = types.ModuleType("sounddevice")
    sd.plays: list = []
    sd.stops: list = []

    def play(*args, **kwargs) -> None:
        sd.plays.append((args, kwargs))

    def stop(ignore_errors: bool = True) -> None:
        sd.stops.append(bool(ignore_errors))

    sd.play = play
    sd.stop = stop
    sd.wait = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    cc._reset_shutdown_state_for_tests()
    yield sd
    cc._reset_shutdown_state_for_tests()


def _patch_exit(monkeypatch) -> list[int]:
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(int(code))

    monkeypatch.setattr(cc.os, "_exit", fake_exit)
    return codes


def test_hanging_uplink_close_exits_within_bound(monkeypatch, fake_sd):
    monkeypatch.setattr(cc, "UPLINK_CLOSE_TIMEOUT_S", 0.25)
    monkeypatch.setattr(cc, "EXIT_CLEANUP_TIMEOUT_S", 0.05)
    codes = _patch_exit(monkeypatch)
    stream = _HangStream()
    box: dict = {}

    def run() -> None:
        # A missing close helper blocks the way the pre-fix uplink close did.
        fn = getattr(cc, "_close_uplink_stream", None)
        if not callable(fn):
            threading.Event().wait()
            return
        box["v"] = fn(stream)

    worker = threading.Thread(target=run, name="uplink-close", daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=3.0)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), "uplink close did not return within 3s"
    assert box.get("v") is False
    assert elapsed < 1.5
    assert elapsed >= 0.2
    assert codes == [cc.WEDGED_EXIT_CODE]
    assert fake_sd.plays == []
    assert cc._uplink_stream_busy() is True


def test_uplink_close_returns_when_stream_stops(monkeypatch, fake_sd):
    codes = _patch_exit(monkeypatch)
    stream = _FastStream()
    started = time.monotonic()
    assert cc._close_uplink_stream(stream, timeout_s=1.0) is True
    assert time.monotonic() - started < 1.0
    assert stream.calls == ["stop", "close"]
    assert codes == []
    assert cc._uplink_stream_busy() is False
    assert fake_sd.plays == []


def test_pw_play_chime_timeout_kills_and_skips_portaudio(monkeypatch, fake_sd, capsys):
    monkeypatch.setattr(cc, "CHIME_PW_PLAY_TIMEOUT_S", 0.25)
    _HangPwPlay.live.clear()
    monkeypatch.setattr(subprocess, "Popen", _HangPwPlay)
    cc._set_uplink_stream_state("closed")

    def run() -> None:
        play = getattr(cc, "_play_local_shutdown", None)
        if not callable(play):
            threading.Event().wait()
            return
        play(kind="disconnect")

    worker = threading.Thread(target=run, name="chime-timeout", daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=3.0)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), "shutdown chime did not return within 3s"
    assert elapsed < 1.5
    assert elapsed >= 0.2
    assert _HangPwPlay.live, "pw-play was not started"
    proc = _HangPwPlay.live[-1]
    assert proc.args[0] == "pw-play"
    assert proc.killed is True
    assert fake_sd.plays == []
    assert "timed out" in capsys.readouterr().out


def test_missing_pw_play_is_logged_and_swallowed(monkeypatch, fake_sd, capsys):
    class _Missing:
        def __init__(self, *args, **kwargs) -> None:
            raise FileNotFoundError("pw-play")

    monkeypatch.setattr(subprocess, "Popen", _Missing)
    cc._set_uplink_stream_state("closed")
    cc._play_local_shutdown(kind="goodbye")
    assert fake_sd.plays == []
    assert "pw-play missing" in capsys.readouterr().out


def test_chime_not_played_while_uplink_stream_open_or_closing(monkeypatch, fake_sd, capsys):
    runs: list = []

    def _run(*args, **kwargs):
        runs.append((args, kwargs))
        raise AssertionError("pw-play ran while the uplink stream was busy")

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(subprocess, "Popen", _run)
    for state in ("open", "closing"):
        cc._set_uplink_stream_state(state)
        cc._play_local_shutdown(kind="disconnect")
        cc._play_local_shutdown(kind="goodbye")
    assert runs == []
    assert fake_sd.plays == []
    assert "deferring shutdown line" in capsys.readouterr().out


def test_timeouts_follow_env_over_constants(monkeypatch):
    monkeypatch.setattr(cc, "UPLINK_CLOSE_TIMEOUT_S", 5.0)
    monkeypatch.setattr(cc, "CHIME_PW_PLAY_TIMEOUT_S", 5.0)
    monkeypatch.setattr(cc, "RECONNECT_STEP_TIMEOUT_S", 20.0)
    monkeypatch.setenv("CRATE_UPLINK_CLOSE_TIMEOUT_S", "0.3")
    monkeypatch.setenv("CRATE_CHIME_TIMEOUT_S", "0.4")
    monkeypatch.setenv("CRATE_RECONNECT_STEP_TIMEOUT_S", "0.5")
    assert cc._uplink_close_timeout_s() == 0.3
    assert cc._chime_timeout_s() == 0.4
    assert cc._reconnect_step_timeout_s() == 0.5


def test_reconnect_watchdog_exits_when_step_is_stale(monkeypatch, fake_sd):
    monkeypatch.setattr(cc, "RECONNECT_STEP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(cc, "EXIT_CLEANUP_TIMEOUT_S", 0.05)
    codes = _patch_exit(monkeypatch)
    start = getattr(cc, "_start_reconnect_watchdog", None)
    stop = getattr(cc, "_stop_reconnect_watchdog", lambda: None)
    if not callable(start):
        time.sleep(0.3)
        assert codes == [getattr(cc, "WEDGED_EXIT_CODE", 1)]
        return
    start()
    try:
        with cc._RECONNECT_STEP_LOCK:
            cc._RECONNECT_STEP_AT = time.monotonic() - 1.0
        cc._RECONNECT_WATCH_ARMED.set()
        deadline = time.monotonic() + 2.0
        while not codes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert codes == [cc.WEDGED_EXIT_CODE]
    finally:
        stop()


def test_timeout_env_rejects_zero_negative_nan_and_inf(monkeypatch, capsys):
    """0, negatives, NaN, and inf fall back to the default and log a warning."""
    monkeypatch.setattr(cc, "UPLINK_CLOSE_TIMEOUT_S", 5.0)
    monkeypatch.setattr(cc, "CHIME_PW_PLAY_TIMEOUT_S", 5.0)
    monkeypatch.setattr(cc, "RECONNECT_STEP_TIMEOUT_S", 20.0)
    monkeypatch.setattr(cc, "TTS_PLAY_MARGIN_S", 5.0, raising=False)
    monkeypatch.setattr(cc, "MIN_GOOD_SESSION_S", 15.0, raising=False)
    warned = getattr(cc, "_BAD_TIMEOUTS", None)
    if isinstance(warned, set):
        warned.clear()
    cases = (
        ("CRATE_UPLINK_CLOSE_TIMEOUT_S", cc._uplink_close_timeout_s, 5.0),
        ("CRATE_CHIME_TIMEOUT_S", cc._chime_timeout_s, 5.0),
        ("CRATE_RECONNECT_STEP_TIMEOUT_S", cc._reconnect_step_timeout_s, 20.0),
        ("CRATE_TTS_PLAY_MARGIN_S", cc._tts_play_margin_s, 5.0),
        ("CRATE_MIN_GOOD_SESSION_S", cc._min_good_session_s, 15.0),
    )
    for raw in ("0", "-1", "nan", "inf", "-inf", "nope"):
        monkeypatch.setenv("CRATE_UPLINK_CLOSE_TIMEOUT_S", raw)
        assert cc._uplink_close_timeout_s() == 5.0
        out = capsys.readouterr().out
        assert "warning" in out
        assert "CRATE_UPLINK_CLOSE_TIMEOUT_S" in out
        assert raw in out
    for name, fn, default in cases[1:]:
        monkeypatch.setenv(name, "0")
        assert fn() == default
        out = capsys.readouterr().out
        assert name in out
        assert "warning" in out


def _pid_alive(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        state = text.rsplit(")", 1)[1].split()[0]
    except (IndexError, ValueError):
        return True
    return state != "Z"


def _install_wedged_pw_play(tmp_path, monkeypatch) -> Path:
    """Put a pw-play that sleeps, and records its pid plus a child, first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pw-play"
    # TTS temp wavs (crate-play-*) fork a grandchild and sleep. Canned
    # shutdown wavs exit at once so a disconnect chime cannot sit on the
    # reconnect path. The grandchild stays up if only the parent is killed.
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "args = sys.argv[1:]\n"
        "if not any('crate-play-' in arg for arg in args):\n"
        "    raise SystemExit(0)\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(3600)\n"
        "    raise SystemExit(0)\n"
        "path = os.environ.get('CRATE_TEST_PW_PLAY_PIDFILE', '')\n"
        "if path:\n"
        "    with open(path, 'w', encoding='utf-8') as handle:\n"
        "        handle.write(f'{os.getpid()} {child}\\n')\n"
        "        handle.flush()\n"
        "        os.fsync(handle.fileno())\n"
        "time.sleep(3600)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    pidfile = tmp_path / "pw-play.pids"
    monkeypatch.setenv("CRATE_TEST_PW_PLAY_PIDFILE", str(pidfile))
    monkeypatch.setenv("CRATE_PLAYBACK", "pw")
    monkeypatch.setenv("CRATE_BT_PRIME_MS", "0")
    monkeypatch.setenv(
        "PATH",
        str(bindir) + os.pathsep + os.environ.get("PATH", ""),
    )
    return pidfile


def _read_pw_pids(path: Path, timeout: float = 2.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            parts = path.read_text(encoding="utf-8").split()
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
        time.sleep(0.01)
    raise AssertionError(f"pw-play did not record pids in {path}")


def _wait_pids_dead(pids: tuple[int, int], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return True
        time.sleep(0.02)
    return False


def _kill_leftover(pids: tuple[int, ...] | None) -> None:
    if not pids:
        return
    for pid in pids:
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_wedged_pw_play_is_killed_after_play_bound(monkeypatch, tmp_path, fake_sd):
    """A pw-play that never exits is killed once duration + margin elapses."""
    pidfile = _install_wedged_pw_play(tmp_path, monkeypatch)
    monkeypatch.setenv("CRATE_TTS_PLAY_MARGIN_S", "0.35")
    pids: tuple[int, int] | None = None
    try:
        started = time.monotonic()
        finished = cc._play_pcm_f32([0.0] * 160, 16000)
        elapsed = time.monotonic() - started
        pids = _read_pw_pids(pidfile, timeout=0.2)
        assert finished is False
        assert not cc._AUDIO_LOCK.locked()
        assert elapsed < 2.0
        assert elapsed >= 0.3
        assert _wait_pids_dead(pids, 1.0), f"pw-play still running: {pids}"
        assert fake_sd.plays == []
    finally:
        _kill_leftover(pids)


def test_teardown_during_tts_lets_next_session_mic_start(monkeypatch, tmp_path, fake_sd):
    """Session teardown during TTS frees the lock so the next mic starts quickly."""
    import numpy as np
    from gourdsworth.net import PROTO, recv_msg, send_msg

    pidfile = _install_wedged_pw_play(tmp_path, monkeypatch)
    monkeypatch.setenv("CRATE_TTS_PLAY_MARGIN_S", "4")
    monkeypatch.setattr(cc, "RECONNECT_BACKOFF_MIN_S", 0.05)
    monkeypatch.setattr(cc, "RECONNECT_BACKOFF_JITTER_S", 0.0)
    monkeypatch.setattr(cc, "RECONNECT_BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(cc, "_stdin_ready", lambda timeout: time.sleep(min(0.05, float(timeout or 0))) or False)
    monkeypatch.setattr(cc, "_install_signal_handlers", lambda: [])
    monkeypatch.setattr(cc, "_setup_log", lambda path: None)

    fake_sd.default = types.SimpleNamespace(device=(0, 1))
    fake_sd.query_devices = lambda idx=None: {
        "name": "test",
        "default_samplerate": 16000.0,
    }
    fake_sd.check_input_settings = lambda **_kwargs: None
    recs: list[float] = []

    def rec(frames, samplerate, channels=1, dtype="int16"):
        recs.append(time.monotonic())
        return np.zeros((int(frames), int(channels)), dtype=dtype)

    fake_sd.rec = rec
    fake_sd.wait = lambda *args, **kwargs: None

    port_ready = threading.Event()
    mic_ready = threading.Event()
    playing = threading.Event()
    second_mic = threading.Event()
    stop = threading.Event()
    server_err: list[BaseException] = []
    port_box: dict = {}
    pids_box: dict = {}
    closed_at = {"t": 0.0}

    def desktop() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            srv.settimeout(0.2)
            port_box["port"] = int(srv.getsockname()[1])
            port_ready.set()
            conn = None
            while conn is None and not stop.is_set():
                try:
                    conn, _addr = srv.accept()
                except TimeoutError:
                    continue
            if conn is None:
                return
            conn.settimeout(2.0)
            buf = bytearray()
            header, _payload = recv_msg(conn, buf)
            if header.get("event") != "hello":
                raise AssertionError(header)
            send_msg(conn, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(conn, {"event": "ready"})
            if not mic_ready.wait(timeout=2.0):
                raise AssertionError("first session mic did not start")
            send_msg(
                conn,
                {
                    "event": "play",
                    "rate": 16000,
                    "channels": 1,
                    "format": "f32le",
                },
                b"\x00\x00\x00\x00" * 32,
            )
            parent, child = _read_pw_pids(pidfile, timeout=2.0)
            pids_box["pids"] = (parent, child)
            playing.set()
            closed_at["t"] = time.monotonic()
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            # Second session: hello + ready, then wait until its mic runs.
            conn2 = None
            while conn2 is None and not stop.is_set():
                try:
                    conn2, _addr = srv.accept()
                except TimeoutError:
                    continue
            if conn2 is None:
                return
            conn2.settimeout(2.0)
            buf2 = bytearray()
            recv_msg(conn2, buf2)
            send_msg(conn2, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(conn2, {"event": "ready"})
            if not second_mic.wait(timeout=3.0):
                raise AssertionError("second session mic did not start")
            conn2.close()
        except BaseException as exc:
            server_err.append(exc)
        finally:
            try:
                srv.close()
            except OSError:
                pass

    server = threading.Thread(target=desktop, name="tts-teardown-desktop", daemon=True)
    server.start()
    assert port_ready.wait(timeout=2.0)
    rc: dict = {}

    def run_client() -> None:
        try:
            rc["v"] = cc.main(
                [
                    "--continuous",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port_box["port"]),
                    "--no-camera",
                    "--log-file",
                    str(tmp_path / "crate-pi.log"),
                ]
            )
        except BaseException as exc:
            rc["err"] = exc

    client = threading.Thread(target=run_client, name="tts-teardown-client", daemon=True)
    try:
        client.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not recs:
            time.sleep(0.01)
        assert recs, "mic never started"
        mic_ready.set()
        assert playing.wait(timeout=3.0), f"pw-play did not start err={server_err!r}"
        pids = pids_box["pids"]
        assert _pid_alive(pids[0])
        deadline = time.monotonic() + 2.0
        recs_at_death: int | None = None
        while time.monotonic() < deadline:
            dead = not _pid_alive(pids[0]) and not _pid_alive(pids[1])
            if dead and recs_at_death is None:
                recs_at_death = len(recs)
            if recs_at_death is not None and len(recs) > recs_at_death:
                second_mic.set()
                break
            time.sleep(0.02)
        elapsed = time.monotonic() - closed_at["t"]
        assert second_mic.is_set(), (
            f"next mic did not start within 2s elapsed={elapsed:.2f} "
            f"recs={len(recs)} at_death={recs_at_death} "
            f"alive={[_pid_alive(pid) for pid in pids]} "
            f"err={server_err!r} rc={rc}"
        )
        assert elapsed < 2.0
        assert not cc._AUDIO_LOCK.locked()
        assert server_err == [], server_err
    finally:
        second_mic.set()
        stop.set()
        cc._SHUTDOWN.set()
        cancel = getattr(cc, "_cancel_inflight_playback", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        _kill_leftover(pids_box.get("pids"))
        client.join(timeout=3.0)
        server.join(timeout=2.0)
        cc._reset_shutdown_state_for_tests()
