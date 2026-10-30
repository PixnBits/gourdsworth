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

    # Both addresses are trusted. A non-empty list is an allowlist, so the
    # first peer has to be on it to be served at all. The second may replace.
    monkeypatch.setenv("CRATE_TRUSTED_PEERS", "192.0.2.30,198.51.100.30")
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


class _CountingSock:
    """Counts bytes _read_crate_hello pulls. Forwards only recv and settimeout."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self.n = 0

    def recv(self, size: int) -> bytes:
        data = self._sock.recv(size)
        self.n += len(data)
        return data

    def settimeout(self, value) -> None:
        self._sock.settimeout(value)


def _plans():
    return {}, threading.Lock()


def _connect_as(port: int, plans: dict, lock: threading.Lock, ip: str) -> socket.socket:
    """Connect from 127.0.0.1 and map this socket's port to a policy IP."""
    deadline = time.monotonic() + 2.0
    last: BaseException | None = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            with lock:
                plans[sock.getsockname()[1]] = ip
            sock.settimeout(2)
            sock.connect(("127.0.0.1", port))
            return sock
        except ConnectionRefusedError as exc:
            last = exc
            sock.close()
            time.sleep(0.02)
        except OSError:
            sock.close()
            raise
    raise AssertionError(f"connect failed: {last}")


def _eof_within(sock: socket.socket, timeout: float) -> bool:
    sock.settimeout(0.05)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = sock.recv(16)
        except TimeoutError:
            continue
        except OSError:
            return True
        if not data:
            return True
    return False


def _hook(plans, lock):
    def hook(addr):
        with lock:
            return plans[addr[1]]

    return hook


def _read_hello_returns(left: socket.socket, right: socket.socket, blob: bytes):
    from gourdsworth.net import _read_crate_hello

    right.sendall(blob)
    counted = _CountingSock(left)
    started = time.monotonic()
    got = _read_crate_hello(counted)
    return got, counted.n, time.monotonic() - started


def test_slow_drip_hello_closes_at_total_deadline(monkeypatch):
    """1 byte every (2/3 of the deadline) must die at the deadline, not the next recv."""
    from gourdsworth import net as netmod
    from gourdsworth.net import run_echo_session

    deadline_s = 0.40
    drip_s = 0.25  # under the deadline, same ratio as 2s / 3s
    monkeypatch.setattr(netmod, "HELLO_DEADLINE_S", deadline_s, raising=False)
    listener, thread, box = _serve(run_echo_session)
    client = None
    drip = None
    stop = threading.Event()
    drip_thread = None
    try:
        client = _connect_served(listener.port)
        drip = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        drip.settimeout(0.05)
        start = time.monotonic()

        def dribbles() -> None:
            payload = b'{"event":"hello","role":"crate","proto":1}'
            for byte in payload:
                if stop.is_set():
                    return
                try:
                    drip.send(bytes([byte]))
                except OSError:
                    return
                if stop.wait(drip_s):
                    return

        drip_thread = threading.Thread(target=dribbles, name="crate-drip", daemon=True)
        drip_thread.start()
        closed_at = None
        while time.monotonic() < start + 1.2:
            try:
                data = drip.recv(16)
            except TimeoutError:
                continue
            except OSError:
                closed_at = time.monotonic()
                break
            if not data:
                closed_at = time.monotonic()
                break
        assert closed_at is not None, "drip hello was still open after the deadline"
        elapsed = closed_at - start
        assert elapsed < deadline_s + 0.35, elapsed
        assert elapsed > 0.15, elapsed
        assert not client.closed
        _echo_turn(client)
    finally:
        stop.set()
        if drip_thread is not None:
            drip_thread.join(1.0)
        if drip is not None:
            drip.close()
        if client is not None:
            client.close()
        _stop(listener, thread, box)


