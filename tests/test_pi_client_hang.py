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
    """stop() and close() block until ``release`` is set."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.release = threading.Event()

    def stop(self) -> None:
        self.calls.append("stop")
        self.release.wait()

    def close(self) -> None:
        self.calls.append("close")
        self.release.wait()


class _FastStream:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")


class _HangPwPlay:
    """Stand-in for ``subprocess.Popen`` that blocks until kill or ``timeout``."""

    def __init__(self, args, **kwargs) -> None:
        self.args = list(args)
        self.kwargs = dict(kwargs)
        # Above any real pid, so a group kill cannot signal another process.
        self.pid = 1 << 30
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.killed = False
        _HangPwPlay.live.append(self)

    live: list[_HangPwPlay] = []

    def _block(self, timeout):
        if timeout is None:
            while not self.killed and self.returncode is None:
                time.sleep(0.01)
            return
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if self.killed or self.returncode is not None:
                return
            time.sleep(0.01)
        raise subprocess.TimeoutExpired(self.args, timeout)

    def communicate(self, input=None, timeout=None):
        self._block(timeout)
        return (None, None)

    def terminate(self) -> None:
        self.killed = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self._block(timeout)
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
    monkeypatch.setattr(cc, "UPLINK_CLOSE_TIMEOUT_S", 0.25, raising=False)
    monkeypatch.setattr(cc, "EXIT_CLEANUP_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setenv("CRATE_UPLINK_CLOSE_TIMEOUT_S", "0.25")
    codes = _patch_exit(monkeypatch)
    stream = _HangStream()
    box: dict = {}

    def run() -> None:
        fn = getattr(cc, "_close_uplink_stream", None)
        if not callable(fn):
            return
        box["v"] = fn(stream)

    worker = threading.Thread(target=run, name="uplink-close", daemon=True)
    started = time.monotonic()
    try:
        worker.start()
        worker.join(timeout=3.0)
        elapsed = time.monotonic() - started
        assert not worker.is_alive(), "uplink close did not return within 3s"
        assert box.get("v") is False
        assert elapsed < 1.5
        assert elapsed >= 0.2
        assert codes == [getattr(cc, "WEDGED_EXIT_CODE", None)]
        assert fake_sd.plays == []
        busy = getattr(cc, "_uplink_stream_busy", lambda: None)
        assert busy() is True
    finally:
        stream.release.set()
        worker.join(timeout=1.0)


def test_uplink_close_returns_when_stream_stops(monkeypatch, fake_sd):
    codes = _patch_exit(monkeypatch)
    stream = _FastStream()
    box: dict = {}

    def run() -> None:
        fn = getattr(cc, "_close_uplink_stream", None)
        if not callable(fn):
            return
        box["v"] = fn(stream, timeout_s=1.0)

    worker = threading.Thread(target=run, name="uplink-close-fast", daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=3.0)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), "uplink close did not return within 3s"
    assert box.get("v") is True
    assert elapsed < 1.0
    assert stream.calls == ["stop", "close"]
    assert codes == []
    busy = getattr(cc, "_uplink_stream_busy", lambda: True)
    assert busy() is False
    assert fake_sd.plays == []


def test_pw_play_chime_timeout_kills_and_skips_portaudio(monkeypatch, fake_sd, capsys):
    monkeypatch.setattr(cc, "CHIME_PW_PLAY_TIMEOUT_S", 5.0, raising=False)
    monkeypatch.setenv("CRATE_CHIME_TIMEOUT_S", "0.25")
    _HangPwPlay.live.clear()
    monkeypatch.setattr(subprocess, "Popen", _HangPwPlay)
    setter = getattr(cc, "_set_uplink_stream_state", None)
    if callable(setter):
        setter("closed")
    box: dict = {}

    def run() -> None:
        play = getattr(cc, "_play_local_shutdown", None)
        if not callable(play):
            return
        play(kind="disconnect")
        box["done"] = True

    worker = threading.Thread(target=run, name="chime-timeout", daemon=True)
    started = time.monotonic()
    try:
        worker.start()
        worker.join(timeout=3.0)
        elapsed = time.monotonic() - started
        assert not worker.is_alive(), "shutdown chime did not return within 3s"
        assert box.get("done") is True
        assert elapsed < 1.5
        assert elapsed >= 0.2
        assert _HangPwPlay.live, "pw-play was not started"
        proc = _HangPwPlay.live[-1]
        assert proc.args[0] == "pw-play"
        assert proc.kwargs.get("start_new_session") is True
        assert proc.killed is True
        assert fake_sd.plays == []
        assert "timed out" in capsys.readouterr().out
        assert not getattr(cc, "_PW_PLAY_GROUPS", set())
    finally:
        for proc in list(_HangPwPlay.live):
            proc.kill()
        worker.join(timeout=1.0)


def test_missing_pw_play_is_logged_and_swallowed(monkeypatch, fake_sd, capsys):
    class _Missing:
        def __init__(self, *args, **kwargs) -> None:
            raise FileNotFoundError("pw-play")

    monkeypatch.setattr(subprocess, "Popen", _Missing)
    setter = getattr(cc, "_set_uplink_stream_state", None)
    if callable(setter):
        setter("closed")
    box: dict = {}

    def run() -> None:
        play = getattr(cc, "_play_local_shutdown", None)
        if not callable(play):
            return
        play(kind="goodbye")
        box["done"] = True

    worker = threading.Thread(target=run, name="chime-missing", daemon=True)
    worker.start()
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "missing pw-play hung the shutdown chime"
    assert box.get("done") is True
    assert fake_sd.plays == []
    assert "pw-play missing" in capsys.readouterr().out


def test_chime_not_played_while_uplink_stream_open_or_closing(monkeypatch, fake_sd, capsys):
    runs: list = []

    def _run(*args, **kwargs):
        runs.append((args, kwargs))
        raise AssertionError("pw-play ran while the uplink stream was busy")

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(subprocess, "Popen", _run)
    setter = getattr(cc, "_set_uplink_stream_state", None)
    play = getattr(cc, "_play_local_shutdown", None)

    def drive() -> None:
        if not callable(play):
            return
        for state in ("open", "closing"):
            if callable(setter):
                setter(state)
            play(kind="disconnect")
            play(kind="goodbye")

    worker = threading.Thread(target=drive, name="chime-deferred", daemon=True)
    worker.start()
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "shutdown chime did not return within 3s"
    assert runs == []
    assert fake_sd.plays == []
    assert "deferring shutdown line" in capsys.readouterr().out


def test_timeouts_follow_env_over_constants(monkeypatch):
    monkeypatch.setattr(cc, "UPLINK_CLOSE_TIMEOUT_S", 5.0, raising=False)
    monkeypatch.setattr(cc, "CHIME_PW_PLAY_TIMEOUT_S", 5.0, raising=False)
    monkeypatch.setattr(cc, "RECONNECT_STEP_TIMEOUT_S", 20.0, raising=False)
    monkeypatch.setenv("CRATE_UPLINK_CLOSE_TIMEOUT_S", "0.3")
    monkeypatch.setenv("CRATE_CHIME_TIMEOUT_S", "0.4")
    monkeypatch.setenv("CRATE_RECONNECT_STEP_TIMEOUT_S", "0.5")
    uplink = getattr(cc, "_uplink_close_timeout_s", lambda: None)
    chime = getattr(cc, "_chime_timeout_s", lambda: None)
    step = getattr(cc, "_reconnect_step_timeout_s", lambda: None)
    assert uplink() == 0.3
    assert chime() == 0.4
    assert step() == 0.5


def test_reconnect_watchdog_exits_when_step_is_stale(monkeypatch, fake_sd):
    monkeypatch.setattr(cc, "RECONNECT_STEP_TIMEOUT_S", 0.2, raising=False)
    monkeypatch.setattr(cc, "EXIT_CLEANUP_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setenv("CRATE_RECONNECT_STEP_TIMEOUT_S", "0.2")
    codes = _patch_exit(monkeypatch)
    start = getattr(cc, "_start_reconnect_watchdog", None)
    stop = getattr(cc, "_stop_reconnect_watchdog", None)
    release = threading.Event()

    def run() -> None:
        if not callable(start):
            release.wait(timeout=5.0)
            return
        start()
        lock = getattr(cc, "_RECONNECT_STEP_LOCK", None)
        if lock is not None:
            with lock:
                cc._RECONNECT_STEP_AT = time.monotonic() - 1.0
        elif hasattr(cc, "_RECONNECT_STEP_AT"):
            cc._RECONNECT_STEP_AT = time.monotonic() - 1.0
        armed = getattr(cc, "_RECONNECT_WATCH_ARMED", None)
        if armed is not None and hasattr(armed, "set"):
            armed.set()
        else:
            release.wait(timeout=5.0)

    worker = threading.Thread(target=run, name="reconnect-watch", daemon=True)
    try:
        worker.start()
        deadline = time.monotonic() + 2.0
        while not codes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert codes == [getattr(cc, "WEDGED_EXIT_CODE", None)]
    finally:
        release.set()
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        worker.join(timeout=1.0)


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


def _install_wedged_pw_play(tmp_path, monkeypatch, request) -> Path:
    """Put a pw-play first on PATH and reap every pid it records.

    TTS wavs (``crate-play-*``) fork a grandchild and sleep. Chimes record
    their pid and exit unless ``CRATE_TEST_PW_PLAY_WEDGE`` is ``chime`` or
    ``all``, in which case they fork and sleep too. The finalizer kills every
    recorded pid and process group, including when the test fails.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pw-play"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "args = sys.argv[1:]\n"
        "is_tts = any('crate-play-' in arg for arg in args)\n"
        "wedge = os.environ.get('CRATE_TEST_PW_PLAY_WEDGE', '')\n"
        "fork = is_tts or wedge == 'all' or (wedge == 'chime' and not is_tts)\n"
        "child = 0\n"
        "if fork:\n"
        "    child = os.fork()\n"
        "    if child == 0:\n"
        "        time.sleep(3600)\n"
        "        raise SystemExit(0)\n"
        "path = os.environ.get('CRATE_TEST_PW_PLAY_PIDFILE', '')\n"
        "if path:\n"
        "    line = f'{os.getpid()} {os.getpgrp()} {child}\\n'.encode()\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)\n"
        "    try:\n"
        "        os.write(fd, line)\n"
        "        os.fsync(fd)\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "if fork:\n"
        "    time.sleep(3600)\n"
        "raise SystemExit(0)\n",
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

    def _reap() -> None:
        _kill_recorded(pidfile)

    request.addfinalizer(_reap)
    return pidfile


