"""Crate protocol framing + in-process TCP loopback (no hardware)."""

from __future__ import annotations

import json
import re
import socket
import tempfile
import threading
from pathlib import Path

import numpy as np
import pytest

from gourdsworth.net import (
    ALLOWED_GESTURES,
    DEFAULT_HOST,
    DOWNLINK_FORMAT,
    MAX_PAYLOAD,
    PROTO,
    UPLINK_CHUNK_BYTES,
    UPLINK_CHUNK_MS,
    UPLINK_CHUNK_SAMPLES,
    UPLINK_FORMAT,
    UPLINK_RATE,
    CrateConnection,
    CrateListener,
    encode_header,
    f32_to_le_bytes,
    recv_msg,
    resolve_bind,
    run_echo_session,
    s16le_to_f32,
    send_msg,
)


def test_config_crate_defaults_are_localhost():
    from gourdsworth.config import DEFAULTS

    crate = DEFAULTS["crate"]
    assert crate["host"] == "127.0.0.1"
    assert crate["port"] == 8746
    assert crate["enabled"] is False
    assert crate["allow_lan"] is False


def test_uplink_chunk_size_is_100ms_s16le_16k_mono():
    assert UPLINK_RATE == 16000
    assert UPLINK_FORMAT == "s16le"
    assert UPLINK_CHUNK_MS == 100
    assert UPLINK_CHUNK_SAMPLES == 1600
    assert UPLINK_CHUNK_BYTES == 3200
    assert UPLINK_CHUNK_BYTES == UPLINK_RATE * UPLINK_CHUNK_MS // 1000 * 2


def test_default_bind_is_loopback():
    assert DEFAULT_HOST == "127.0.0.1"
    assert resolve_bind(None, allow_lan=False) == "127.0.0.1"
    assert resolve_bind("localhost", allow_lan=False) == "127.0.0.1"
    assert resolve_bind("127.0.0.1", allow_lan=False) == "127.0.0.1"


def test_lan_bind_refused_without_allow():
    with pytest.raises(ValueError, match="allow_lan"):
        resolve_bind("0.0.0.0", allow_lan=False)
    with pytest.raises(ValueError, match="allow_lan"):
        resolve_bind("192.168.1.5", allow_lan=False)


def test_lan_bind_opt_in():
    assert resolve_bind("0.0.0.0", allow_lan=True) == "0.0.0.0"
    assert resolve_bind("192.168.1.5", allow_lan=True) == "192.168.1.5"


def test_json_control_roundtrip_socketpair():
    a, b = socket.socketpair()
    try:
        send_msg(a, {"event": "button", "state": "down"})
        send_msg(a, {"event": "gesture", "name": "stamp"})
        buf = bytearray()
        h1, p1 = recv_msg(b, buf)
        h2, p2 = recv_msg(b, buf)
        assert h1 == {"event": "button", "state": "down"}
        assert p1 is None
        assert h2 == {"event": "gesture", "name": "stamp"}
        assert p2 is None
        still_header = json.loads(encode_header({"event": "still"}).decode().strip())
        assert "n" not in still_header
    finally:
        a.close()
        b.close()


def test_pcm_and_jpeg_length_prefixed_roundtrip():
    a, b = socket.socketpair()
    try:
        pcm = np.array([0, 16384, -16384, 32767, -32768], dtype="<i2").tobytes()
        jpeg = b"\xff\xd8in-ram-only\xff\xd9"
        send_msg(
            a,
            {
                "event": "pcm",
                "rate": UPLINK_RATE,
                "channels": 1,
                "format": UPLINK_FORMAT,
            },
            pcm,
        )
        send_msg(a, {"event": "jpeg"}, jpeg)
        buf = bytearray()
        h1, p1 = recv_msg(b, buf)
        h2, p2 = recv_msg(b, buf)
        assert h1["event"] == "pcm"
        assert h1["n"] == len(pcm)
        assert p1 == pcm
        assert np.frombuffer(p1, dtype="<i2").tolist() == [0, 16384, -16384, 32767, -32768]
        assert h2["event"] == "jpeg"
        assert p2 == jpeg
        assert buf == b""
    finally:
        a.close()
        b.close()


def test_s16le_to_f32_scale():
    raw = np.array([0, 32767, -32768], dtype="<i2").tobytes()
    f = s16le_to_f32(raw)
    assert f.dtype == np.float32
    assert f[0] == pytest.approx(0.0)
    assert f[1] == pytest.approx(32767 / 32768)
    assert f[2] == pytest.approx(-1.0)
    assert f32_to_le_bytes(f)  # round-trip helper does not raise


