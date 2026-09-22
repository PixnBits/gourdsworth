"""Pi client waits for desktop and announces with disconnect lines."""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest

_CLIENT = Path(__file__).resolve().parents[1] / "clients" / "pi" / "crate_client.py"


@pytest.fixture(scope="module")
def crate_client():
    spec = importlib.util.spec_from_file_location("crate_client_under_test", _CLIENT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wait_for_desktop_connects_when_server_appears(crate_client, monkeypatch):
    calls = {"n": 0, "plays": []}
    pair = socket.socketpair()

    def fake_connect(addr, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("refused")
        return pair[0]

    monkeypatch.setattr(crate_client.socket, "create_connection", fake_connect)
    quit_ev = threading.Event()

    def play(*, kind="disconnect"):
        calls["plays"].append(kind)

    sock = crate_client._wait_for_desktop(
        "127.0.0.1",
        8746,
        quit_ev,
        retry_s=0.05,
        announce_every_s=60.0,
        connect_timeout_s=0.05,
        play=play,
    )
    assert sock is pair[0]
    assert calls["n"] >= 3
    assert calls["plays"] == ["disconnect"]
    pair[0].close()
    pair[1].close()


def test_wait_for_desktop_stops_on_quit(crate_client, monkeypatch):
    monkeypatch.setattr(
        crate_client.socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")),
    )
    quit_ev = threading.Event()
    plays = []

    def play(*, kind="disconnect"):
        plays.append(kind)
        quit_ev.set()

    sock = crate_client._wait_for_desktop(
        "127.0.0.1",
        9,
        quit_ev,
        retry_s=0.05,
        announce_every_s=60.0,
        play=play,
    )
    assert sock is None
    assert plays == ["disconnect"]


def test_wait_skips_immediate_announce_when_asked(crate_client, monkeypatch):
    monkeypatch.setattr(
        crate_client.socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")),
    )
    quit_ev = threading.Event()
    plays = []

    def play(*, kind="disconnect"):
        plays.append(kind)

    def stopper():
        time.sleep(0.12)
        quit_ev.set()

    threading.Thread(target=stopper, daemon=True).start()
    sock = crate_client._wait_for_desktop(
        "127.0.0.1",
        9,
        quit_ev,
        retry_s=0.05,
        announce_every_s=60.0,
        announce_immediately=False,
        play=play,
    )
    assert sock is None
    assert plays == []
