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


def _stub_client_side(crate_client, monkeypatch):
    crate_client._reset_shutdown_state_for_tests()
    monkeypatch.setattr(crate_client, "_play_local_shutdown", lambda **k: None)
    monkeypatch.setattr(crate_client, "_stdin_ready", lambda timeout: False)
    monkeypatch.setattr(crate_client, "_setup_log", lambda path: None)
    monkeypatch.setattr(crate_client, "_install_signal_handlers", lambda: [])
    monkeypatch.setattr(crate_client, "_cleanup_devices", lambda **k: True)
    monkeypatch.setattr(crate_client, "_apply_audio_devices", lambda *a, **k: None)


def test_backoff_superseded_returns_when_quit(crate_client, monkeypatch):
    """Quit during the supersede wait returns before the full backoff."""
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_S", 5.0)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_JITTER_S", 0.0)
    monkeypatch.setattr(crate_client, "_stdin_ready", lambda timeout: False)
    quit_ev = threading.Event()

    def quitter() -> None:
        time.sleep(0.05)
        quit_ev.set()

    threading.Thread(target=quitter, daemon=True).start()
    t0 = time.monotonic()
    assert crate_client._backoff_superseded(quit_ev) is True
    assert (time.monotonic() - t0) < 1.0


def test_superseded_backs_off_before_reconnect(crate_client, monkeypatch, tmp_path):
    """A superseded event delays the next TCP connect by the backoff."""
    from gourdsworth.net import PROTO, send_msg

    _stub_client_side(crate_client, monkeypatch)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_S", 0.45)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_JITTER_S", 0.0)
    started = threading.Event()
    real_backoff = crate_client._backoff_superseded

    def wrapped(quit_ev):
        started.set()
        return real_backoff(quit_ev)

    monkeypatch.setattr(crate_client, "_backoff_superseded", wrapped)
    second_accepted = threading.Event()

    def wait_trigger(gpio, stop, conn=None):
        if second_accepted.is_set():
            return "quit"
        return "disconnect"

    monkeypatch.setattr(crate_client, "_wait_trigger", wait_trigger)

    port_ready = threading.Event()
    done = threading.Event()
    port_box: dict = {}
    accepted: list = []
    times: list[float] = []
    server_err: list[BaseException] = []

    def fake_desktop() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            srv.settimeout(3.0)
            port_box["port"] = int(srv.getsockname()[1])
            port_ready.set()
            c1, _addr = srv.accept()
            accepted.append(c1)
            times.append(time.monotonic())
            send_msg(c1, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(c1, {"event": "superseded"})
            try:
                c1.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c1.close()
            c2, _addr2 = srv.accept()
            accepted.append(c2)
            times.append(time.monotonic())
            second_accepted.set()
            send_msg(c2, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(c2, {"event": "ready"})
            done.wait(timeout=3.0)
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

    server = threading.Thread(target=fake_desktop, name="crate-supersede-desktop", daemon=True)
    server.start()
    assert port_ready.wait(timeout=2.0), server_err
    rc = {"v": None}

    def run_client() -> None:
        rc["v"] = crate_client.main(
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(port_box["port"]),
                "--no-camera",
                "--no-mic",
                "--log-file",
                str(tmp_path / "crate-supersede.log"),
            ]
        )

    client = threading.Thread(target=run_client, name="crate-supersede-client", daemon=True)
    try:
        client.start()
        assert second_accepted.wait(timeout=3.0), (
            f"client did not reconnect after backoff: {server_err!r} rc={rc} "
            f"accepts={len(accepted)} started={started.is_set()}"
        )
        assert started.is_set()
        assert len(times) >= 2
        assert (times[1] - times[0]) >= 0.35
        client.join(timeout=3.0)
        assert not client.is_alive(), f"client still running rc={rc} err={server_err!r}"
        assert rc["v"] == 0
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


def test_shutdown_interrupts_superseded_backoff(crate_client, monkeypatch, tmp_path):
    """Shutdown during the supersede backoff does not open another connection."""
    from gourdsworth.net import PROTO, send_msg

    _stub_client_side(crate_client, monkeypatch)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_S", 2.0)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_JITTER_S", 0.0)
    started = threading.Event()
    real_backoff = crate_client._backoff_superseded

    def wrapped(quit_ev):
        started.set()
        return real_backoff(quit_ev)

    monkeypatch.setattr(crate_client, "_backoff_superseded", wrapped)

    port_ready = threading.Event()
    port_box: dict = {}
    accepted: list = []
    server_err: list[BaseException] = []
    release = threading.Event()

    def fake_desktop() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            srv.settimeout(0.4)
            port_box["port"] = int(srv.getsockname()[1])
            port_ready.set()
            c1, _addr = srv.accept()
            accepted.append(c1)
            send_msg(c1, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(c1, {"event": "superseded"})
            try:
                c1.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c1.close()
            # A second accept means the client did not stay in backoff.
            while not release.is_set():
                try:
                    extra, _addr2 = srv.accept()
                except TimeoutError:
                    continue
                accepted.append(extra)
                try:
                    extra.close()
                except OSError:
                    pass
                break
        except BaseException as exc:
            server_err.append(exc)
        finally:
            try:
                srv.close()
            except OSError:
                pass

    server = threading.Thread(target=fake_desktop, name="crate-supersede-quit", daemon=True)
    server.start()
    assert port_ready.wait(timeout=2.0), server_err
    rc = {"v": None}

    def run_client() -> None:
        rc["v"] = crate_client.main(
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(port_box["port"]),
                "--no-camera",
                "--no-mic",
                "--log-file",
                str(tmp_path / "crate-supersede-quit.log"),
            ]
        )

    client = threading.Thread(target=run_client, name="crate-supersede-quit-client", daemon=True)
    try:
        client.start()
        assert started.wait(timeout=3.0), (
            f"backoff did not start: {server_err!r} accepts={len(accepted)}"
        )
        crate_client._SHUTDOWN.set()
        client.join(timeout=2.0)
        assert not client.is_alive(), f"quit did not interrupt backoff rc={rc} err={server_err!r}"
        assert rc["v"] == 0
        assert len(accepted) == 1, accepted
    finally:
        release.set()
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