def test_oversize_payload_rejected():
    a, b = socket.socketpair()
    try:
        b.sendall(b'{"event":"jpeg","n":999999999}\n')
        with pytest.raises(ValueError, match="rejected"):
            recv_msg(a, bytearray())
        with pytest.raises(ValueError, match="exceeds"):
            send_msg(a, {"event": "jpeg"}, b"x" * (MAX_PAYLOAD + 1))
    finally:
        a.close()
        b.close()


def test_net_module_has_no_disk_or_tempfile_api():
    src = Path(__file__).resolve().parents[1] / "src" / "gourdsworth" / "net.py"
    text = src.read_text(encoding="utf-8")
    assert "tempfile" not in text
    assert "NamedTemporaryFile" not in text
    assert "mkstemp" not in text
    assert "write_bytes" not in text
    assert re.search(r"open\s*\(", text) is None
    assert ".wav" not in text
    assert ".jpg" not in text or "JPEG" in text


def _start_echo(gesture: str = "stamp") -> tuple[CrateListener, threading.Thread]:
    listener = CrateListener("127.0.0.1", 0)

    def server() -> None:
        conn = listener.accept()
        try:
            run_echo_session(conn, gesture=gesture)
        finally:
            conn.close()

    t = threading.Thread(target=server, name="crate-echo-test", daemon=True)
    t.start()
    return listener, t


def test_tcp_loopback_fake_client_talk_roundtrip(tmp_path, monkeypatch):
    """One fake in-process client: button + PCM + JPEG → play + gesture.

    No mic, speaker, camera, or models. Asserts RAM-only (no temp files).
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    listener, thread = _start_echo(gesture="wave")
    try:
        sock = socket.create_connection(("127.0.0.1", listener.port), timeout=3)
        client = CrateConnection(sock)
        try:
            client.send({"event": "hello", "role": "crate", "proto": PROTO})
            hello = client.wait_event("hello", timeout=3)
            assert hello is not None
            assert hello[0]["role"] == "desktop"
            ready = client.wait_event("ready", timeout=3)
            assert ready is not None

            tone = (
                (0.25 * np.sin(2 * np.pi * np.arange(UPLINK_CHUNK_SAMPLES) / 40))
                .astype(np.float32)
            )
            pcm = (tone * 32767).astype("<i2").tobytes()
            jpeg = b"\xff\xd8fake-porch-still\xff\xd9"

            client.send({"event": "jpeg"}, jpeg)
            client.send({"event": "button", "state": "down"})
            client.send(
                {
                    "event": "pcm",
                    "rate": UPLINK_RATE,
                    "channels": 1,
                    "format": UPLINK_FORMAT,
                },
                pcm,
            )
            client.send({"event": "button", "state": "up"})

            play = client.wait_event("play", timeout=4)
            assert play is not None
            header, payload = play
            assert header["event"] == "play"
            assert header["format"] == DOWNLINK_FORMAT
            assert header["rate"] == UPLINK_RATE
            assert payload is not None and len(payload) >= 4
            played = np.frombuffer(payload, dtype="<f4")
            assert played.dtype == np.float32
            # Echo of the sine should be in a similar ballpark, not zeros.
            assert float(np.max(np.abs(played))) > 0.05
            client.send({"event": "play_done"})

            gest = client.wait_event("gesture", timeout=3)
            assert gest is not None
            assert gest[0]["name"] == "wave"
            assert gest[0]["name"] in ALLOWED_GESTURES
            ready2 = client.wait_event("ready", timeout=3)
            assert ready2 is not None
        finally:
            client.close()
        thread.join(timeout=3)
        assert list(tmp_path.iterdir()) == []
    finally:
        listener.close()


def test_button_events_match_issue_shape():
    a, b = socket.socketpair()
    try:
        send_msg(a, {"event": "button", "state": "down"})
        send_msg(a, {"event": "button", "state": "up"})
        send_msg(a, {"event": "still"})
        buf = bytearray()
        down, _ = recv_msg(b, buf)
        up, _ = recv_msg(b, buf)
        still, payload = recv_msg(b, buf)
        assert down == {"event": "button", "state": "down"}
        assert up == {"event": "button", "state": "up"}
        assert still == {"event": "still"}
        assert payload is None
    finally:
        a.close()
        b.close()


def test_help_lists_serve_crate(capsys):
    from gourdsworth.app import main

    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--serve-crate" in out
    assert "--crate-echo" in out


def test_serve_crate_rejects_dry_run():
    from gourdsworth.app import main

    assert main(["--serve-crate", "--dry-run"]) == 2
