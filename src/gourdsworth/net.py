"""Crate TCP protocol: Pi I/O client ↔ desktop model runner.

Inference (STT / LLM / TTS / CLIP) stays on the desktop. The Pi sends
microphone PCM, optional JPEG stills, and button events; it receives TTS
PCM and gesture names. Children's PCM, JPEG, and transcripts stay in RAM
and are dropped after the turn. Nothing is written to disk.

Wire format (one TCP connection; a validated same-IP or trusted client may replace the session)
------------------------------------------------------
Control is JSON lines. A binary blob (PCM or JPEG) is length-prefixed by
the JSON line's ``n`` field, then that many raw bytes with **no** extra
delimiter:

    <json-object>\\n
    <n bytes payload if n > 0>

JSON is compact UTF-8, one object per line. ``json.dumps`` escapes
newlines inside strings, so the first ``\\n`` always ends the header.

PCM uplink (Pi → desktop)
    event:    "pcm"
    rate:     16000
    channels: 1
    format:   "s16le"   signed 16-bit little-endian mono
    chunk:    100 ms = 1600 samples = 3200 bytes  (UPLINK_CHUNK_BYTES)
    Desktop concatenates chunks during a Talk. Other chunk sizes are
    accepted; 100 ms matches the local sounddevice block size.

PCM / float downlink (desktop → Pi, TTS)
    event:    "play"
    rate:     native Piper rate (often 22050)
    channels: 1
    format:   "f32le"  (default) or "s16le"
    payload:  raw little-endian samples for that buffer
    Pi plays, then replies ``{"event":"play_done"}``.

JPEG still (Pi → desktop)
    event:    "jpeg"   (also accepts "still" with n > 0)
    payload:  JPEG bytes, one frame, RAM only. Typical Talk still is
              640×480. Capped at MAX_PAYLOAD (2 MiB).

Control JSON (no payload)
    {"event":"hello","role":"crate"|"desktop","proto":1}
    {"event":"button","state":"down"|"up"}
    {"event":"still"}                 # optional hint; JPEG may follow
    {"event":"listen"|"thinking"|"speaking"|"ready"}
    {"event":"gesture","name":"tip"|"beam"|"reckon"|"chuckle"|"attend"|"twirl"}
    {"event":"play_done"}
    {"event":"bye"}
    {"event":"error","message":"..."}       # "busy" = valid hello refused (other peer or rate limit)
    {"event":"superseded"}                  # desktop → previous crate; that client should back off
    {"event":"telemetry","cpu_temp_c":47.1}
        # Pi → desktop, lightweight porch health. Optional; omitted fields mean
        # unknown. Cached on CrateConnection.latest_telemetry; does not enter
        # the PCM / JPEG / wait_event queues. cpu_temp_c is Celsius float.


Default bind is 127.0.0.1. Binding a LAN address or 0.0.0.0 requires
``crate.allow_lan: true`` — anyone on that network could then read kids'
PCM/JPEG. There is no TLS and no auth. A hello must finish within
``HELLO_DEADLINE_S`` for the whole read, and it must not carry a payload.
When ``crate.trusted_peers`` / ``CRATE_TRUSTED_PEERS`` is non-empty it is
an allowlist: other addresses are closed at accept, even if the slot is
empty. The list should contain only the Pi. A live or not-yet-served
session is replaced only by the same IP or a trusted IP. History stays
only with that exact same IP.
"""

from __future__ import annotations

from dataclasses import dataclass

import errno
import ipaddress
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

PROTO = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8746
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

UPLINK_RATE = 16000
UPLINK_CHANNELS = 1
UPLINK_FORMAT = "s16le"
UPLINK_CHUNK_MS = 100
UPLINK_CHUNK_SAMPLES = UPLINK_RATE * UPLINK_CHUNK_MS // 1000  # 1600
UPLINK_CHUNK_BYTES = UPLINK_CHUNK_SAMPLES * 2  # 3200

DOWNLINK_FORMAT = "f32le"
ALLOWED_GESTURES = frozenset({"tip", "beam", "reckon", "chuckle", "attend", "twirl"})
DEFAULT_GESTURE = "attend"

MAX_LINE = 16 * 1024
MAX_PAYLOAD = 2 * 1024 * 1024
MAX_EVENTS = 64

_RECV_TIMEOUT_S = 0.4

# Room for a reconnect (and a CLOSE-WAIT left in the queue) while a session
# is still open. The desktop serves one Pi; see serve_newest.
LISTEN_BACKLOG = 4

# Dead-peer probes. With TCP_USER_TIMEOUT set, Linux aborts the socket at
# about 30s (TCP_USER_TIMEOUT governs), including an idle black hole.
# Keepalive still starts probes after TCP_KEEPIDLE_S; it does not cut the
# connection at KEEPIDLE + KEEPCNT*KEEPINTVL while user-timeout is set.
TCP_KEEPIDLE_S = 10
TCP_KEEPINTVL_S = 5
TCP_KEEPCNT = 3
TCP_USER_TIMEOUT_MS = 30_000

# A newcomer must send a crate hello before it can be served or replace anyone.
# One absolute deadline for the whole hello, not a fresh timeout per recv.
HELLO_DEADLINE_S = 3.0
# Hello line cap. A longer line, or any declared payload, is rejected unread.
HELLO_MAX_BYTES = 1024
# Floor so settimeout(0) does not turn the last slice into a busy poll.
# The deadline is still checked before every recv.
_HELLO_RECV_FLOOR_S = 0.02
# Not-yet-validated hellos. Over-cap sockets are closed at accept.
HELLO_PENDING_MAX = 16
HELLO_PENDING_PER_IP = 2
# At most one replacement of an active or pending session per IP per this interval.
REPLACE_MIN_INTERVAL_S = 5.0
# Connects per IP before a hello thread is started. Excess sockets are closed.
CONNECT_RATE_MAX = 8
CONNECT_RATE_WINDOW_S = 10.0
# First pause after a transient accept() error. Later pauses double up to the cap.
ACCEPT_RETRY_S = 0.2
ACCEPT_BACKOFF_MAX_S = 2.0
# One accept-error or refusal line per this interval, per kind.
LOG_THROTTLE_S = 10.0
# Best-effort write of superseded / busy. Never block the hello thread on it.
SUPERSEDE_SEND_TIMEOUT_S = 0.5