def _bind_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    srv.settimeout(2.0)
    return srv


def _send_busy_and_close(sock: socket.socket) -> None:
    from gourdsworth.net import send_msg

    sock.settimeout(1.0)
    try:
        sock.recv(256)
    except OSError:
        pass
    send_msg(sock, {"event": "error", "message": "busy"})
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def test_busy_backs_off_and_announces_once_per_outage(crate_client, monkeypatch, tmp_path):
    """busy waits out the supersede backoff and speaks the disconnect line once."""
    from gourdsworth.net import PROTO, send_msg

    _stub_client_side(crate_client, monkeypatch)
    plays: list[float] = []

    def play(**_k):
        plays.append(time.monotonic())

    monkeypatch.setattr(crate_client, "_play_local_shutdown", play)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_S", 0.40)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_JITTER_S", 0.0)
    monkeypatch.setattr(crate_client, "DISCONNECT_ANNOUNCE_EVERY_S", 30.0)
    # A ready-then-immediate-disconnect is unserved and waits the minimum.
    # This test is about the busy chime, so keep that pause short.
    monkeypatch.setattr(crate_client, "RECONNECT_BACKOFF_MIN_S", 0.05)
    monkeypatch.setattr(crate_client, "RECONNECT_BACKOFF_JITTER_S", 0.0)
    backoffs = {"n": 0}
    second_backoff = threading.Event()
    real_backoff = crate_client._backoff_superseded

    def wrapped(quit_ev):
        backoffs["n"] += 1
        if backoffs["n"] >= 2:
            second_backoff.set()
        return real_backoff(quit_ev)

    monkeypatch.setattr(crate_client, "_backoff_superseded", wrapped)
    ready_visits = {"n": 0}

    def wait_trigger(gpio, stop, conn=None):
        ready_visits["n"] += 1
        if ready_visits["n"] >= 2:
            return "quit"
        return "disconnect"

    monkeypatch.setattr(crate_client, "_wait_trigger", wait_trigger)

    times: list[float] = []
    accepted: list = []
    server_err: list[BaseException] = []
    port_ready = threading.Event()
    port_box: dict = {}
    release = threading.Event()

    def fake_desktop() -> None:
        srv = _bind_server()
        try:
            port_box["port"] = int(srv.getsockname()[1])
            port_ready.set()
            for _ in range(2):
                conn, _addr = srv.accept()
                accepted.append(conn)
                times.append(time.monotonic())
                _send_busy_and_close(conn)
            conn, _addr = srv.accept()
            accepted.append(conn)
            try:
                conn.recv(256)
            except OSError:
                pass
            send_msg(conn, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(conn, {"event": "ready"})
            # Client returns from the served session, then comes back once more.
            conn2, _addr2 = srv.accept()
            accepted.append(conn2)
            try:
                conn2.recv(256)
            except OSError:
                pass
            send_msg(conn2, {"event": "hello", "role": "desktop", "proto": PROTO})
            send_msg(conn2, {"event": "ready"})
            release.wait(timeout=3.0)
        except BaseException as exc:
            server_err.append(exc)
        finally:
            try:
                srv.close()
            except OSError:
                pass
            for sock in accepted:
                try:
                    sock.close()
                except OSError:
                    pass

    server = threading.Thread(target=fake_desktop, name="crate-busy-desktop", daemon=True)
    server.start()
    assert port_ready.wait(timeout=2.0), server_err
    rc = {"v": None}

    def run_client() -> None:
        rc["v"] = crate_client.main(
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(port_box["port"]),
                "--no-camera",
                "--no-mic",
                "--log-file",
                str(tmp_path / "crate-busy.log"),
            ]
        )

    client = threading.Thread(target=run_client, name="crate-busy-client", daemon=True)
    try:
        client.start()
        assert second_backoff.wait(timeout=3.0), (
            f"second busy did not back off: plays={len(plays)} accepts={len(accepted)} "
            f"err={server_err!r}"
        )
        assert len(plays) == 1, plays
        assert len(times) >= 2
        assert (times[1] - times[0]) >= 0.30
        client.join(timeout=3.0)
        assert not client.is_alive(), f"client still running rc={rc} err={server_err!r}"
        assert rc["v"] == 0
        # Two busies were one outage. The served session resets it, so the
        # drop after ready speaks again. A third busy would be a third line.
        assert len(plays) == 2, plays
        assert backoffs["n"] >= 2
    finally:
        release.set()
        crate_client._SHUTDOWN.set()
        client.join(timeout=2.0)
        server.join(timeout=2.0)
        crate_client._reset_shutdown_state_for_tests()
        assert not client.is_alive()
        assert not server.is_alive(), f"fake desktop still running: {server_err!r}"


