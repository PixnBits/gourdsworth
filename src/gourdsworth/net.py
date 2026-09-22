"""Crate TCP protocol: Pi I/O client ↔ desktop model runner.

Inference (STT / LLM / TTS / CLIP) stays on the desktop. The Pi sends
microphone PCM, optional JPEG stills, and button events; it receives TTS
PCM and gesture names. Children's PCM, JPEG, and transcripts stay in RAM
and are dropped after the turn. Nothing is written to disk.

Wire format (one TCP connection, one client at a time)
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
    {"event":"gesture","name":"stamp"|"wave"|"think"|"laugh"|"bow"|"listen"}
    {"event":"play_done"}
    {"event":"bye"}
    {"event":"error","message":"..."}
    {"event":"telemetry","cpu_temp_c":47.1}
        # Pi → desktop, lightweight porch health. Optional; omitted fields mean
        # unknown. Cached on CrateConnection.latest_telemetry; does not enter
        # the PCM / JPEG / wait_event queues. cpu_temp_c is Celsius float.


Default bind is 127.0.0.1. Binding a LAN address or 0.0.0.0 requires
``crate.allow_lan: true`` — anyone on that network could then read kids'
PCM/JPEG. There is no TLS and no auth.
"""

from __future__ import annotations

from dataclasses import dataclass

import json
import queue
import socket
import threading
import time
from collections import deque
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
ALLOWED_GESTURES = frozenset({"stamp", "wave", "think", "laugh", "bow", "listen"})

MAX_LINE = 16 * 1024
MAX_PAYLOAD = 2 * 1024 * 1024
MAX_EVENTS = 64

_RECV_TIMEOUT_S = 0.4


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

    def __init__(self, sock: socket.socket, peer: tuple[str, int] | None = None):
        self.sock = sock
        self.peer = peer
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(_RECV_TIMEOUT_S)
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
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
        buf = bytearray()
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


class CrateListener:
    """TCP listener. Default bind is loopback. Accepts one client at a time."""

    def __init__(self, host: str, port: int):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, int(port)))
        self._sock.listen(1)
        self._sock.settimeout(0.5)
        self.host, self.port = self._sock.getsockname()[:2]

    def accept(self) -> CrateConnection:
        while True:
            try:
                sock, addr = self._sock.accept()
            except socket.timeout:
                continue
            return CrateConnection(sock, peer=addr)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def run_echo_session(conn: CrateConnection, *, gesture: str = "stamp") -> None:
    """Protocol smoke: hello, then Talk → echo PCM as play + a gesture.

    Stays on the connection until the client drops. Does not run STT/LLM/TTS.
    Used by ``--crate-echo`` and unit tests.
    """
    conn.send({"event": "hello", "role": "desktop", "proto": PROTO})
    conn.send({"event": "ready"})
    name = gesture if gesture in ALLOWED_GESTURES else "stamp"
    while not conn.closed:
        if not conn.wait_button("down", timeout=0.5):
            continue
        conn.send({"event": "listen"})
        audio, _ms, _uf, _uj, _voiced = conn.listen_pcm(limit_s=4.0, mode="ptt")
        jpeg = conn.take_jpeg()
        if jpeg:
            del jpeg
        conn.send({"event": "speaking"})
        conn.play_float(audio, UPLINK_RATE)
        conn.send({"event": "gesture", "name": name})
        conn.reset_talk_latch()
        conn.send({"event": "ready"})


def serve_echo(host: str, port: int, *, oneshot: bool = False) -> None:
    listener = CrateListener(host, port)
    print(f"  crate echo listening on {listener.host}:{listener.port}")
    try:
        while True:
            print("  waiting for crate client…")
            conn = listener.accept()
            peer = conn.peer or ("?", 0)
            print(f"  connected {peer[0]}:{peer[1]}  (echo; no models)")
            try:
                run_echo_session(conn)
            except (ConnectionError, OSError) as exc:
                print(f"  session ended: {exc}")
            finally:
                conn.close()
                print("  crate disconnected")
            if oneshot:
                break
    finally:
        listener.close()
