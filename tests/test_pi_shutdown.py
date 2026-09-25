"""SIGTERM/SIGHUP shutdown: close PortAudio and the camera before exit."""

from __future__ import annotations

import os
import signal
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


class _FakeCap:
    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


@pytest.fixture
def fake_sd(monkeypatch):
    sd = types.ModuleType("sounddevice")
    sd.stops: list[bool] = []

    def stop(ignore_errors: bool = True) -> None:
        sd.stops.append(bool(ignore_errors))

    sd.stop = stop
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


@pytest.fixture(autouse=True)
def _isolate_shutdown_state():
    saved = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGHUP: signal.getsignal(signal.SIGHUP),
    }
    cc._reset_shutdown_state_for_tests()
    yield
    cc._reset_shutdown_state_for_tests()
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_on_shutdown_signal_raises_keyboardinterrupt():
    with pytest.raises(cc._ShutdownSignal) as excinfo:
        cc._on_shutdown_signal(signal.SIGTERM, None)
    assert isinstance(excinfo.value, KeyboardInterrupt)
    assert excinfo.value.signum == signal.SIGTERM
    assert cc._SHUTDOWN.is_set()
    assert cc._SHUTDOWN_SIGNAL == signal.SIGTERM


def test_cleanup_stops_audio_and_releases_caps_when_forced_or_busy(fake_sd):
    cap = _FakeCap()
    cc._ACTIVE_CAPS.add(cap)
    started = threading.Event()
    release = threading.Event()

    def holder() -> None:
        cc._CAMERA_LOCK.acquire()
        try:
            started.set()
            release.wait(timeout=2.0)
        finally:
            cc._CAMERA_LOCK.release()

    thread = threading.Thread(target=holder)
    thread.start()
    assert started.wait(timeout=1.0)
    try:
        assert cc._cleanup_devices(camera_wait_s=0.05) is True
        assert cap.released == 1
        assert fake_sd.stops == [True]
    finally:
        release.set()
        thread.join(timeout=1.0)

    cc._reset_shutdown_state_for_tests()
    fake_sd.stops.clear()
    forced = _FakeCap()
    cc._ACTIVE_CAPS.add(forced)
    assert cc._cleanup_devices(force=True, camera_wait_s=0.05) is True
    assert forced.released == 1
    assert fake_sd.stops == [True]


def test_cleanup_is_idempotent(fake_sd):
    assert cc._cleanup_devices() is True
    assert cc._cleanup_devices() is True
    assert fake_sd.stops == [True]


def test_install_signal_handlers_skips_ignored_sighup():
    previous_hup = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        installed = cc._install_signal_handlers()
        assert signal.SIGTERM in installed
        assert signal.SIGHUP not in installed
        assert signal.getsignal(signal.SIGTERM) is cc._on_shutdown_signal
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous_hup)

    installed = cc._install_signal_handlers()
    assert signal.SIGTERM in installed
    assert signal.SIGHUP in installed
    assert signal.getsignal(signal.SIGHUP) is cc._on_shutdown_signal


def test_real_sigterm_raises_keyboardinterrupt_and_cleanup_runs(fake_sd):
    cc._install_signal_handlers()
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(2.0)
        assert isinstance(excinfo.value, cc._ShutdownSignal)
        assert cc._SHUTDOWN.is_set()
    finally:
        assert cc._cleanup_devices() is True
    assert fake_sd.stops == [True]


def test_play_local_shutdown_skips_audio_when_signal_set(capsys):
    cc._SHUTDOWN.set()
    cc._play_local_shutdown(kind="goodbye")
    assert "skipping canned goodbye audio" in capsys.readouterr().out


def test_second_signal_cleans_up_and_force_kills(monkeypatch, fake_sd):
    cc._SHUTDOWN.set()
    signals: list[tuple] = []
    kills: list[tuple] = []

    def fake_signal(signum, handler):
        signals.append((signum, handler))

    def fake_kill(pid, signum):
        kills.append((pid, signum))

    monkeypatch.setattr(cc.signal, "signal", fake_signal)
    monkeypatch.setattr(cc.os, "kill", fake_kill)
    cc._on_shutdown_signal(signal.SIGTERM, None)
    assert cc._CLEANUP_DONE is True
    assert fake_sd.stops == [True]
    assert signals == [(signal.SIGTERM, signal.SIG_DFL)]
    assert kills == [(os.getpid(), signal.SIGTERM)]