def test_shutdown_interrupts_busy_backoff(crate_client, monkeypatch, tmp_path):
    """Quit or shutdown during a busy backoff does not open another connection."""
    _stub_client_side(crate_client, monkeypatch)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_S", 2.0)
    monkeypatch.setattr(crate_client, "SUPERSEDED_BACKOFF_JITTER_S", 0.0)
    started = threading.Event()
    real_backoff = crate_client._backoff_superseded

    def wrapped(quit_ev):
        started.set()
        return real_backoff(quit_ev)

    monkeypatch.setattr(crate_client, "_backoff_superseded", wrapped)
    accepted: list = []
    server_err: list[BaseException] = []
    port_ready = threading.Event()
    port_box: dict = {}
    release = threading.Event()

    def fake_desktop() -> None:
        srv = _bind_server()
        srv.settimeout(0.4)
        try:
            port_box["port"] = int(srv.getsockname()[1])
            port_ready.set()
            conn, _addr = srv.accept()
            accepted.append(conn)
            _send_busy_and_close(conn)
            while not release.is_set():
                try:
                    extra, _addr2 = srv.accept()
                except TimeoutError:
                    continue
                accepted.append(extra)
                try:
                    extra.close()
                except OSError:
                    pass
                break
        except BaseException as exc:
            server_err.append(exc)
        finally:
            try:
                srv.close()
            except OSError:
                pass

    server = threading.Thread(target=fake_desktop, name="crate-busy-quit", daemon=True)
    server.start()
    assert port_ready.wait(timeout=2.0), server_err
    rc = {"v": None}

    def run_client() -> None:
        rc["v"] = crate_client.main(
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(port_box["port"]),
                "--no-camera",
                "--no-mic",
                "--log-file",
                str(tmp_path / "crate-busy-quit.log"),
            ]
        )

    client = threading.Thread(target=run_client, name="crate-busy-quit-client", daemon=True)
    try:
        client.start()
        assert started.wait(timeout=3.0), (
            f"busy backoff did not start: {server_err!r} accepts={len(accepted)}"
        )
        crate_client._SHUTDOWN.set()
        client.join(timeout=2.0)
        assert not client.is_alive(), f"shutdown did not interrupt busy backoff rc={rc}"
        assert rc["v"] == 0
        assert len(accepted) == 1, accepted
    finally:
        release.set()
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


