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


def test_wait_ready_returns_false_promptly_when_conn_closed(crate_client):
    crate_client._reset_shutdown_state_for_tests()
    a, b = socket.socketpair()
    try:
        conn = crate_client.CrateConnection(b)
        try:
            ready = threading.Event()

            def closer() -> None:
                time.sleep(0.05)
                conn.close()

            threading.Thread(target=closer, daemon=True).start()
            t0 = time.monotonic()
            assert crate_client._wait_ready(ready, conn, 5.0) is False
            assert (time.monotonic() - t0) < 1.5
        finally:
            conn.close()
    finally:
        a.close()
        b.close()


def test_wait_trigger_returns_disconnect_when_conn_closed(crate_client, monkeypatch):
    crate_client._reset_shutdown_state_for_tests()
    monkeypatch.setattr(crate_client, "_stdin_ready", lambda timeout: False)
    a, b = socket.socketpair()
    stop = threading.Event()
    try:
        conn = crate_client.CrateConnection(b)
        try:

            def closer() -> None:
                time.sleep(0.05)
                conn.close()

            def hang_guard() -> None:
                time.sleep(2.0)
                stop.set()

            threading.Thread(target=closer, daemon=True).start()
            threading.Thread(target=hang_guard, daemon=True).start()
            t0 = time.monotonic()
            assert crate_client._wait_trigger(None, stop, conn) == "disconnect"
            assert (time.monotonic() - t0) < 1.5
        finally:
            conn.close()
    finally:
        a.close()
        b.close()


def test_client_reconnects_after_desktop_drops(crate_client, monkeypatch, tmp_path):
    """Fake desktop accepts, sends ready, drops; the client loop accepts again."""
    from gourdsworth.net import PROTO, send_msg

    crate_client._reset_shutdown_state_for_tests()
    monkeypatch.setattr(crate_client, "_play_local_shutdown", lambda **k: None)
    monkeypatch.setattr(crate_client, "_stdin_ready", lambda timeout: False)
    monkeypatch.setattr(crate_client, "_setup_log", lambda path: None)
    monkeypatch.setattr(crate_client, "_install_signal_handlers", lambda: [])
    monkeypatch.setattr(crate_client, "_cleanup_devices", lambda **k: True)
    monkeypatch.setattr(crate_client, "_apply_audio_devices", lambda *a, **k: None)

    orig_wait_trigger = crate_client._wait_trigger
    second_accepted = threading.Event()

    def wait_trigger(gpio, stop, conn=None):
        # Second TCP accept is the reconnect; don't wait for Enter.
        if second_accepted.is_set():
            return "quit"
        return orig_wait_trigger(gpio, stop, conn)

    monkeypatch.setattr(crate_client, "_wait_trigger", wait_trigger)

    port_ready = threading.Event()
    done = threading.Event()
    port_box: dict = {}
    accepted: list = []
    server_err: list[BaseException] = []

    def fake_desktop() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            srv.settimeout(5.0)
            port_box["port"] = int(srv.getsockname()[1])
            port_ready.set()
            c1, _addr1 = srv.accept()
            accepted.append(c1)
            send_msg(c1, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(c1, {"event": "ready"})
            time.sleep(0.2)
            try:
                c1.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c1.close()
            c2, _addr2 = srv.accept()
            accepted.append(c2)
            second_accepted.set()
            send_msg(c2, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(c2, {"event": "ready"})
            done.wait(timeout=5.0)
            try:
                c2.close()
            except OSError:
                pass
        except BaseException as exc:
            server_err.append(exc)
        finally:
            try:
                srv.close()
            except OSError:
                pass

    server = threading.Thread(target=fake_desktop, name="crate-fake-desktop", daemon=True)
    server.start()
    assert port_ready.wait(timeout=2.0)
    port = int(port_box["port"])
    rc = {"v": None}

    def run_client() -> None:
        rc["v"] = crate_client.main(
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-camera",
                "--no-mic",
                "--log-file",
                str(tmp_path / "crate-pi.log"),
            ]
        )

    client = threading.Thread(target=run_client, name="crate-client-reconnect", daemon=True)
    try:
        client.start()
        assert second_accepted.wait(timeout=5.0), (
            f"client did not reconnect: {server_err!r} rc={rc} accepts={len(accepted)}"
        )
        assert len(accepted) >= 2
        client.join(timeout=5.0)
        assert not client.is_alive(), f"client still running rc={rc} err={server_err!r}"
        assert rc["v"] == 0
        done.set()
    finally:
        done.set()
        crate_client._SHUTDOWN.set()
        for sock in accepted:
            try:
                sock.close()
            except OSError:
                pass
        client.join(timeout=2.0)
        server.join(timeout=2.0)
        crate_client._reset_shutdown_state_for_tests()
        assert not client.is_alive()
        assert not server.is_alive(), f"fake desktop still running: {server_err!r}"
