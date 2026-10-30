"""Hello-before-replace, trusted peers, history, and accept failures.

Every socket is 127.0.0.1 with an ephemeral port. Policy IPs that are not
the real peer use the documentation ranges 192.0.2.0/24 and 198.51.100.0/24.
Timeouts are short so a regression fails instead of hanging.
"""

from __future__ import annotations

import errno
import json
import socket
import threading
import time

import numpy as np
import pytest

from gourdsworth.net import (
    PROTO,
    UPLINK_CHUNK_SAMPLES,
    UPLINK_FORMAT,
    UPLINK_RATE,
    CrateConnection,
    CrateListener,
    recv_msg,
    send_msg,
    serve_newest,
)


def _wait_until(pred, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def _serve(session_fn, *, peer_ip=None):
    listener = CrateListener("127.0.0.1", 0)
    box: dict = {"code": None, "err": None}

    def run() -> None:
        try:
            box["code"] = serve_newest(listener, session_fn, peer_ip=peer_ip)
        except BaseException as exc:
            box["err"] = exc

    thread = threading.Thread(target=run, name="crate-admit", daemon=True)
    thread.start()
    return listener, thread, box


def _stop(listener, thread, box, timeout: float = 2.0) -> None:
    try:
        listener.close()
    except OSError:
        pass
    thread.join(timeout)
    assert not thread.is_alive(), f"server still running code={box.get('code')!r} err={box.get('err')!r}"
    assert box.get("err") is None, box.get("err")


def _read_lines_until_eof(sock: socket.socket, timeout: float) -> tuple[list[dict], bool]:
    sock.settimeout(0.2)
    buf = bytearray()
    events: list[dict] = []
    eof = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            eof = True
            break
        if not chunk:
            eof = True
            break
        buf.extend(chunk)
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(buf[:nl])
            del buf[: nl + 1]
            if line:
                events.append(json.loads(line))
    return events, eof


def _connect_served(port: int):
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    conn = CrateConnection(sock)
    try:
        conn.send({"event": "hello", "role": "crate", "proto": PROTO})
        hello = conn.wait_event("hello", timeout=2.0)
        assert hello is not None, "client was not served"
        assert hello[0].get("role") == "desktop"
        ready = conn.wait_event("ready", timeout=2.0)
        assert ready is not None
        return conn
    except Exception:
        conn.close()
        raise


def _echo_turn(conn: CrateConnection) -> None:
    tone = (0.25 * np.sin(2 * np.pi * np.arange(UPLINK_CHUNK_SAMPLES) / 40)).astype(
        np.float32
    )
    pcm = (tone * 32767).astype("<i2").tobytes()
    conn.send({"event": "button", "state": "down"})
    conn.send(
        {"event": "pcm", "rate": UPLINK_RATE, "channels": 1, "format": UPLINK_FORMAT},
        pcm,
    )
    conn.send({"event": "button", "state": "up"})
    play = conn.wait_event("play", timeout=2.0)
    assert play is not None, "served client did not complete an echo turn"
    conn.send({"event": "play_done"})
    assert conn.wait_event("gesture", timeout=2.0) is not None
    assert conn.wait_event("ready", timeout=2.0) is not None


def _send_hello(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.settimeout(2)
    send_msg(sock, {"event": "hello", "role": "crate", "proto": PROTO})
    return sock


def test_primed_hello_is_what_handshake_returns():
    from gourdsworth.net import CrateConnection as Conn

    left, right = socket.socketpair()
    conn = None
    try:
        hello = {"event": "hello", "role": "crate", "proto": PROTO, "continuous": True}
        conn = Conn(right, peer=("127.0.0.1", 1), primed=(hello, None))

        def reader() -> None:
            buf = bytearray()
            recv_msg(left, buf)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        got = conn.handshake("desktop", timeout=2.0)
        thread.join(timeout=2.0)
        assert got is not None
        assert got.get("continuous") is True
        assert got.get("role") == "crate"
        assert got.get("proto") == PROTO
    finally:
        if conn is not None:
            conn.close()
        left.close()


def test_parse_trusted_peers_list_and_csv():
    from gourdsworth.net import parse_trusted_peers

    assert parse_trusted_peers(None) == frozenset()
    assert parse_trusted_peers("") == frozenset()
    assert parse_trusted_peers([]) == frozenset()
    assert parse_trusted_peers("192.0.2.1, 198.51.100.2") == frozenset(
        {"192.0.2.1", "198.51.100.2"}
    )
    assert parse_trusted_peers(["192.0.2.1", "198.51.100.2"]) == frozenset(
        {"192.0.2.1", "198.51.100.2"}
    )


def test_invalid_connect_does_not_end_served_session(monkeypatch):
    from gourdsworth import net as netmod
    from gourdsworth.net import run_echo_session

    monkeypatch.setattr(netmod, "HELLO_DEADLINE_S", 0.35, raising=False)
    listener, thread, box = _serve(run_echo_session)
    client = None
    silent = None
    try:
        client = _connect_served(listener.port)
        bare = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        bare.close()
        wrong = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        try:
            send_msg(wrong, {"event": "hello", "role": "desktop", "proto": PROTO})
            wrong.settimeout(0.5)
            try:
                wrong.recv(16)
            except OSError:
                pass
        finally:
            wrong.close()
        garbage = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        try:
            garbage.sendall(b"{{{not-json\n")
            garbage.settimeout(0.5)
            try:
                garbage.recv(16)
            except OSError:
                pass
        finally:
            garbage.close()
        silent = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        deadline = time.monotonic() + 0.7
        while time.monotonic() < deadline:
            assert not client.closed, "newcomer ended the live session"
            time.sleep(0.05)
        assert not client.closed
        _echo_turn(client)
    finally:
        if silent is not None:
            silent.close()
        if client is not None:
            client.close()
        _stop(listener, thread, box)


def test_different_untrusted_ip_is_rejected(monkeypatch):
    from gourdsworth.net import run_echo_session

    monkeypatch.delenv("CRATE_TRUSTED_PEERS", raising=False)
    plan = ["192.0.2.20", "198.51.100.21"]

    def hook(_addr):
        return plan.pop(0)

    listener, thread, box = _serve(run_echo_session, peer_ip=hook)
    client = None
    newbie = None
    try:
        client = _connect_served(listener.port)
        newbie = _send_hello(listener.port)
        events, eof = _read_lines_until_eof(newbie, timeout=2.0)
        assert eof, "rejected newcomer was not closed"
        assert any(item.get("event") == "error" and item.get("message") == "busy" for item in events)
        assert not client.closed
        _echo_turn(client)
    finally:
        if newbie is not None:
            newbie.close()
        if client is not None:
            client.close()
        _stop(listener, thread, box)


def test_trusted_peer_replaces_live_session(monkeypatch):
    from gourdsworth.net import run_echo_session

    monkeypatch.setenv("CRATE_TRUSTED_PEERS", "198.51.100.30")
    plan = ["192.0.2.30", "198.51.100.30"]

    def hook(_addr):
        return plan.pop(0)

    listener, thread, box = _serve(run_echo_session, peer_ip=hook)
    old = None
    newbie = None
    try:
        old = _connect_served(listener.port)
        newbie = _connect_served(listener.port)
        superseded = old.wait_event("superseded", timeout=2.0)
        assert superseded is not None
        assert superseded[0].get("event") == "superseded"
        _wait_until(lambda: old.closed, 2.0, "replaced client stayed open")
        _echo_turn(newbie)
    finally:
        if newbie is not None:
            newbie.close()
        if old is not None:
            old.close()
        _stop(listener, thread, box)


def test_same_ip_replace_sends_superseded_and_keeps_history(capsys):
    from gourdsworth.net import carry_history_for_peer

    history = [{"role": "user", "content": "porch-secret"}]
    last = {"ip": None}
    seen: list[list] = []

    def session(conn) -> None:
        last["ip"] = carry_history_for_peer(
            history, conn.policy_ip, last["ip"], frozenset()
        )
        seen.append([dict(item) for item in history])
        deadline = time.monotonic() + 2.0
        while not conn.closed and time.monotonic() < deadline:
            time.sleep(0.02)

    listener, thread, box = _serve(session)
    old = None
    newbie = None
    try:
        old = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        old.settimeout(2)
        send_msg(old, {"event": "hello", "role": "crate", "proto": PROTO, "continuous": True})
        _wait_until(lambda: len(seen) >= 1, 2.0, "first session did not start")
        newbie = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        send_msg(newbie, {"event": "hello", "role": "crate", "proto": PROTO})
        events, eof = _read_lines_until_eof(old, timeout=2.0)
        assert any(item.get("event") == "superseded" for item in events)
        assert eof, "old client did not see EOF after superseded"
        _wait_until(lambda: len(seen) >= 2, 2.0, "replacement session did not start")
        assert seen[0][0]["content"] == "porch-secret"
        assert seen[1][0]["content"] == "porch-secret"
        assert history[0]["content"] == "porch-secret"
    finally:
        if newbie is not None:
            newbie.close()
        if old is not None:
            old.close()
        _stop(listener, thread, box)
    out = capsys.readouterr().out
    matched = [
        line
        for line in out.splitlines()
        if "crate session replaced: closing " in line
    ]
    assert matched, out
    line = matched[-1]
    left, _, right = line.partition(" for newer client ")
    old_peer = left.split("closing ", 1)[1].strip()
    new_peer = right.strip()
    assert "," not in old_peer
    assert old_peer != new_peer
    assert old_peer.count(":") == 1
    assert new_peer.count(":") == 1


def test_fresh_untrusted_peer_gets_empty_history(monkeypatch):
    from gourdsworth.net import carry_history_for_peer

    monkeypatch.delenv("CRATE_TRUSTED_PEERS", raising=False)
    history = [{"role": "user", "content": "porch-secret"}]
    last = {"ip": None}
    seen: list[tuple[str, list]] = []

    def hook(_addr):
        # First served peer, then any later socket. Retries stay on the new IP.
        if not seen:
            return "192.0.2.40"
        return "198.51.100.40"

    def session(conn) -> None:
        last["ip"] = carry_history_for_peer(
            history, conn.policy_ip, last["ip"], frozenset()
        )
        seen.append((conn.policy_ip, [dict(item) for item in history]))

    listener, thread, box = _serve(session, peer_ip=hook)
    first = None
    extras: list[socket.socket] = []
    try:
        first = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        send_msg(first, {"event": "hello", "role": "crate", "proto": PROTO})
        _wait_until(lambda: len(seen) >= 1, 2.0, "first session did not start")
        first.close()
        first = None
        # session_fn returns immediately; retry until the server has marked it idle.
        deadline = time.monotonic() + 2.0
        while len(seen) < 2 and time.monotonic() < deadline:
            sock = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
            extras.append(sock)
            send_msg(sock, {"event": "hello", "role": "crate", "proto": PROTO})
            time.sleep(0.05)
        assert len(seen) >= 2, "second session did not start"
        assert seen[1][0] == "198.51.100.40"
        assert seen[1][1] == []
        assert history == []
    finally:
        for sock in extras:
            try:
                sock.close()
            except OSError:
                pass
        if first is not None:
            first.close()
        _stop(listener, thread, box)


def test_second_replace_inside_interval_is_rejected(monkeypatch):
    from gourdsworth import net as netmod
    from gourdsworth.net import run_echo_session

    monkeypatch.setattr(netmod, "REPLACE_MIN_INTERVAL_S", 30.0, raising=False)
    listener, thread, box = _serve(run_echo_session)
    first = None
    second = None
    third = None
    try:
        first = _connect_served(listener.port)
        second = _connect_served(listener.port)
        assert first.wait_event("superseded", timeout=2.0) is not None
        third = _send_hello(listener.port)
        events, eof = _read_lines_until_eof(third, timeout=2.0)
        assert eof
        assert any(item.get("message") == "busy" for item in events)
        assert not second.closed, "rate-limited hello ended the live session"
        _echo_turn(second)
    finally:
        if third is not None:
            third.close()
        if second is not None:
            second.close()
        if first is not None:
            first.close()
        _stop(listener, thread, box)


def test_connect_rate_limit_drops_excess_without_ending_session(monkeypatch):
    from gourdsworth import net as netmod
    from gourdsworth.net import run_echo_session

    monkeypatch.setattr(netmod, "CONNECT_RATE_MAX", 1, raising=False)
    listener, thread, box = _serve(run_echo_session)
    client = None
    extra = None
    try:
        client = _connect_served(listener.port)
        extra = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        extra.settimeout(0.5)
        try:
            extra.sendall(b'{"event":"hello","role":"crate","proto":1}\n')
        except OSError:
            pass
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            assert not client.closed, "rate-limited connect ended the live session"
            time.sleep(0.05)
        _echo_turn(client)
    finally:
        if extra is not None:
            extra.close()
        if client is not None:
            client.close()
        _stop(listener, thread, box)


def test_socket_option_failure_warns_once(monkeypatch, capsys):
    from gourdsworth import net as netmod

    monkeypatch.setattr(netmod, "_sockopt_warned", False)

    def boom(self, level, opt, value):
        raise OSError("nope")

    monkeypatch.setattr(socket.socket, "setsockopt", boom)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        netmod.enable_tcp_keepalive(sock)
        netmod.enable_tcp_keepalive(sock)
    finally:
        sock.close()
    err = capsys.readouterr().err
    assert err.count("warning: crate socket option") == 1


def test_fatal_accept_error_exits_nonzero(monkeypatch, capsys):
    from gourdsworth import net as netmod

    held: dict = {}
    orig_init = netmod.CrateListener.__init__

    def capturing(self, host, port):
        orig_init(self, host, port)
        held["listener"] = self

    monkeypatch.setattr(netmod.CrateListener, "__init__", capturing)

    def boom(self):
        raise OSError(errno.ENOBUFS, "no buffer space")

    monkeypatch.setattr(netmod.CrateListener, "accept_socket", boom)
    box: dict = {}

    def run() -> None:
        try:
            box["code"] = netmod.serve_echo("127.0.0.1", 0)
        except BaseException as exc:
            box["exc"] = exc

    thread = threading.Thread(target=run, name="crate-fatal-accept", daemon=True)
    thread.start()
    thread.join(2.0)
    listener = held.get("listener")
    try:
        if thread.is_alive() and listener is not None:
            listener.close()
            thread.join(2.0)
        assert not thread.is_alive(), "serve_echo hung after a fatal accept error"
        assert box.get("exc") is None, box.get("exc")
        assert box.get("code") == 1
        assert "fatal" in capsys.readouterr().err
    finally:
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread.join(1.0)


def test_serve_crate_and_echo_return_nonzero_on_accept_failure(monkeypatch):
    import argparse

    from gourdsworth import app as appmod

    monkeypatch.setattr(appmod, "serve_echo", lambda *args, **kwargs: 1)
    args = argparse.Namespace(config=None, crate_host="127.0.0.1", crate_port=9)
    assert appmod._run_crate_echo(args) == 1

    monkeypatch.setattr(appmod, "serve_newest", lambda *args, **kwargs: 1)

    class _Listener:
        def __init__(self, host, port):
            self.host, self.port = "127.0.0.1", 9

        def close(self):
            return None

    monkeypatch.setattr(appmod, "CrateListener", _Listener)
    cfg = {
        "mode": "vad",
        "crate": {"host": "127.0.0.1", "port": 9, "allow_lan": False},
    }
    assert appmod._serve_crate(cfg, None, object(), None, None, {"goodnight": ["night"]}) == 1