def _virtual_reconnect_waits(crate_client, monkeypatch, waits: list[float], stop_when):
    """Replace the reconnect sleep with an instant wait that records delays.

    ``stop_when(waits)`` true ends the client. Missing on the old client, so
    a hot loop is left to the caller's safety cap instead of hanging here.
    """
    monkeypatch.setattr(crate_client, "RECONNECT_BACKOFF_JITTER_S", 0.0, raising=False)

    def virtual(quit_ev, delay):
        del quit_ev
        waits.append(float(delay))
        return bool(stop_when(waits))

    if hasattr(crate_client, "_backoff_wait"):
        monkeypatch.setattr(crate_client, "_backoff_wait", virtual)
        return True
    return False


def _drive_pi_main(crate_client, on_accept, tmp_path, *, join_s=3.0, safety=12):
    """Run the real Pi main loop against a 127.0.0.1 ephemeral desktop.

    ``on_accept(conn, n, held)`` sees each accepted socket, n starting at 1.
    The safety cap stops a reconnect spin so a missing backoff fails fast.
    """
    port_ready = threading.Event()
    stop = threading.Event()
    accepted: list = []
    held: list = []
    server_err: list = []
    port_box: dict = {}
    srv_box: dict = {}

    def desktop() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_box["srv"] = srv
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(8)
            srv.settimeout(0.2)
            port_box["port"] = int(srv.getsockname()[1])
            port_ready.set()
            while not stop.is_set() and not crate_client._SHUTDOWN.is_set():
                try:
                    conn, _addr = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                accepted.append(conn)
                if len(accepted) >= safety:
                    crate_client._SHUTDOWN.set()
                    try:
                        conn.close()
                    except OSError:
                        pass
                    break
                try:
                    on_accept(conn, len(accepted), held)
                except BaseException as exc:
                    server_err.append(exc)
                    try:
                        conn.close()
                    except OSError:
                        pass
        except BaseException as exc:
            server_err.append(exc)
        finally:
            for sock in held:
                try:
                    sock.close()
                except OSError:
                    pass
            try:
                srv.close()
            except OSError:
                pass

    server = threading.Thread(target=desktop, name="crate-backoff-desktop", daemon=True)
    server.start()
    assert port_ready.wait(timeout=2.0), f"desktop did not bind: {server_err!r}"
    rc: dict = {}

    def run_client() -> None:
        try:
            rc["v"] = crate_client.main(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port_box["port"]),
                    "--no-camera",
                    "--no-mic",
                    "--log-file",
                    str(tmp_path / "crate-pi.log"),
                ]
            )
        except BaseException as exc:
            rc["err"] = exc

    client = threading.Thread(target=run_client, name="crate-backoff-client", daemon=True)
    try:
        client.start()
        client.join(timeout=join_s)
        if client.is_alive():
            crate_client._SHUTDOWN.set()
            stop.set()
            srv = srv_box.get("srv")
            if srv is not None:
                try:
                    srv.close()
                except OSError:
                    pass
            for sock in accepted:
                try:
                    sock.close()
                except OSError:
                    pass
            client.join(timeout=2.0)
        else:
            stop.set()
        server.join(timeout=2.0)
        assert not client.is_alive(), f"client still running rc={rc} err={server_err!r}"
        assert not server.is_alive(), f"desktop still running err={server_err!r}"
        assert "err" not in rc, rc.get("err")
        assert server_err == [], server_err
        return accepted, rc.get("v")
    finally:
        stop.set()
        crate_client._SHUTDOWN.set()
        srv = srv_box.get("srv")
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        for sock in accepted:
            try:
                sock.close()
            except OSError:
                pass
        client.join(timeout=2.0)
        server.join(timeout=2.0)
        crate_client._reset_shutdown_state_for_tests()