_TRUSTED_SPLIT = re.compile(r"[\s,]+")

_TRANSIENT_ACCEPT_ERRNOS = frozenset(
    {
        errno.ECONNABORTED,
        errno.EMFILE,
        errno.ENFILE,
        errno.EINTR,
    }
)

_sockopt_warn_lock = threading.Lock()
_sockopt_warned = False


def parse_thermal_sysfs_temp(raw: str | bytes | None) -> float | None:
    """Parse `/sys/class/thermal/*/temp` millidegree text into Celsius.

    Returns None when the reading is missing or not a plausible CPU temp.
    """
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii", errors="ignore")
        except Exception:
            return None
    try:
        milli = int(str(raw).strip().split()[0])
    except (TypeError, ValueError, IndexError):
        return None
    if milli <= 0 or milli > 200_000:
        return None
    return milli / 1000.0




def is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def _warn_sockopt_once(name: str, exc: OSError) -> None:
    """Log the first socket-option failure in this process. Later ones stay quiet."""
    global _sockopt_warned
    with _sockopt_warn_lock:
        if _sockopt_warned:
            return
        _sockopt_warned = True
    print(
        f"warning: crate socket option {name} failed to set ({exc}); "
        "dead-peer detection may fall back to kernel defaults",
        file=sys.stderr,
        flush=True,
    )


def _setsockopt_if(sock: socket.socket, level: int, name: str, value: int) -> None:
    """Set a socket option when this platform defines it. Warn once on OSError."""
    opt = getattr(socket, name, None)
    if opt is None:
        return
    try:
        sock.setsockopt(level, opt, int(value))
    except OSError as exc:
        _warn_sockopt_once(name, exc)


def enable_tcp_keepalive(sock: socket.socket) -> None:
    """Bound dead-peer detection to about 30s without an app read timeout.

    With TCP_USER_TIMEOUT set, Linux aborts the connection at about 30s
    (TCP_USER_TIMEOUT governs). Keepalive probes start after TCP_KEEPIDLE_S.
    They do not drop the socket at KEEPIDLE + KEEPCNT*KEEPINTVL while
    user-timeout is set. If payload stays unacked, user-timeout also forces
    ETIMEDOUT.

    No application heartbeat. PTT can sit with no button forever, and the
    desktop sends nothing during that idle. The Pi's telemetry event is
    optional, starts only after "ready", and is every 15s — an idle cutoff
    would drop a healthy porch session and any older peer that never sends
    telemetry. Long TTS playback and LLM turns are quiet on the desktop
    side except for the audio already on the wire, which user-timeout
    covers if those bytes stop being acked. The recv thread turns the
    resulting OSError into CrateConnection.closed so the session loop
    returns. Applied from CrateConnection.__init__, so both the accepted
    server socket and the Pi client's outbound socket get it.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError as exc:
        _warn_sockopt_once("SO_KEEPALIVE", exc)
    _setsockopt_if(sock, socket.IPPROTO_TCP, "TCP_KEEPIDLE", TCP_KEEPIDLE_S)
    _setsockopt_if(sock, socket.IPPROTO_TCP, "TCP_KEEPINTVL", TCP_KEEPINTVL_S)
    _setsockopt_if(sock, socket.IPPROTO_TCP, "TCP_KEEPCNT", TCP_KEEPCNT)
    _setsockopt_if(sock, socket.IPPROTO_TCP, "TCP_USER_TIMEOUT", TCP_USER_TIMEOUT_MS)


def resolve_bind(host: str | None, allow_lan: bool) -> str:
    """Return a bind address, or raise if a LAN bind was not opted into."""
    host = (host or DEFAULT_HOST).strip() or DEFAULT_HOST
    if is_loopback(host):
        return "127.0.0.1" if host.lower() == "localhost" else host
    if not allow_lan:
        raise ValueError(
            f"Refusing to bind crate to {host!r}. Default is {DEFAULT_HOST}. "
            "Set crate.allow_lan: true only on a trusted home LAN "
            "(children's PCM/JPEG would be reachable by anyone on that "
            "network; there is no TLS and no auth)."
        )
    return host


class TrustedPeersError(ValueError):
    """A trusted-peer entry is not an IP address. Startup must not continue."""


def normalize_peer_ip(value: Any) -> str | None:
    """Return a canonical IP string, or None when ``value`` is not an address.

    IPv4-mapped IPv6 (``::ffff:a.b.c.d``) becomes the IPv4 address so a
    trusted entry and a peer address compare equal.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    return str(parsed)


def _partition_trusted(value: Any) -> tuple[set[str], list[str]]:
    """Split trusted-peer text into normalized addresses and rejected tokens."""
    good: set[str] = set()
    bad: list[str] = []
    if isinstance(value, str):
        tokens = [tok for tok in _TRUSTED_SPLIT.split(value) if tok]
    elif isinstance(value, (list, tuple, set, frozenset)):
        tokens = []
        for item in value:
            if isinstance(item, str):
                tokens.extend(tok for tok in _TRUSTED_SPLIT.split(item) if tok)
            elif item is not None:
                bad.append(str(item))
    else:
        return good, bad
    for tok in tokens:
        norm = normalize_peer_ip(tok)
        if norm is None:
            bad.append(tok)
        else:
            good.add(norm)
    return good, bad


def _reject_bad_trusted(bad: list[str]) -> None:
    if not bad:
        return
    listed = ", ".join(bad)
    raise TrustedPeersError(
        "Refusing to start: invalid trusted peer entries "
        f"(not IP addresses): {listed}"
    )


def parse_trusted_peers(value: Any) -> frozenset[str]:
    """Parse ``crate.trusted_peers`` (list or comma/whitespace string).

    CIDR, hostnames, ``*``, and other non-addresses raise
    ``TrustedPeersError`` and name those entries. IPv4-mapped IPv6 is stored
    as IPv4. An empty value is an empty allowlist.
    """
    good, bad = _partition_trusted(value)
    _reject_bad_trusted(bad)
    return frozenset(good)


