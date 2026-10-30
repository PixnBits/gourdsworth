"""Uplink teardown and reconnect steps must not block the crate client forever."""

from __future__ import annotations

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
    started = time.monotonic()
    finished = cc._close_uplink_stream(stream)
    elapsed = time.monotonic() - started
    assert finished is False
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
    started = time.monotonic()
    cc._play_local_shutdown(kind="disconnect")
    elapsed = time.monotonic() - started
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
    cc._start_reconnect_watchdog()
    try:
        with cc._RECONNECT_STEP_LOCK:
            cc._RECONNECT_STEP_AT = time.monotonic() - 1.0
        cc._RECONNECT_WATCH_ARMED.set()
        deadline = time.monotonic() + 2.0
        while not codes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert codes == [cc.WEDGED_EXIT_CODE]
    finally:
        cc._stop_reconnect_watchdog()