def test_oversized_hello_and_payload_are_not_read(monkeypatch):
    from gourdsworth import net as netmod
    from gourdsworth.net import HELLO_MAX_BYTES, run_echo_session

    monkeypatch.setattr(netmod, "HELLO_DEADLINE_S", 0.30, raising=False)
    left, right = socket.socketpair()
    try:
        blob = b"{" * (HELLO_MAX_BYTES + 80)
        got, consumed, elapsed = _read_hello_returns(left, right, blob)
        assert got is None
        assert consumed <= HELLO_MAX_BYTES
        assert elapsed < 0.5, elapsed
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        header = b'{"event":"hello","role":"crate","proto":1,"n":100000}\n'
        payload = b"Z" * 8000
        got, consumed, elapsed = _read_hello_returns(left, right, header + payload)
        assert got is None
        assert consumed <= HELLO_MAX_BYTES
        assert consumed < len(header) + len(payload)
        assert elapsed < 0.5, elapsed
    finally:
        left.close()
        right.close()

    # The server path closes both without touching a live session.
    listener, thread, box = _serve(run_echo_session)
    client = None
    fat = None
    wide = None
    try:
        client = _connect_served(listener.port)
        fat = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        fat.sendall(b'{"event":"hello","role":"crate","proto":1,"n":100000}\n')
        assert _eof_within(fat, 0.35), "payload hello was not rejected promptly"
        wide = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        wide.sendall(b"{" * (HELLO_MAX_BYTES + 40))
        assert _eof_within(wide, 0.35), "oversized hello was not rejected promptly"
        assert not client.closed
        _echo_turn(client)
    finally:
        if wide is not None:
            wide.close()
        if fat is not None:
            fat.close()
        if client is not None:
            client.close()
        _stop(listener, thread, box)


def test_pending_hello_caps_keep_the_served_client(monkeypatch):
    from gourdsworth import net as netmod
    from gourdsworth.net import HELLO_PENDING_MAX, HELLO_PENDING_PER_IP, run_echo_session

    # Longer than the burst that opens every capped socket. A short deadline
    # lets the first hellos expire mid-burst, which frees a slot and the
    # "extra" connection is admitted instead of refused. Still far longer
    # than the immediate-close check below.
    deadline_s = 8.0
    monkeypatch.setattr(netmod, "HELLO_DEADLINE_S", deadline_s, raising=False)
    plans, lock = _plans()
    listener, thread, box = _serve(run_echo_session, peer_ip=_hook(plans, lock))
    client = None
    held: list[socket.socket] = []
    extra_ip = None
    extra_global = None
    newbie = None
    try:
        client_sock = _connect_as(listener.port, plans, lock, "192.0.2.10")
        client = CrateConnection(client_sock)
        client.send({"event": "hello", "role": "crate", "proto": PROTO})
        assert client.wait_event("hello", timeout=2.0) is not None
        assert client.wait_event("ready", timeout=2.0) is not None

        per_ip = "192.0.2.20"
        for _ in range(HELLO_PENDING_PER_IP):
            held.append(_connect_as(listener.port, plans, lock, per_ip))
        extra_ip = _connect_as(listener.port, plans, lock, per_ip)
        assert _eof_within(extra_ip, 0.25), "per-IP hello cap did not close at accept"
        assert _still_pending(held[-1]), "capped IP's earlier hello was closed with the extra"

        # 2 already held for per_ip. Fill the global cap with other addresses.
        need = HELLO_PENDING_MAX - len(held)
        n_ips = (need + HELLO_PENDING_PER_IP - 1) // HELLO_PENDING_PER_IP
        for index in range(n_ips):
            ip = f"192.0.2.{21 + index}"
            for _ in range(HELLO_PENDING_PER_IP):
                if len(held) >= HELLO_PENDING_MAX:
                    break
                held.append(_connect_as(listener.port, plans, lock, ip))
        assert len(held) == HELLO_PENDING_MAX
        time.sleep(0.05)  # let the accept thread reserve the sockets it already took
        extra_global = _connect_as(listener.port, plans, lock, "192.0.2.80")
        assert _eof_within(extra_global, 0.25), "global hello cap did not close at accept"
        assert not client.closed
        _echo_turn(client)

        # One budget for the whole set: a hello accepted at the end of the
        # burst still has a full deadline left. Earlier ones are already closed.
        give_up = time.monotonic() + deadline_s + 0.5
        for sock in held:
            left = give_up - time.monotonic()
            assert left > 0.05, "ran out of time before every pending hello closed"
            assert _eof_within(sock, left), "pending hello outlived the deadline"
        client.close()
        client = None
        # The echo session notices the drop on its next read. A hello that
        # arrives before that is correctly busy, so retry until the slot frees.
        admit_until = time.monotonic() + 3.0
        while True:
            newbie_sock = _connect_as(listener.port, plans, lock, "198.51.100.55")
            newbie = CrateConnection(newbie_sock)
            newbie.send({"event": "hello", "role": "crate", "proto": PROTO})
            if newbie.wait_event("hello", timeout=0.6) is not None:
                assert newbie.wait_event("ready", timeout=2.0) is not None
                break
            newbie.close()
            newbie = None
            assert time.monotonic() < admit_until, "newcomer was not admitted"
            time.sleep(0.15)
    finally:
        for sock in (extra_ip, extra_global):
            if sock is not None:
                sock.close()
        for sock in held:
            try:
                sock.close()
            except OSError:
                pass
        if newbie is not None:
            newbie.close()
        if client is not None:
            client.close()
        _stop(listener, thread, box)