def load_trusted_peers(config_value: Any = None, *, env: str | None = None) -> frozenset[str]:
    """Merge config and ``CRATE_TRUSTED_PEERS`` once.

    ``env is None`` reads the environment. Pass ``env=""`` to skip it.
    Any entry that is not an IP raises ``TrustedPeersError`` naming the bad
    entries; startup must not continue with a shorter list. Call this at
    startup and keep the returned set; do not re-read per connection.
    """
    if env is None:
        env = os.environ.get("CRATE_TRUSTED_PEERS", "")
    good, bad = _partition_trusted(config_value)
    good_env, bad_env = _partition_trusted(env)
    _reject_bad_trusted(bad + bad_env)
    return frozenset(good) | frozenset(good_env)


def carry_history_for_peer(
    history: list,
    peer_ip: str,
    previous_ip: str | None,
    trusted: set[str] | frozenset[str] | None = None,
) -> str:
    """Keep history only when the new peer IP is exactly the previous one.

    A different IP starts empty, including a different trusted IP. Trust
    decides who may replace a session; it does not carry the conversation.
    ``trusted`` is accepted so existing callers keep working and is ignored.
    ``previous_ip`` is None until the first served session records a peer.
    Returns the normalized peer IP for the caller to store. The turn cap
    stays with the caller.
    """
    del trusted
    ip = normalize_peer_ip(peer_ip) or str(peer_ip or "").strip()
    if previous_ip:
        prev = normalize_peer_ip(previous_ip) or str(previous_ip).strip()
    else:
        prev = ""
    if prev and ip != prev:
        history.clear()
    return ip


def encode_header(header: dict[str, Any], payload: bytes | None = None) -> bytes:
    msg = dict(header)
    if payload:
        msg["n"] = len(payload)
    else:
        msg.pop("n", None)
    line = json.dumps(msg, separators=(",", ":"), ensure_ascii=True)
    if "\n" in line:
        raise ValueError("crate JSON header must be a single line")
    return (line + "\n").encode("utf-8")


def send_msg(sock: socket.socket, header: dict[str, Any], payload: bytes | None = None) -> None:
    """Write one JSON line and optional payload. Never writes to disk."""
    if payload is not None and len(payload) > MAX_PAYLOAD:
        raise ValueError(f"crate payload {len(payload)} exceeds {MAX_PAYLOAD} bytes")
    blob = encode_header(header, payload)
    if payload:
        sock.sendall(blob + payload)
    else:
        sock.sendall(blob)