def test_reconnect_backoff_is_interruptible(crate_client, monkeypatch):
    """Quit during the reconnect wait returns before the capped delay."""
    crate_client._reset_shutdown_state_for_tests()
    monkeypatch.setattr(crate_client, "RECONNECT_BACKOFF_JITTER_S", 0.0)
    monkeypatch.setattr(crate_client, "_stdin_ready", lambda timeout: False)
    quit_ev = threading.Event()
    done: dict = {}

    def quitter() -> None:
        time.sleep(0.05)
        quit_ev.set()

    def run() -> None:
        done["v"] = crate_client._backoff_reconnect(
            quit_ev, float(crate_client.RECONNECT_BACKOFF_MAX_S)
        )

    threading.Thread(target=quitter, daemon=True).start()
    worker = threading.Thread(target=run, daemon=True)
    try:
        t0 = time.monotonic()
        worker.start()
        worker.join(timeout=1.5)
        if worker.is_alive():
            quit_ev.set()
            crate_client._SHUTDOWN.set()
            worker.join(timeout=1.0)
        assert not worker.is_alive(), "reconnect backoff ignored quit"
        assert done.get("v") is True
        assert (time.monotonic() - t0) < 1.0
    finally:
        quit_ev.set()
        crate_client._SHUTDOWN.set()
        worker.join(timeout=1.0)
        crate_client._reset_shutdown_state_for_tests()


def test_close_at_accept_backs_off_within_ten_seconds(crate_client, monkeypatch, tmp_path):
    """A desktop that closes at accept is not polled in a hot loop.

    Waits are the real 1s, 2s, 4s, … schedule (jitter zeroed). The virtual
    clock stops once those delays add up to 10s. That window allows at most
    six connects: 1+2+4 = 7, and the next 8s wait is still inside the window.
    """
    _stub_client_side(crate_client, monkeypatch)
    monkeypatch.setattr(
        crate_client, "_wait_trigger", lambda gpio, stop, conn=None: "disconnect"
    )
    waits: list[float] = []
    _virtual_reconnect_waits(
        crate_client, monkeypatch, waits, lambda seen: sum(seen) >= 10.0
    )

    def on_accept(conn, _n, _held) -> None:
        conn.close()

    accepted, rc = _drive_pi_main(crate_client, on_accept, tmp_path)
    assert crate_client.RECONNECT_BACKOFF_MIN_S == 1.0
    assert crate_client.RECONNECT_BACKOFF_MAX_S == 30.0
    assert len(accepted) <= 6, f"connects={len(accepted)} waits={waits}"
    assert sum(waits) >= 10.0, waits
    assert waits[0] == pytest.approx(1.0)
    assert rc == 0


def test_reconnect_backoff_resets_after_served_session(crate_client, monkeypatch, tmp_path):
    """A session that stays up past the minimum good duration resets the backoff."""
    from gourdsworth.net import PROTO, send_msg

    _stub_client_side(crate_client, monkeypatch)
    monkeypatch.setenv("CRATE_MIN_GOOD_SESSION_S", "0.05")

    def wait_trigger(gpio, stop, conn=None):
        time.sleep(0.12)
        return "disconnect"

    monkeypatch.setattr(crate_client, "_wait_trigger", wait_trigger)
    waits: list[float] = []
    assert _virtual_reconnect_waits(
        crate_client, monkeypatch, waits, lambda seen: len(seen) >= 4
    )

    def on_accept(conn, n, held) -> None:
        if n != 4:
            conn.close()
            return
        conn.settimeout(1.0)
        try:
            data = conn.recv(4096)
        except OSError:
            data = b""
        if not data:
            conn.close()
            raise AssertionError("served connection sent no hello")
        send_msg(conn, {"event": "hello", "role": "desktop", "proto": PROTO})
        send_msg(conn, {"event": "ready"})
        held.append(conn)

    accepted, rc = _drive_pi_main(crate_client, on_accept, tmp_path)
    assert waits == pytest.approx([1.0, 2.0, 4.0, 1.0]), waits
    assert len(accepted) <= 6, len(accepted)
    assert rc == 0