def _recorded_pw(path: Path) -> list[tuple[int, int, int]]:
    """Lines written by the fake: ``pid pgid child`` (child is 0 when none)."""
    if not path.is_file():
        return []
    rows: list[tuple[int, int, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
            child = int(parts[2]) if len(parts) >= 3 else 0
        except ValueError:
            continue
        rows.append((pid, pgid, child))
    return rows


def _read_pw_pids(path: Path, timeout: float = 2.0) -> tuple[int, int]:
    """Return the first forked pw-play's ``(parent, child)``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pid, _pgid, child in _recorded_pw(path):
            if child > 0:
                return pid, child
        time.sleep(0.01)
    raise AssertionError(f"pw-play did not record pids in {path}")


def _wait_pids_dead(pids: tuple[int, ...] | list[int], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return True
        time.sleep(0.02)
    return False


def _assert_pw_procs_dead(path: Path, timeout: float = 2.0) -> None:
    """Parent and any forked grandchild are gone. Zombies count as dead."""
    pids: list[int] = []
    for pid, _pgid, child in _recorded_pw(path):
        if pid > 1:
            pids.append(pid)
        if child > 1:
            pids.append(child)
    assert pids, f"pw-play did not record any pid in {path}"
    assert _wait_pids_dead(pids, timeout), f"fake pw-play still alive: {pids}"


def _kill_recorded(path: Path) -> None:
    """SIGKILL every pid and foreign process group the fake recorded."""
    me_pid = os.getpid()
    me_pgid = os.getpgrp()
    for pid, pgid, child in _recorded_pw(path):
        for target in (child, pid):
            if target <= 1 or target == me_pid:
                continue
            try:
                os.kill(int(target), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if pgid > 1 and pgid != me_pgid:
            try:
                os.killpg(int(pgid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass


def test_wedged_pw_play_is_killed_after_play_bound(monkeypatch, tmp_path, fake_sd, request):
    """A pw-play that never exits is killed once duration + margin elapses."""
    pidfile = _install_wedged_pw_play(tmp_path, monkeypatch, request)
    monkeypatch.setenv("CRATE_TTS_PLAY_MARGIN_S", "0.35")
    box: dict = {}

    def run() -> None:
        try:
            box["finished"] = cc._play_pcm_f32([0.0] * 160, 16000)
        except BaseException as exc:
            box["err"] = exc

    worker = threading.Thread(target=run, name="wedged-pw-play", daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=10.0)
    elapsed = time.monotonic() - started
    try:
        assert not worker.is_alive(), "pw-play playback did not return within 10s"
        assert "err" not in box, box.get("err")
        assert box.get("finished") is False
        assert not cc._AUDIO_LOCK.locked()
        assert elapsed < 2.0
        assert elapsed >= 0.3
        assert fake_sd.plays == []
        _assert_pw_procs_dead(pidfile, timeout=2.0)
        assert not getattr(cc, "_PW_PLAY_GROUPS", set())
    finally:
        _kill_recorded(pidfile)
        worker.join(timeout=1.0)


def test_teardown_during_tts_lets_next_session_mic_start(monkeypatch, tmp_path, fake_sd, request):
    """Session teardown during TTS frees the lock so the next mic starts quickly."""
    import numpy as np
    from gourdsworth.net import PROTO, recv_msg, send_msg

    pidfile = _install_wedged_pw_play(tmp_path, monkeypatch, request)
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
        _assert_pw_procs_dead(pidfile, timeout=2.0)
        assert not getattr(cc, "_PW_PLAY_GROUPS", set())
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
        _kill_recorded(pidfile)
        client.join(timeout=3.0)
        server.join(timeout=2.0)
        cc._reset_shutdown_state_for_tests()


def test_chime_pw_play_timeout_kills_grandchild(monkeypatch, tmp_path, fake_sd, request, capsys):
    """A chime pw-play that forks and sleeps is killed, grandchild included."""
    pidfile = _install_wedged_pw_play(tmp_path, monkeypatch, request)
    timeout_s = 0.35
    monkeypatch.setenv("CRATE_CHIME_TIMEOUT_S", str(timeout_s))
    monkeypatch.setenv("CRATE_TEST_PW_PLAY_WEDGE", "chime")
    monkeypatch.setattr(cc, "CHIME_PW_PLAY_TIMEOUT_S", 5.0, raising=False)
    grace = float(getattr(cc, "PW_PLAY_KILL_GRACE_S", 0.25))
    wav = tmp_path / "disconnect_1.wav"
    wav.write_bytes(b"RIFF")
    box: dict = {}

    def run() -> None:
        fn = getattr(cc, "_pw_play_chime", None)
        if not callable(fn):
            box["missing"] = True
            return
        fn(wav)
        box["done"] = True

    worker = threading.Thread(target=run, name="chime-grandchild", daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=10.0)
    elapsed = time.monotonic() - started
    try:
        assert not worker.is_alive(), "chime pw-play did not return within 10s"
        assert not box.get("missing"), "chime playback has no pw-play timeout"
        assert box.get("done") is True
        assert elapsed >= timeout_s * 0.5
        assert elapsed <= timeout_s + grace + 0.5
        assert fake_sd.plays == []
        assert "timed out" in capsys.readouterr().out
        rows = _recorded_pw(pidfile)
        assert any(child > 0 for _pid, _pgid, child in rows), rows
        _assert_pw_procs_dead(pidfile, timeout=2.0)
        assert not getattr(cc, "_PW_PLAY_GROUPS", set())
    finally:
        _kill_recorded(pidfile)
        worker.join(timeout=1.0)


def test_cleanup_reaps_live_chime_group(monkeypatch, tmp_path, fake_sd, request):
    """Session cleanup kills a chime group that has not hit its timeout yet."""
    pidfile = _install_wedged_pw_play(tmp_path, monkeypatch, request)
    monkeypatch.setenv("CRATE_CHIME_TIMEOUT_S", "30")
    monkeypatch.setenv("CRATE_TEST_PW_PLAY_WEDGE", "chime")
    wav = tmp_path / "disconnect_2.wav"
    wav.write_bytes(b"RIFF")
    box: dict = {}

    def run() -> None:
        fn = getattr(cc, "_pw_play_chime", None)
        if not callable(fn):
            box["missing"] = True
            return
        fn(wav)
        box["done"] = True

    worker = threading.Thread(target=run, name="chime-cleanup", daemon=True)
    worker.start()
    try:
        parent, child = _read_pw_pids(pidfile, timeout=2.0)
        assert _pid_alive(parent) and _pid_alive(child)
        cleanup = getattr(cc, "_cleanup_devices", None)
        assert callable(cleanup)
        cleanup()
        worker.join(timeout=3.0)
        assert not worker.is_alive(), "cleanup did not unblock the chime"
        assert box.get("done") is True
        assert not box.get("missing")
        _assert_pw_procs_dead(pidfile, timeout=2.0)
        assert not getattr(cc, "_PW_PLAY_GROUPS", set())
        assert fake_sd.plays == []
    finally:
        _kill_recorded(pidfile)
        cancel = getattr(cc, "_cancel_inflight_playback", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        worker.join(timeout=1.0)