def recv_msg(sock: socket.socket, buf: bytearray) -> tuple[dict[str, Any], bytes | None]:
    """Read one JSON line plus ``n`` payload bytes into RAM. Mutates ``buf``."""
    while True:
        nl = buf.find(b"\n")
        if nl != -1:
            break
        if len(buf) > MAX_LINE:
            raise ValueError("crate JSON line too long")
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("crate connection closed")
        buf.extend(chunk)
    if nl > MAX_LINE:
        raise ValueError("crate JSON line too long")
    line = bytes(buf[:nl])
    del buf[: nl + 1]
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"crate JSON header is not valid JSON: {exc}") from exc
    if not isinstance(header, dict) or "event" not in header:
        raise ValueError("crate message missing event")
    n = int(header.get("n") or 0)
    if n < 0 or n > MAX_PAYLOAD:
        raise ValueError(f"crate payload length {n} rejected (max {MAX_PAYLOAD})")
    while len(buf) < n:
        chunk = sock.recv(max(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("crate connection closed mid-payload")
        buf.extend(chunk)
    if n == 0:
        return header, None
    payload = bytes(buf[:n])
    del buf[:n]
    return header, payload


def s16le_to_f32(payload: bytes):
    """Uplink s16le bytes → float32 ndarray in [-1, 1]. RAM only."""
    import numpy as np

    if not payload:
        return np.zeros(1, dtype=np.float32)
    n = len(payload) // 2
    raw = np.frombuffer(payload, dtype="<i2", count=n)
    return raw.astype(np.float32) / 32768.0


def f32_to_le_bytes(samples) -> bytes:
    import numpy as np

    return np.asarray(samples, dtype="<f4").reshape(-1).tobytes()



@dataclass
class AdaptiveVad:
    """Noise-floor + speech-peak VAD for porch groups.

    Fixed absolute thresholds fail when kids chatter in the background.
    This tracks a slow ambient floor and ends an utterance when level
    falls back toward that floor *or* drops from the utterance peak.
    """

    # Minimum absolute start (config energy_threshold); avoids triggering on hush
    absolute_start: float = 0.012
    floor_min: float = 0.0015
    start_ratio: float = 2.0
    end_ratio: float = 1.35
    start_margin: float = 0.012
    end_margin: float = 0.006
    peak_end_ratio: float = 0.32
    floor_alpha_idle: float = 0.08
    floor_alpha_speech: float = 0.015

    floor: float = 0.008
    peak: float = 0.0
    voiced: bool = False

    def reset_utterance(self) -> None:
        self.peak = 0.0
        self.voiced = False

    def _update_floor(self, level: float) -> None:
        alpha = self.floor_alpha_speech if self.voiced else self.floor_alpha_idle
        # Pull floor toward quieter readings; creep up slowly if ambient rose
        target = max(float(self.floor_min), float(level))
        if level <= self.floor * 1.25 or not self.voiced:
            self.floor = (1.0 - alpha) * self.floor + alpha * target
        elif level < self.floor:
            self.floor = level
        self.floor = max(float(self.floor_min), float(self.floor))

    def start_threshold(self) -> float:
        return max(
            float(self.absolute_start),
            float(self.floor) * float(self.start_ratio),
            float(self.floor) + float(self.start_margin),
        )

    def end_threshold(self) -> float:
        floor_end = max(
            float(self.floor) * float(self.end_ratio),
            float(self.floor) + float(self.end_margin),
        )
        if self.peak > 0:
            peak_end = float(self.peak) * float(self.peak_end_ratio)
            # End when below the higher of floor-relative and peak-relative
            # (peak drop catches "loud kid stopped, group still murmuring")
            return max(floor_end, min(peak_end, self.peak * 0.9))
        return floor_end

    def observe(self, level: float) -> bool:
        """Return True if this chunk counts as voiced speech."""
        level = float(level)
        self._update_floor(level)
        if not self.voiced:
            if level >= self.start_threshold():
                self.voiced = True
                self.peak = level
                return True
            return False
        self.peak = max(self.peak, level)
        if level <= self.end_threshold():
            return False
        return True


def _rms(frame) -> float:
    import numpy as np

    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


class CrateConnection:
    """One accepted (or outbound) crate TCP socket with a recv thread."""

    def __init__(
        self,
        sock: socket.socket,
        peer: tuple[str, int] | None = None,
        *,
        recv_prebuffer: bytes = b"",
        primed: tuple[dict[str, Any], bytes | None] | None = None,
    ):
        self.sock = sock
        self.peer = peer
        # IP used for replace / history decisions. A test hook may overwrite it.
        self.policy_ip = peer[0] if peer else ""
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            _warn_sockopt_once("TCP_NODELAY", exc)
        # Server accept and the Pi outbound socket both construct this class.
        enable_tcp_keepalive(sock)
        sock.settimeout(_RECV_TIMEOUT_S)
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        # Set when a later validated client replaces this socket.
        # An attribute on the object, not id(), so a recycled id cannot
        # mark a different connection as superseded.
        self.superseded = False
        self._last_err: BaseException | None = None
        self._gate = threading.Lock()
        self._listening = False
        self._pcm: queue.Queue[bytes] = queue.Queue()
        self._jpeg_lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._telemetry_lock = threading.Lock()
        self._latest_telemetry: dict[str, Any] | None = None
        self._button_down = threading.Event()
        self._button_up = threading.Event()
        self._cv = threading.Condition()
        self._events: deque[tuple[dict[str, Any], bytes | None]] = deque()
        # Bytes already read with the hello, so the recv thread does not lose them.
        self._recv_prebuffer = bytes(recv_prebuffer)
        if primed is not None:
            self._push_event(primed[0], primed[1])
        self._thread = threading.Thread(
            target=self._recv_loop, name="gourdsworth-crate-recv", daemon=True
        )
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send(self, header: dict[str, Any], payload: bytes | None = None) -> None:
        if self._closed.is_set():
            raise ConnectionError("crate connection closed")
        with self._send_lock:
            send_msg(self.sock, header, payload)

    def close(self) -> None:
        self._closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        with self._cv:
            self._cv.notify_all()

    def wake_superseded(self) -> None:
        """Mark this session replaced and wake its thread. Do not close the socket.

        The hello thread calls this. The session thread's ``finally`` calls
        ``close()``. Sending ``superseded`` is best-effort and must not block
        if the session thread holds the send lock.
        """
        self.superseded = True
        try:
            self.sock.settimeout(SUPERSEDE_SEND_TIMEOUT_S)
        except OSError:
            pass
        acquired = self._send_lock.acquire(timeout=SUPERSEDE_SEND_TIMEOUT_S)
        if acquired:
            try:
                if not self._closed.is_set():
                    send_msg(self.sock, {"event": "superseded"})
            except OSError:
                pass
            finally:
                self._send_lock.release()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def wait_event(
        self, name: str, timeout: float | None = None
    ) -> tuple[dict[str, Any], bytes | None] | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                for i, item in enumerate(self._events):
                    if item[0].get("event") == name:
                        del self._events[i]
                        return item
                if self._closed.is_set():
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._cv.wait(0.1 if remaining is None else min(0.1, remaining))

    def wait_any(
        self, timeout: float | None = None
    ) -> tuple[dict[str, Any], bytes | None] | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                if self._events:
                    return self._events.popleft()
                if self._closed.is_set():
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._cv.wait(0.1 if remaining is None else min(0.1, remaining))

    def wait_button(self, state: str, timeout: float | None = None) -> bool:
        flag = self._button_down if state == "down" else self._button_up
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._closed.is_set():
                return False
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            slice_t = 0.2 if remaining is None else min(0.2, remaining)
            if flag.wait(timeout=slice_t):
                return True

    def take_jpeg(self) -> bytes | None:
        with self._jpeg_lock:
            jpeg = self._latest_jpeg
            self._latest_jpeg = None
            return jpeg

    def peek_jpeg(self) -> bool:
        with self._jpeg_lock:
            return self._latest_jpeg is not None

    @property
    def latest_telemetry(self) -> dict[str, Any] | None:
        """Most recent Pi telemetry event header (RAM cache; may be stale)."""
        with self._telemetry_lock:
            if self._latest_telemetry is None:
                return None
            return dict(self._latest_telemetry)

    def wait_jpeg(self, timeout_s: float = 2.0) -> bytes | None:
        """Wait briefly for a speech-triggered still (Pi encode is ~1–2s)."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            jpeg = self.take_jpeg()
            if jpeg is not None:
                return jpeg
            if time.monotonic() >= deadline or self._closed.is_set():
                return None
            time.sleep(0.05)

    def arm_listen(self, *, flush: bool = True) -> None:
        """Start accepting PCM without a fresh button-down (continuous porch)."""
        with self._gate:
            if flush:
                self._flush_pcm_unlocked()
            self._listening = True
            self._button_up.clear()
            self._button_down.set()

    def reset_talk_latch(self) -> None:
        self._button_down.clear()
        self._button_up.clear()
        with self._gate:
            self._listening = False
            self._flush_pcm_unlocked()

    def listen_pcm(
        self,
        *,
        sample_rate: int = UPLINK_RATE,
        limit_s: float = 8.0,
        mode: str = "ptt",
        silence_s: float = 0.55,
        energy_threshold: float = 0.012,
        min_voiced_s: float = 0.35,
        keep_listening: bool = False,
    ):
        """Collect uplink PCM until button-up, VAD silence, or ``limit_s``.

        VAD is adaptive: tracks ambient noise floor and utterance peak so
        noisy porch groups can still end a turn when the speaker pauses.
        ``energy_threshold`` is the minimum absolute start gate.

        Returns ``(float32 ndarray, record_ms, uplink_first_ms, uplink_jitter_ms, had_voice)``.
        ``had_voice`` is true only if sustained energy lasted at least ``min_voiced_s``
        (filters mic noise blips). Drops s16le chunks as converted; caller ``del``s audio.
        """
        import numpy as np

        with self._gate:
            # Do not flush here — button-down / arm_listen already did; PCM may be queued.
            self._listening = True
        t0 = time.perf_counter()
        speech_t0 = t0
        chunks: list = []
        voiced = False
        voiced_s = 0.0
        silent_run = 0.0
        vad = AdaptiveVad(absolute_start=float(energy_threshold))
        block_s = UPLINK_CHUNK_MS / 1000.0
        uplink_first_ms = 0.0
        uplink_jitter_ms = 0.0
        last_chunk_t: float | None = None
        while True:
            if self._closed.is_set():
                break
            # PTT / one-shot: hard cap from button-down. VAD: wait forever for first
            # voice (porch continuous), then cap speech length from first voice.
            if mode != "vad" and (time.perf_counter() - t0) >= limit_s:
                break
            if mode == "vad" and voiced and (time.perf_counter() - speech_t0) >= limit_s:
                break
            try:
                payload = self._pcm.get(timeout=0.05)
            except queue.Empty:
                if self._button_up.is_set() and self._pcm.empty():
                    break
                if (not self._listening) and self._pcm.empty():
                    break
                continue
            now = time.perf_counter()
            if uplink_first_ms <= 0:
                uplink_first_ms = (now - t0) * 1000.0
            if last_chunk_t is not None:
                gap_ms = (now - last_chunk_t) * 1000.0
                if gap_ms > uplink_jitter_ms:
                    uplink_jitter_ms = gap_ms
            last_chunk_t = now
            chunk = s16le_to_f32(payload)
            del payload
            chunks.append(chunk)
            if mode == "vad":
                level = _rms(chunk)
                speech_now = vad.observe(level)
                if speech_now:
                    if not voiced:
                        speech_t0 = now
                    voiced = True
                    voiced_s += block_s
                    silent_run = 0.0
                elif voiced:
                    silent_run += block_s
                    if silent_run >= silence_s:
                        break
        with self._gate:
            if not keep_listening:
                self._listening = False
                self._flush_pcm_unlocked()
            # else: stay armed so continuous porch keeps buffering during STT/LLM
        if chunks:
            audio = np.concatenate(chunks)
        else:
            audio = np.zeros(1, dtype=np.float32)
        del chunks
        record_ms = (time.perf_counter() - t0) * 1000.0
        real_voice = bool(voiced) and (voiced_s >= float(min_voiced_s))
        return audio, record_ms, uplink_first_ms, uplink_jitter_ms, real_voice

    def play_float(self, samples, rate: int) -> float:
        """Send one TTS buffer as f32le and wait for play_done (or duration)."""
        import numpy as np

        t0 = time.perf_counter()
        raw = np.asarray(samples, dtype=np.float32).reshape(-1)
        if raw.size == 0 or not rate:
            return 0.0
        payload = f32_to_le_bytes(raw)
        duration = float(raw.size) / float(rate)
        del raw
        self.send(
            {
                "event": "play",
                "rate": int(rate),
                "channels": 1,
                "format": DOWNLINK_FORMAT,
            },
            payload,
        )
        del payload
        self.wait_event("play_done", timeout=max(1.0, duration + 2.0))
        return (time.perf_counter() - t0) * 1000.0

    def handshake(self, role: str, timeout: float = 8.0) -> dict[str, Any] | None:
        self.send({"event": "hello", "role": role, "proto": PROTO})
        got = self.wait_event("hello", timeout=timeout)
        if got is None:
            return None
        return got[0]

    def _flush_pcm_unlocked(self) -> None:
        while True:
            try:
                self._pcm.get_nowait()
            except queue.Empty:
                return

    def _push_event(self, header: dict[str, Any], payload: bytes | None) -> None:
        with self._cv:
            self._events.append((header, payload))
            while len(self._events) > MAX_EVENTS:
                self._events.popleft()
            self._cv.notify_all()

    def _dispatch(self, header: dict[str, Any], payload: bytes | None) -> None:
        ev = header.get("event")
        if ev == "pcm":
            if not payload:
                return
            with self._gate:
                if self._listening:
                    self._pcm.put(payload)
            return
        if ev in {"jpeg", "still"} and payload:
            with self._jpeg_lock:
                self._latest_jpeg = payload
            return
        if ev == "button":
            state = header.get("state")
            with self._gate:
                if state == "down":
                    self._flush_pcm_unlocked()
                    self._listening = True
                    self._button_up.clear()
                    self._button_down.set()
                elif state == "up":
                    self._listening = False
                    self._button_up.set()
            self._push_event(header, None)
            return
        if ev == "telemetry":
            # Cache only — do not enqueue (keeps wait_event / wait_any free of noise).
            with self._telemetry_lock:
                self._latest_telemetry = dict(header)
            return
        self._push_event(header, payload)

    def _recv_loop(self) -> None:
        buf = bytearray(self._recv_prebuffer)
        try:
            while not self._closed.is_set():
                try:
                    header, payload = recv_msg(self.sock, buf)
                except socket.timeout:
                    continue
                self._dispatch(header, payload)
        except (ConnectionError, OSError, ValueError) as exc:
            self._last_err = exc
        finally:
            self._closed.set()
            with self._cv:
                self._cv.notify_all()


class ListenerClosed(OSError):
    """accept() returned because the listener was closed. Clean shutdown."""


class CrateListener:
    """TCP listener. Default bind is loopback.

    Backlog is LISTEN_BACKLOG so a new handshake can finish while an old
    socket is still in the session. ``serve_newest`` keeps accepting and
    serves a client only after a valid crate hello.
    """

    def __init__(self, host: str, port: int):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, int(port)))
        self._sock.listen(LISTEN_BACKLOG)
        self._sock.settimeout(0.5)
        self.host, self.port = self._sock.getsockname()[:2]
        self._closed = False

    def accept_socket(self) -> tuple[socket.socket, tuple]:
        """Return the next TCP socket. Does not start a crate session."""
        while not self._closed:
            try:
                return self._sock.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._closed:
                    raise ListenerClosed("crate listener closed") from exc
                raise
        raise ListenerClosed("crate listener closed")

    def accept(self) -> CrateConnection:
        sock, addr = self.accept_socket()
        return CrateConnection(sock, peer=addr)

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


def run_echo_session(conn: CrateConnection, *, gesture: str = "tip") -> None:
    """Protocol smoke: hello, then Talk → echo PCM as play + a gesture.

    Stays on the connection until the client drops. Does not run STT/LLM/TTS.
    Used by ``--crate-echo`` and unit tests.
    """
    conn.send({"event": "hello", "role": "desktop", "proto": PROTO})
    conn.send({"event": "ready"})
    name = gesture if gesture in ALLOWED_GESTURES else DEFAULT_GESTURE
    while not conn.closed:
        if not conn.wait_button("down", timeout=0.5):
            continue
        if conn.closed:
            return
        conn.send({"event": "listen"})
        audio, _ms, _uf, _uj, _voiced = conn.listen_pcm(limit_s=4.0, mode="ptt")
        if conn.closed:
            return
        jpeg = conn.take_jpeg()
        if jpeg:
            del jpeg
        conn.send({"event": "speaking"})
        conn.play_float(audio, UPLINK_RATE)
        if conn.closed:
            return
        conn.send({"event": "gesture", "name": name})
        conn.reset_talk_latch()
        conn.send({"event": "ready"})


def _peer_text(conn: CrateConnection) -> str:
    peer = conn.peer
    if not peer:
        return "?"
    return f"{peer[0]}:{peer[1]}"


def _valid_crate_hello(header: dict[str, Any]) -> bool:
    return (
        header.get("event") == "hello"
        and header.get("role") == "crate"
        and header.get("proto") == PROTO
    )


def _read_crate_hello(sock: socket.socket) -> tuple[dict[str, Any], bytes] | None:
    """Read one crate hello, or None on timeout, close, garbage, or a payload.

    ``HELLO_DEADLINE_S`` is read when this runs so tests can shorten it.
    The deadline is absolute: each recv waits only for the time still left.
    The line is capped at ``HELLO_MAX_BYTES``. ``n`` > 0 is rejected without
    a further read. Bytes after the newline are returned for the recv thread.
    """
    deadline = time.monotonic() + float(HELLO_DEADLINE_S)
    cap = int(HELLO_MAX_BYTES)
    buf = bytearray()
    while True:
        nl = buf.find(b"\n")
        if nl != -1:
            break
        if len(buf) >= cap:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            sock.settimeout(max(_HELLO_RECV_FLOOR_S, remaining))
            chunk = sock.recv(cap - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    nl = buf.find(b"\n")
    if nl < 0 or nl > cap:
        return None
    line = bytes(buf[:nl])
    leftover = bytes(buf[nl + 1 :])
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(header, dict):
        return None
    if "n" in header:
        try:
            n = int(header.get("n") or 0)
        except (TypeError, ValueError):
            return None
        if n != 0:
            # Declared payload. Do not recv it.
            return None
    if not _valid_crate_hello(header):
        return None
    return header, leftover


def _close_socket(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _send_busy(sock: socket.socket) -> None:
    try:
        sock.settimeout(SUPERSEDE_SEND_TIMEOUT_S)
        send_msg(sock, {"event": "error", "message": "busy"})
    except OSError:
        pass


class _NewestAccept:
    """Accept on a side thread. Only a validated crate hello can be served.

    The accept loop never blocks on a newcomer: each socket is handed to a
    short-lived thread that reads the hello, unless the trusted list, the
    connect rate, or the pending-hello cap says to close it first. A live
    or not-yet-served session is replaced only for the same policy IP or a
    trusted peer, and at most once per ``REPLACE_MIN_INTERVAL_S`` for that
    IP. The hello thread wakes the old session with shutdown(); that
    session's thread is the one that close()s.
    """

    def __init__(
        self,
        listener: CrateListener,
        *,
        peer_ip: Callable[..., str] | None = None,
        trusted_peers: Any = None,
        trust_env: bool = True,
    ):
        self._listener = listener
        self._peer_ip = peer_ip
        # Parsed once. trust_env is false when the caller already merged the env.
        self._trusted = load_trusted_peers(
            trusted_peers, env=None if trust_env else ""
        )
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._pending: CrateConnection | None = None
        self._current: CrateConnection | None = None
        self._active = False
        self._connects: dict[str, deque[float]] = {}
        self._replaced_at: dict[str, float] = {}
        self._vet_socks: set[socket.socket] = set()
        self._hello_ips: dict[socket.socket, str] = {}
        self._vetters: set[threading.Thread] = set()
        self._accept_backoff = float(ACCEPT_RETRY_S)
        self._log_lock = threading.Lock()
        self._log_slots: dict[str, dict[str, Any]] = {}
        self.exit_code = 0
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="gourdsworth-crate-accept",
            daemon=True,
        )
        self._thread.start()

    def take(self, waiting_message: str | None = None) -> CrateConnection | None:
        """Block until the next connection to serve, or None on shutdown."""
        announced = False
        with self._cv:
            # Previous session has returned; don't treat a later connect as
            # a replacement of a session we are already done with.
            self._active = False
            self._current = None
            while self._pending is None and not self._stop.is_set():
                if waiting_message and not announced:
                    print(waiting_message, flush=True)
                    announced = True
                self._cv.wait(timeout=0.5)
            if self._pending is None:
                return None
            conn = self._pending
            self._pending = None
            self._current = conn
            self._active = True
            return conn

    def was_superseded(self, conn: CrateConnection) -> bool:
        return bool(getattr(conn, "superseded", False))

    def note_session_ended(self, conn: CrateConnection) -> None:
        """The session thread has left ``session_fn``. A new hello is not a replace."""
        with self._cv:
            if self._current is conn:
                self._current = None
                self._active = False

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        with self._cv:
            victims: list[CrateConnection] = []
            if self._current is not None:
                victims.append(self._current)
            if self._pending is not None and self._pending is not self._current:
                victims.append(self._pending)
            socks = list(self._vet_socks)
            vetters = [item for item in self._vetters if item is not threading.current_thread()]
            self._current = None
            self._pending = None
            self._active = False
            self._cv.notify_all()
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            _close_socket(sock)
        # Caller is the serve_newest thread, which owns these sessions.
        for conn in victims:
            conn.close()
        thread = self._thread
        if thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=2.0)
        for item in vetters:
            item.join(timeout=1.0)
        self._flush_limited_logs()

    def _allow_connect(self, ip: str) -> bool:
        now = time.monotonic()
        window = CONNECT_RATE_WINDOW_S
        limit = CONNECT_RATE_MAX
        if len(self._connects) > 256:
            stale = [
                key
                for key, bucket in self._connects.items()
                if not bucket or now - bucket[-1] > window
            ]
            for key in stale:
                self._connects.pop(key, None)
        bucket = self._connects.setdefault(ip, deque())
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def _policy_ip(self, addr: tuple) -> str:
        if self._peer_ip is None:
            raw = str(addr[0])
        else:
            raw = str(self._peer_ip(addr)).strip()
        return normalize_peer_ip(raw) or raw

    def _prune_replaced_locked(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        limit = float(REPLACE_MIN_INTERVAL_S)
        stale = [key for key, ts in self._replaced_at.items() if now - ts >= limit]
        for key in stale:
            self._replaced_at.pop(key, None)

    def _replacement_ok_locked(self, ip: str) -> bool:
        last = self._replaced_at.get(ip)
        if last is None:
            return True
        return (time.monotonic() - last) >= float(REPLACE_MIN_INTERVAL_S)

    def _pause_accept(self, delay: float) -> bool:
        """Wait out an accept backoff. True when the listener is stopping."""
        if delay <= 0:
            return self._stop.is_set()
        return self._stop.wait(delay)

    def _log_limited(self, kind: str, message: str) -> None:
        """Print at most one line per ``LOG_THROTTLE_S`` for ``kind``.

        The next line, or a flush, includes how many repeats were suppressed.
        """
        now = time.monotonic()
        interval = float(LOG_THROTTLE_S)
        emit: list[str] = []
        with self._log_lock:
            slot = self._log_slots.get(kind)
            if slot is not None and now - float(slot["ts"]) < interval:
                slot["suppressed"] = int(slot["suppressed"]) + 1
                slot["message"] = message
                return
            if slot is not None and int(slot["suppressed"]):
                emit.append(f"{slot['message']} ({slot['suppressed']} suppressed)")
            self._log_slots[kind] = {"ts": now, "suppressed": 0, "message": message}
            emit.append(message)
        for line in emit:
            print(line, file=sys.stderr, flush=True)

    def _flush_limited_logs(self) -> None:
        emit: list[str] = []
        with self._log_lock:
            for slot in self._log_slots.values():
                if int(slot["suppressed"]):
                    emit.append(f"{slot['message']} ({slot['suppressed']} suppressed)")
                    slot["suppressed"] = 0
        for line in emit:
            print(line, file=sys.stderr, flush=True)

    def _reserve_hello(self, sock: socket.socket, ip: str) -> str | None:
        """Reserve a not-yet-validated hello slot. Return a refusal reason or None."""
        with self._cv:
            self._prune_replaced_locked()
            per = sum(1 for item in self._hello_ips.values() if item == ip)
            if per >= int(HELLO_PENDING_PER_IP):
                return "per-ip"
            if len(self._hello_ips) >= int(HELLO_PENDING_MAX):
                return "global"
            self._hello_ips[sock] = ip
            self._vet_socks.add(sock)
            return None

    def _release_hello(self, sock: socket.socket) -> None:
        with self._cv:
            self._hello_ips.pop(sock, None)
            self._vet_socks.discard(sock)

    def _accept_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    sock, addr = self._listener.accept_socket()
                except ListenerClosed:
                    break
                except OSError as exc:
                    if self._stop.is_set() or self._listener._closed:
                        break
                    if exc.errno in _TRANSIENT_ACCEPT_ERRNOS:
                        self._log_limited(
                            f"accept-{exc.errno}",
                            f"crate accept transient error: {exc}",
                        )
                        delay = self._accept_backoff
                        grown = delay * 2
                        floor = float(ACCEPT_RETRY_S)
                        cap = float(ACCEPT_BACKOFF_MAX_S)
                        if grown < floor:
                            grown = floor
                        if grown > cap:
                            grown = cap
                        self._accept_backoff = grown
                        if self._pause_accept(delay):
                            break
                        continue
                    self._log_limited(
                        f"accept-fatal-{exc.errno}",
                        f"crate accept fatal error: {exc}",
                    )
                    with self._cv:
                        self.exit_code = 1
                        self._stop.set()
                        self._cv.notify_all()
                    break
                self._accept_backoff = float(ACCEPT_RETRY_S)
                try:
                    ip = self._policy_ip(addr)
                except Exception:
                    _close_socket(sock)
                    continue
                if not ip:
                    _close_socket(sock)
                    continue
                if self._trusted and ip not in self._trusted:
                    self._log_limited(
                        "untrusted",
                        f"crate untrusted: refused {ip} at accept",
                    )
                    _close_socket(sock)
                    continue
                if not self._allow_connect(ip):
                    self._log_limited("rate", f"crate rate limit: refused {ip}")
                    _close_socket(sock)
                    continue
                reason = self._reserve_hello(sock, ip)
                if reason is not None:
                    self._log_limited(
                        "pending",
                        f"crate pending cap: refused {ip} ({reason})",
                    )
                    _close_socket(sock)
                    continue
                self._spawn_vet(sock, addr, ip)
        finally:
            with self._cv:
                self._stop.set()
                self._cv.notify_all()
            self._flush_limited_logs()

    def _spawn_vet(self, sock: socket.socket, addr: tuple, ip: str) -> None:
        thread = threading.Thread(
            target=self._vet,
            args=(sock, addr, ip),
            name="gourdsworth-crate-hello",
            daemon=True,
        )
        with self._cv:
            self._vetters.add(thread)
        try:
            thread.start()
        except Exception as exc:
            # A failed start never runs _vet, so the reservation would leak
            # and the exception would stop the accept loop.
            with self._cv:
                self._vetters.discard(thread)
            self._release_hello(sock)
            _close_socket(sock)
            self._log_limited(
                "vet-start",
                f"crate hello thread failed to start for {ip}: {exc}",
            )

    def _vet(self, sock: socket.socket, addr: tuple, ip: str) -> None:
        conn: CrateConnection | None = None
        try:
            got = _read_crate_hello(sock)
            if got is None:
                self._release_hello(sock)
                _close_socket(sock)
                return
            header, leftover = got
            conn = CrateConnection(
                sock,
                peer=addr,
                recv_prebuffer=leftover,
                primed=(header, None),
            )
            conn.policy_ip = ip
            decision = "drop"
            old_active: CrateConnection | None = None
            old_pending: CrateConnection | None = None
            refusal: tuple[str, str] | None = None
            with self._cv:
                # Drop the hello slot and publish the connection under one lock
                # so close() cannot lose the socket between the two.
                self._hello_ips.pop(sock, None)
                self._vet_socks.discard(sock)
                if not self._stop.is_set():
                    decision, old_active, old_pending, refusal = self._claim_locked(conn, ip)
            if refusal is not None:
                self._log_limited(refusal[0], refusal[1])
            if decision != "serve":
                if decision == "busy":
                    _send_busy(sock)
                conn.close()
                conn = None
                return
            self._finish_replace(conn, old_active, old_pending)
        except Exception:
            if conn is not None:
                conn.close()
            else:
                _close_socket(sock)
        finally:
            with self._cv:
                self._vetters.discard(threading.current_thread())
                self._hello_ips.pop(sock, None)
                self._vet_socks.discard(sock)

    def _claim_locked(
        self, conn: CrateConnection, ip: str
    ) -> tuple[str, CrateConnection | None, CrateConnection | None, tuple[str, str] | None]:
        self._prune_replaced_locked()
        guard: CrateConnection | None = None
        replacing_active = False
        if self._active and self._current is not None:
            guard = self._current
            replacing_active = True
        elif self._pending is not None:
            guard = self._pending
        if guard is not None:
            cur_ip = normalize_peer_ip(str(getattr(guard, "policy_ip", "") or "")) or str(
                getattr(guard, "policy_ip", "") or ""
            )
            if ip != cur_ip and ip not in self._trusted:
                return (
                    "busy",
                    None,
                    None,
                    ("busy", f"crate busy: refused {ip} (active {cur_ip})"),
                )
            if not self._replacement_ok_locked(ip):
                return (
                    "busy",
                    None,
                    None,
                    ("busy", f"crate busy: refused {ip} (replace interval)"),
                )
            old_active = self._current if replacing_active else None
            old_pending = self._pending
            self._pending = conn
            self._replaced_at[ip] = time.monotonic()
            self._cv.notify_all()
            return "serve", old_active, old_pending, None
        self._pending = conn
        self._cv.notify_all()
        return "serve", None, None, None

    def _finish_replace(
        self,
        conn: CrateConnection,
        old_active: CrateConnection | None,
        old_pending: CrateConnection | None,
    ) -> None:
        # One old peer in the log. The live session wins over a not-yet-served socket.
        logged = old_active if old_active is not None else old_pending
        if logged is not None and logged is not conn:
            print(
                f"crate session replaced: closing {_peer_text(logged)} "
                f"for newer client {_peer_text(conn)}",
                flush=True,
            )
        if old_active is not None and old_active is not conn:
            old_active.wake_superseded()
        if (
            old_pending is not None
            and old_pending is not old_active
            and old_pending is not conn
        ):
            # Never handed to session_fn, so no session thread will close it.
            old_pending.close()


def serve_newest(
    listener: CrateListener,
    session_fn,
    *,
    oneshot: bool = False,
    waiting_message: str | None = None,
    peer_ip: Callable[..., str] | None = None,
    trusted_peers: Any = None,
    trust_env: bool = True,
) -> int:
    """Run ``session_fn(conn)`` for each validated crate client.

    A bare connect does not replace anyone. After a valid hello, the client
    is served if no session is active and the pending slot is empty. An
    active or not-yet-served session is replaced only when the newcomer's
    policy IP matches that peer or is trusted, and only once per
    ``REPLACE_MIN_INTERVAL_S`` for that IP. When the trusted list is
    non-empty, other addresses are closed at accept. The hello thread wakes
    the old socket; this thread's ``finally`` close()s it.

    ``peer_ip`` optionally maps the accepted sockaddr to the IP used for
    those checks (loopback tests simulate another host). Conversation history
    belongs in the closure around ``session_fn``; use ``carry_history_for_peer``
    so a different IP starts empty. ``trust_env`` reads ``CRATE_TRUSTED_PEERS``
    once at startup. Pass false when ``trusted_peers`` is already the merge.

    Returns 0, or 1 if ``accept()`` fails with a non-transient OSError.
    ``listener.close()`` or Ctrl+C makes ``take`` return. ``oneshot`` stops
    after a session that was not replaced.
    """
    gate = _NewestAccept(
        listener,
        peer_ip=peer_ip,
        trusted_peers=trusted_peers,
        trust_env=trust_env,
    )
    try:
        while True:
            conn = gate.take(waiting_message)
            if conn is None:
                return gate.exit_code
            try:
                session_fn(conn)
            except (ConnectionError, OSError):
                # Replaced or dropped peer: serve the pending socket next.
                pass
            finally:
                # Drop "active" before close so a fast reconnect is a new
                # session, not a replacement of one that already returned.
                gate.note_session_ended(conn)
                conn.close()
            if oneshot and not gate.was_superseded(conn):
                return gate.exit_code
    finally:
        gate.close()


def serve_echo(
    host: str,
    port: int,
    *,
    oneshot: bool = False,
    peer_ip: Callable[..., str] | None = None,
    trusted_peers: Any = None,
    trust_env: bool = True,
) -> int:
    listener = CrateListener(host, port)
    print(f"  crate echo listening on {listener.host}:{listener.port}")

    def _one(conn: CrateConnection) -> None:
        peer = conn.peer or ("?", 0)
        print(f"  connected {peer[0]}:{peer[1]}  (echo; no models)")
        try:
            run_echo_session(conn)
        except (ConnectionError, OSError) as exc:
            print(f"  session ended: {exc}")
        finally:
            print("  crate disconnected")

    try:
        return serve_newest(
            listener,
            _one,
            oneshot=oneshot,
            waiting_message="  waiting for crate client…",
            peer_ip=peer_ip,
            trusted_peers=trusted_peers,
            trust_env=trust_env,
        )
    finally:
        listener.close()