def _still_pending(sock: socket.socket) -> bool:
    sock.settimeout(0.1)
    try:
        data = sock.recv(1)
    except TimeoutError:
        return True
    except OSError:
        return False
    return False if data == b"" else True


def test_untrusted_is_dropped_at_accept_when_list_is_set(monkeypatch, capsys):
    from gourdsworth import app as appmod
    from gourdsworth.app import _trusted_for_start
    from gourdsworth.net import TrustedPeersError, parse_trusted_peers, run_echo_session

    assert parse_trusted_peers("192.0.2.7 198.51.100.8") == frozenset(
        {"192.0.2.7", "198.51.100.8"}
    )
    assert parse_trusted_peers("::ffff:192.0.2.7") == frozenset({"192.0.2.7"})
    with pytest.raises(TrustedPeersError) as excinfo:
        parse_trusted_peers("*, 192.0.2.0/24, localhost, junk")
    text = str(excinfo.value)
    for token in ("*", "192.0.2.0/24", "localhost", "junk"):
        assert token in text
    monkeypatch.delenv("CRATE_TRUSTED_PEERS", raising=False)
    with pytest.raises(TrustedPeersError) as excinfo:
        _trusted_for_start(
            {"crate": {"trusted_peers": ["192.0.2.55", "pi.local"]}},
            "127.0.0.1",
        )
    assert "pi.local" in str(excinfo.value)
    assert "192.0.2.55" not in str(excinfo.value)

    def _no_serve(*_args, **_kwargs):
        raise AssertionError("server started with invalid trusted peers")

    monkeypatch.setattr(appmod, "serve_echo", _no_serve)
    monkeypatch.setenv(
        "CRATE_TRUSTED_PEERS",
        "192.0.2.55, *, 192.0.2.0/24, localhost, junk, ::ffff:198.51.100.55",
    )
    code = appmod.main(
        ["--crate-echo", "--crate-host", "127.0.0.1", "--crate-port", "0"]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "Refusing to start" in err
    for token in ("*", "192.0.2.0/24", "localhost", "junk"):
        assert token in err
    assert "192.0.2.55" not in err
    assert "198.51.100.55" not in err

    monkeypatch.setenv(
        "CRATE_TRUSTED_PEERS",
        "192.0.2.55, ::ffff:198.51.100.55",
    )
    plans, lock = _plans()
    listener, thread, box = _serve(run_echo_session, peer_ip=_hook(plans, lock))
    stranger = None
    trusted = None
    mapped = None
    try:
        stranger = _connect_as(listener.port, plans, lock, "198.51.100.9")
        assert _eof_within(stranger, 0.4), "untrusted peer was not closed at accept"
        trusted = _connect_as(listener.port, plans, lock, "192.0.2.55")
        _expect_desktop_ready(trusted)
        trusted.close()
        trusted = None
        mapped = _connect_as(listener.port, plans, lock, "::ffff:198.51.100.55")
        _expect_desktop_ready(mapped)
    finally:
        for sock in (stranger, trusted, mapped):
            if sock is not None:
                sock.close()
        _stop(listener, thread, box)


def _expect_desktop_ready(sock: socket.socket) -> None:
    send_msg(sock, {"event": "hello", "role": "crate", "proto": PROTO})
    events, _eof = _read_lines_until_eof(sock, timeout=2.0)
    assert any(item.get("event") == "hello" and item.get("role") == "desktop" for item in events)
    assert any(item.get("event") == "ready" for item in events)


def test_lan_bind_with_empty_trust_warns_and_still_returns(monkeypatch, capsys):
    from gourdsworth.app import _trusted_for_start

    monkeypatch.delenv("CRATE_TRUSTED_PEERS", raising=False)
    trusted = _trusted_for_start({"crate": {"trusted_peers": []}}, "0.0.0.0")
    assert trusted == frozenset()
    err = capsys.readouterr().err
    assert "empty slot" in err
    assert "CRATE_TRUSTED_PEERS" in err
    assert "systemctl --user edit gourdsworth-server" in err
    assert "192.0.2.55" in err
    pinned = _trusted_for_start({"crate": {"trusted_peers": ["192.0.2.55"]}}, "0.0.0.0")
    assert pinned == frozenset({"192.0.2.55"})
    assert capsys.readouterr().err == ""
    loop = _trusted_for_start({"crate": {}}, "127.0.0.1")
    assert loop == frozenset()
    assert capsys.readouterr().err == ""


def test_different_trusted_ip_starts_with_empty_history(monkeypatch):
    from gourdsworth.net import carry_history_for_peer

    monkeypatch.setenv("CRATE_TRUSTED_PEERS", "192.0.2.55,198.51.100.55")
    history = [{"role": "user", "content": "porch-secret"}]
    last = {"ip": None}
    seen: list[tuple[str, list]] = []
    plans, lock = _plans()
    trusted = frozenset({"192.0.2.55", "198.51.100.55"})

    def session(conn) -> None:
        last["ip"] = carry_history_for_peer(
            history, conn.policy_ip, last["ip"], trusted
        )
        seen.append((conn.policy_ip, [dict(item) for item in history]))
        deadline = time.monotonic() + 2.0
        while not conn.closed and time.monotonic() < deadline:
            time.sleep(0.02)

    listener, thread, box = _serve(session, peer_ip=_hook(plans, lock))
    first = None
    second = None
    try:
        first = _connect_as(listener.port, plans, lock, "192.0.2.55")
        send_msg(first, {"event": "hello", "role": "crate", "proto": PROTO})
        _wait_until(lambda: len(seen) >= 1, 2.0, "first session did not start")
        assert seen[0][1][0]["content"] == "porch-secret"
        second = _connect_as(listener.port, plans, lock, "198.51.100.55")
        send_msg(second, {"event": "hello", "role": "crate", "proto": PROTO})
        _wait_until(lambda: len(seen) >= 2, 2.0, "trusted replacement did not start")
        assert seen[1][0] == "198.51.100.55"
        assert seen[1][1] == []
        assert history == []
    finally:
        if second is not None:
            second.close()
        if first is not None:
            first.close()
        _stop(listener, thread, box)


def test_pending_hello_rejects_a_different_ip(monkeypatch):
    from gourdsworth import net as netmod

    monkeypatch.delenv("CRATE_TRUSTED_PEERS", raising=False)
    monkeypatch.setattr(netmod, "HELLO_DEADLINE_S", 1.0, raising=False)
    plans, lock = _plans()
    listener = CrateListener("127.0.0.1", 0)
    gate = netmod._NewestAccept(listener, peer_ip=_hook(plans, lock))
    first = None
    other = None
    same = None
    try:
        first = _connect_as(listener.port, plans, lock, "192.0.2.55")
        send_msg(first, {"event": "hello", "role": "crate", "proto": PROTO})

        def pending_is_first() -> bool:
            with gate._cv:
                pending = gate._pending
                return pending is not None and pending.policy_ip == "192.0.2.55"

        _wait_until(pending_is_first, 2.0, "first hello was not held pending")
        with gate._cv:
            first_conn = gate._pending
        other = _connect_as(listener.port, plans, lock, "198.51.100.55")
        send_msg(other, {"event": "hello", "role": "crate", "proto": PROTO})
        events, eof = _read_lines_until_eof(other, timeout=1.0)
        assert eof, "different IP was not closed"
        assert any(item.get("message") == "busy" for item in events)
        with gate._cv:
            assert gate._pending is first_conn
            assert gate._pending.policy_ip == "192.0.2.55"
        assert not _eof_within(first, 0.15), "pending session was replaced by another IP"
        same = _connect_as(listener.port, plans, lock, "192.0.2.55")
        send_msg(same, {"event": "hello", "role": "crate", "proto": PROTO})

        def pending_replaced() -> bool:
            with gate._cv:
                pending = gate._pending
                return pending is not None and pending is not first_conn and pending.policy_ip == "192.0.2.55"

        _wait_until(pending_replaced, 2.0, "same IP did not replace the pending hello")
        assert _eof_within(first, 1.0), "replaced pending socket stayed open"
    finally:
        for sock in (same, other, first):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        gate.close()


def test_replaced_at_prunes_expired_entries(monkeypatch):
    from gourdsworth import net as netmod

    monkeypatch.delenv("CRATE_TRUSTED_PEERS", raising=False)
    listener = CrateListener("127.0.0.1", 0)
    gate = netmod._NewestAccept(listener)
    try:
        gate._replaced_at["192.0.2.55"] = time.monotonic() - 100
        gate._replaced_at["198.51.100.55"] = time.monotonic()
        with gate._cv:
            gate._prune_replaced_locked()
        assert "192.0.2.55" not in gate._replaced_at
        assert "198.51.100.55" in gate._replaced_at
    finally:
        gate.close()


def test_emfile_accept_errors_log_once_with_count(monkeypatch, capsys):
    from gourdsworth import net as netmod

    calls = {"n": 0}
    delays: list[float] = []

    def boom(self):
        calls["n"] += 1
        if calls["n"] > 25:
            raise OSError(errno.ENOBUFS, "no buffer space")
        raise OSError(errno.EMFILE, "Too many open files")

    def fast_pause(self, delay):
        delays.append(delay)
        return False

    monkeypatch.setattr(netmod.CrateListener, "accept_socket", boom)
    monkeypatch.setattr(netmod._NewestAccept, "_pause_accept", fast_pause)
    box: dict = {}

    def run() -> None:
        try:
            box["code"] = netmod.serve_echo("127.0.0.1", 0)
        except BaseException as exc:
            box["exc"] = exc

    thread = threading.Thread(target=run, name="crate-emfile", daemon=True)
    thread.start()
    thread.join(3.0)
    if thread.is_alive():
        # Unblock a loop that still sleeps, then fail below.
        thread.join(0.2)
    assert not thread.is_alive(), "accept-error server did not exit"
    assert box.get("exc") is None, box.get("exc")
    assert box.get("code") == 1
    err = capsys.readouterr().err
    transient = [line for line in err.splitlines() if "transient" in line]
    assert len(transient) == 2, err
    assert "Too many open files" in transient[0]
    assert "24 suppressed" in transient[1]
    assert delays[:5] == pytest.approx([0.2, 0.4, 0.8, 1.6, 2.0])
    assert delays[-1] == pytest.approx(2.0)
    assert all(item <= 2.0 + 1e-9 for item in delays)


def test_vet_thread_start_failure_releases_slot_and_serves_next(monkeypatch, capsys):
    """A thread.start failure must not leak a hello slot or stop accept."""
    from gourdsworth import net as netmod
    from gourdsworth.net import run_echo_session

    monkeypatch.delenv("CRATE_TRUSTED_PEERS", raising=False)
    gates: list = []
    orig = netmod._NewestAccept

    def capturing(*args, **kwargs):
        gate = orig(*args, **kwargs)
        gates.append(gate)
        return gate

    monkeypatch.setattr(netmod, "_NewestAccept", capturing)
    real_start = threading.Thread.start
    state = {"boom": False}

    def start(self):
        if self.name == "gourdsworth-crate-hello" and not state["boom"]:
            state["boom"] = True
            raise RuntimeError("cannot start thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    served: list[str] = []

    def session(conn) -> None:
        served.append(str(conn.policy_ip))
        run_echo_session(conn)

    listener, thread, box = _serve(session)
    first = None
    second = None
    try:
        first = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        assert _eof_within(first, 1.0), "failed vet socket was not closed"
        assert gates, "accept gate was not created"
        gate = gates[0]

        def slots_clear() -> bool:
            with gate._cv:
                return (
                    len(gate._hello_ips) == 0
                    and len(gate._vet_socks) == 0
                    and not gate._vetters
                )

        _wait_until(slots_clear, 1.0, "hello slot was not released after thread start failed")
        err = capsys.readouterr().err
        assert "failed to start" in err
        assert state["boom"] is True
        assert thread.is_alive(), "accept loop exited after thread start failed"
        assert served == []
        second = _connect_served(listener.port)
        _wait_until(lambda: len(served) == 1, 1.0, "next client was not served")
        assert box.get("err") is None, box.get("err")
    finally:
        if second is not None:
            second.close()
        if first is not None:
            try:
                first.close()
            except OSError:
                pass
        try:
            listener.close()
        except OSError:
            pass
        thread.join(timeout=2.0)
        assert not thread.is_alive(), f"server still running err={box.get('err')!r}"