def test_short_ready_close_grows_backoff_and_announces_once(
    crate_client, monkeypatch, tmp_path
):
    """hello + ready + immediate close keeps doubling and chimes once."""
    import builtins

    from gourdsworth.net import PROTO, recv_msg, send_msg

    _stub_client_side(crate_client, monkeypatch)
    plays: list[str] = []
    lines: list[str] = []
    orig_print = builtins.print

    def spy(*args, **kwargs):
        lines.append(" ".join(str(item) for item in args))
        return orig_print(*args, **kwargs)

    def play(**kwargs):
        plays.append(str(kwargs.get("kind") or ""))

    monkeypatch.setattr(builtins, "print", spy)
    monkeypatch.setattr(crate_client, "_play_local_shutdown", play)
    waits: list[float] = []
    assert _virtual_reconnect_waits(
        crate_client, monkeypatch, waits, lambda seen: len(seen) >= 4
    )

    def on_accept(conn, _n, _held) -> None:
        conn.settimeout(1.0)
        buf = bytearray()
        header, _payload = recv_msg(conn, buf)
        if header.get("event") != "hello":
            raise AssertionError(f"expected hello, got {header!r}")
        send_msg(conn, {"event": "hello", "role": "desktop", "proto": PROTO})
        send_msg(conn, {"event": "ready"})
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()

    accepted, rc = _drive_pi_main(crate_client, on_accept, tmp_path)
    assert len(accepted) >= 4, len(accepted)
    assert waits[0] == pytest.approx(1.0)
    assert all(nxt + 1e-9 >= prev for prev, nxt in zip(waits, waits[1:]))
    cap = float(crate_client.RECONNECT_BACKOFF_MAX_S)
    for prev, nxt in zip(waits, waits[1:]):
        assert nxt == pytest.approx(min(prev * 2.0, cap))
    assert len(waits) >= 4, waits
    ready_lines = [line for line in lines if line.strip() == "ready."]
    gone_lines = [line for line in lines if "desktop gone" in line]
    assert len(ready_lines) >= 4, lines
    assert len(gone_lines) == 1, gone_lines
    assert plays == ["disconnect"], plays
    assert rc == 0


def test_completed_tts_resets_backoff_without_min_duration(
    crate_client, monkeypatch, tmp_path
):
    """A finished server reply resets backoff even when the session is short."""
    from gourdsworth.net import PROTO, recv_msg, send_msg

    _stub_client_side(crate_client, monkeypatch)
    monkeypatch.setenv("CRATE_MIN_GOOD_SESSION_S", "30")

    def fake_tts(header, payload):
        del header
        return bool(payload)

    monkeypatch.setattr(crate_client, "_play_tts", fake_tts)

    def wait_trigger(gpio, stop, conn=None):
        del gpio, stop
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if crate_client._SESSION_HAD_TRAFFIC:
                break
            if conn is not None and conn.closed:
                break
            time.sleep(0.01)
        return "disconnect"

    monkeypatch.setattr(crate_client, "_wait_trigger", wait_trigger)
    waits: list[float] = []
    assert _virtual_reconnect_waits(
        crate_client, monkeypatch, waits, lambda seen: len(seen) >= 4
    )

    def on_accept(conn, n, held) -> None:
        if n != 4:
            conn.close()
            return
        conn.settimeout(1.0)
        buf = bytearray()
        header, _payload = recv_msg(conn, buf)
        if header.get("event") != "hello":
            raise AssertionError(header)
        send_msg(conn, {"event": "hello", "role": "desktop", "proto": PROTO})
        send_msg(conn, {"event": "ready"})
        send_msg(
            conn,
            {"event": "play", "rate": 16000, "channels": 1, "format": "f32le"},
            b"\x00\x00\x00\x00" * 8,
        )
        held.append(conn)

    _accepted, rc = _drive_pi_main(crate_client, on_accept, tmp_path, join_s=4.0)
    assert waits == pytest.approx([1.0, 2.0, 4.0, 1.0]), waits
    assert rc == 0
