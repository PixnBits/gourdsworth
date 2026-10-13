#!/usr/bin/env python3
"""UART face link, Pi → ESP32.

The Pi does not bit-bang WS2812 and does not pulse servos. It writes one
JSON line per command (newline terminated, 115200 8N1) on the UART the
ESP32 already owns:

    {"op":"viseme","id":"rest|aa|ee|oh|mbp","rms":0.0}
    {"op":"gesture","name":"tip|beam|reckon|chuckle|attend|twirl"}
    {"op":"idle"}
    {"op":"ping"}
    {"op":"set_color","r":255,"g":140,"b":20}
    {"op":"set_brightness_cap","cap":102}
    {"op":"test_fill","r":255,"g":120,"b":0}
    {"op":"test_fill","r":255,"g":120,"b":0,"start":0,"count":16}
    {"op":"test_swatches","n":2,"rgb":[255,0,0,0,255,0]}
    {"op":"test_exit"}

``rms`` is 0.0–1.0 from the downlink PCM this process is playing. A gesture
line names a pose. It never carries pixels. Unknown names, including the
retired body set, are dropped so the mouth keeps moving.

Colour commands use the same newline-terminated JSON. There is no checksum.
Test fills write exact RGB on the device (the brightness cap is not applied).
The ESP32 still enforces its 4 A FastLED limit. While test mode is on, the
firmware drops idle, gesture, and viseme lines.
"""

from __future__ import annotations

import json
import math
import numbers
import sys
import threading
import time
from collections.abc import Sequence

import numpy as np

BAUD = 115200
VISEME_HZ = 30

# Default geometry in firmware/esp32/src/main.cpp. A build that changes
# MOUTH_COLS, MOUTH_ROWS, or EYE_LEDS has to change these too.
MOUTH_LEDS = 32 * 8
EYE_LEDS = 16
FACE_LEDS = MOUTH_LEDS + EYE_LEDS * 2
MAX_SWATCHES = 16
# main.cpp lineBuf is 512 bytes including the NUL. The newline is not stored.
LINE_MAX = 511

ALLOWED_GESTURES = ("tip", "beam", "reckon", "chuckle", "attend", "twirl")
RETIRED_GESTURES = ("stamp", "wave", "think", "laugh", "bow", "listen")

# Loud frames of a danny-low voice sit near RMS_REF. The upper edge belongs
# to the next shape, except 1.0 → aa. rest < 0.04 <= mbp < 0.10 <= ee < 0.17
# <= oh < 0.24 <= aa.
_RMS_BINS = (
    (0.04, "rest"),
    (0.10, "mbp"),
    (0.17, "ee"),
    (0.24, "oh"),
)

RMS_REF = 0.30
_PEAK_TAU_S = 3.0


def clamp_rms(value: float) -> float:
    try:
        rms = float(value)
    except (TypeError, ValueError):
        return 0.0
    if rms != rms:  # NaN
        return 0.0
    if rms < 0.0:
        return 0.0
    if rms > 1.0:
        return 1.0
    return rms


class RmsNormalizer:
    """Scale frame RMS onto RMS_REF. The peak rises instantly and decays slowly.

    The peak never falls below RMS_REF, so silence is not amplified into a shape.
    Binned level is ``rms * (RMS_REF / peak)``.
    """

    def __init__(self, ref: float = RMS_REF, tau_s: float = _PEAK_TAU_S) -> None:
        self.ref = ref if ref > 0 else RMS_REF
        self.tau_s = tau_s if tau_s > 0 else _PEAK_TAU_S
        self.peak = self.ref

    def normalize(self, rms: float) -> float:
        level = clamp_rms(rms)
        if level > self.peak:
            self.peak = level
        scaled = level * (self.ref / self.peak)
        decay = math.exp(-(1.0 / VISEME_HZ) / self.tau_s)
        self.peak = self.ref + (self.peak - self.ref) * decay
        if self.peak < self.ref:
            self.peak = self.ref
        return scaled


def viseme_id_for_rms(rms: float) -> str:
    level = clamp_rms(rms)
    for limit, name in _RMS_BINS:
        if level < limit:
            return name
    return "aa"


def rms_of(samples) -> float:
    arr = np.asarray(samples, dtype=np.float64).reshape(-1)
    if arr.size == 0:
        return 0.0
    return clamp_rms(float(np.sqrt(np.mean(arr * arr))))


def encode_line(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def as_u8(value) -> int:
    """Integer in 0..255. Bool is rejected (it is an int subclass)."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"expected integer 0..255, got {value!r}")
    number = int(value)
    if number < 0 or number > 255:
        raise ValueError(f"expected integer 0..255, got {value!r}")
    return number


def _as_count(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"{label} must be an integer")
    return int(value)


def swatch_layout(num_leds: int, n: int) -> list[tuple[int, int]]:
    """Split ``num_leds`` into ``n`` contiguous ``(start, count)`` groups.

    Earlier groups receive one extra LED when the count does not divide
    evenly, so the counts differ by at most one. 10 LEDs into 3 groups
    is ``(0, 4), (4, 3), (7, 3)``. The firmware paints swatches the same way.
    """
    leds = _as_count(num_leds, "num_leds")
    count = _as_count(n, "n")
    if leds < 1 or count < 1 or count > leds:
        raise ValueError(f"cannot split {leds} LEDs into {count} swatches")
    base, extra = divmod(leds, count)
    spans: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        width = base + (1 if index < extra else 0)
        spans.append((start, width))
        start += width
    return spans


def set_color(r: int, g: int, b: int) -> dict:
    """Persisted base face colour. The mouth still scales this by viseme level."""
    return {"op": "set_color", "r": as_u8(r), "g": as_u8(g), "b": as_u8(b)}


def set_brightness_cap(cap: int) -> dict:
    """Persisted normal-mode brightness cap. Test fills do not use it."""
    return {"op": "set_brightness_cap", "cap": as_u8(cap)}


def test_fill(
    r: int,
    g: int,
    b: int,
    start: int | None = None,
    count: int | None = None,
    *,
    num_leds: int = FACE_LEDS,
) -> dict:
    """Exact RGB fill of every LED, or of ``[start, start+count)``.

    The device bypasses the brightness cap for this pixel write.
    """
    if (start is None) != (count is None):
        raise ValueError("start and count are set together")
    obj = {"op": "test_fill", "r": as_u8(r), "g": as_u8(g), "b": as_u8(b)}
    if start is None or count is None:
        return obj
    start_i = _as_count(start, "start")
    count_i = _as_count(count, "count")
    leds = _as_count(num_leds, "num_leds")
    if start_i < 0 or count_i < 1 or start_i > leds or count_i > leds or start_i + count_i > leds:
        raise ValueError(f"fill [{start_i}, {count_i}) is outside 0..{leds}")
    obj["start"] = start_i
    obj["count"] = count_i
    return obj


def test_swatches(colors: Sequence[Sequence[int]], *, num_leds: int = FACE_LEDS) -> dict:
    """N side-by-side swatches. ``colors`` is a sequence of RGB triples.

    At most ``MAX_SWATCHES``, and not more groups than LEDs. ``rgb`` is a
    flat list of channels so the firmware can scan it without a JSON library.
    """
    if not colors:
        raise ValueError("need at least one swatch")
    if len(colors) > MAX_SWATCHES:
        raise ValueError(f"at most {MAX_SWATCHES} swatches")
    leds = _as_count(num_leds, "num_leds")
    if len(colors) > leds:
        raise ValueError(f"more swatches than LEDs ({leds})")
    rgb: list[int] = []
    for colour in colors:
        if len(tuple(colour)) != 3:
            raise ValueError("each swatch needs three channels")
        rgb.extend(as_u8(channel) for channel in colour)
    return {"op": "test_swatches", "n": len(colors), "rgb": rgb}


def test_exit() -> dict:
    """Leave test mode and resume the normal face on the next frame."""
    return {"op": "test_exit"}


def _quoted(line: str, key: str) -> str | None:
    pat = f'"{key}"'
    index = line.find(pat)
    if index < 0:
        return None
    pos = index + len(pat)
    while pos < len(line) and line[pos] in " \t:":
        pos += 1
    if pos >= len(line) or line[pos] != '"':
        return None
    pos += 1
    end = line.find('"', pos)
    if end < 0:
        return None
    value = line[pos:end]
    return value or None


def _long_field(line: str, key: str) -> tuple[bool, int | None]:
    """Return ``(present, value)``. A present but illegal token raises ValueError."""
    pat = f'"{key}"'
    index = line.find(pat)
    if index < 0:
        return False, None
    pos = index + len(pat)
    while pos < len(line) and line[pos] in " \t:":
        pos += 1
    if pos < len(line) and line[pos] == "+":
        pos += 1
    start = pos
    if pos < len(line) and line[pos] == "-":
        pos += 1
    while pos < len(line) and line[pos].isdigit():
        pos += 1
    if pos == start or (line[start] == "-" and pos == start + 1):
        raise ValueError(f"bad integer for {key}")
    if pos < len(line) and line[pos] not in ",}] \t":
        raise ValueError(f"bad integer for {key}")
    return True, int(line[start:pos])


def _u8_field(line: str, key: str) -> int:
    present, value = _long_field(line, key)
    if not present or value is None or value < 0 or value > 255:
        raise ValueError(f"bad u8 for {key}")
    return value


def _rgb_list(line: str) -> list[int]:
    key_at = line.find('"rgb"')
    if key_at < 0:
        raise ValueError("missing rgb")
    bracket = line.find("[", key_at)
    if bracket < 0:
        raise ValueError("missing rgb list")
    pos = bracket + 1
    values: list[int] = []
    while pos < len(line) and line[pos] != "]":
        while pos < len(line) and line[pos] in " \t,":
            pos += 1
        if pos < len(line) and line[pos] == "]":
            break
        if pos < len(line) and line[pos] == "+":
            pos += 1
        start = pos
        if pos < len(line) and line[pos] == "-":
            raise ValueError("rgb channel out of range")
        while pos < len(line) and line[pos].isdigit():
            pos += 1
        if pos == start:
            raise ValueError("bad rgb list")
        if pos < len(line) and line[pos] not in ",] \t":
            raise ValueError("bad rgb list")
        value = int(line[start:pos])
        if value > 255:
            raise ValueError("rgb channel out of range")
        values.append(value)
    if pos >= len(line) or line[pos] != "]" or not values or len(values) % 3 != 0:
        raise ValueError("bad rgb list")
    return values


def decode_line(payload: bytes | str, *, num_leds: int = FACE_LEDS) -> dict | None:
    """Parse one command line the way the firmware scans fields.

    Returns the canonical dict the encoders build, or None when the firmware
    would ignore the line. This is a mirror of the quoted-key scan, not
    ``json.loads``.
    """
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", "replace")
    else:
        text = payload
    line = text.strip("\r\n")
    if not line:
        return None
    try:
        op = _quoted(line, "op")
        if op == "set_color":
            return set_color(_u8_field(line, "r"), _u8_field(line, "g"), _u8_field(line, "b"))
        if op == "set_brightness_cap":
            return set_brightness_cap(_u8_field(line, "cap"))
        if op == "test_fill":
            r = _u8_field(line, "r")
            g = _u8_field(line, "g")
            b = _u8_field(line, "b")
            has_start, start = _long_field(line, "start")
            has_count, count = _long_field(line, "count")
            if has_start != has_count:
                return None
            if not has_start:
                return test_fill(r, g, b)
            return test_fill(r, g, b, start, count, num_leds=num_leds)
        if op == "test_swatches":
            n = _u8_field(line, "n")
            rgb = _rgb_list(line)
            if n < 1 or n > MAX_SWATCHES or n > num_leds or len(rgb) != n * 3:
                return None
            colors = [tuple(rgb[i : i + 3]) for i in range(0, len(rgb), 3)]
            return test_swatches(colors, num_leds=num_leds)
        if op == "test_exit":
            return test_exit()
    except ValueError:
        return None
    return None


def viseme_obj(samples, normalizer: RmsNormalizer | None = None) -> dict:
    raw = rms_of(samples)
    rms = round(raw, 4)
    level = raw if normalizer is None else normalizer.normalize(raw)
    return {"op": "viseme", "id": viseme_id_for_rms(level), "rms": rms}


def gesture_obj(name: str | None) -> dict | None:
    key = (name or "").strip().lower()
    if key not in ALLOWED_GESTURES:
        return None
    return {"op": "gesture", "name": key}


def playback_lines(samples, rate: int) -> list[str]:
    """Viseme lines at about 30 Hz, then idle. No sleeping, no pixels."""
    arr = np.asarray(samples, dtype=np.float64).reshape(-1)
    rate = int(rate) if rate else 0
    if arr.size == 0 or rate <= 0:
        return [encode_line({"op": "idle"}).decode("utf-8")]
    hop = max(1, int(round(rate / VISEME_HZ)))
    normalizer = RmsNormalizer()
    lines = []
    for start in range(0, int(arr.size), hop):
        lines.append(
            encode_line(viseme_obj(arr[start : start + hop], normalizer)).decode("utf-8")
        )
    lines.append(encode_line({"op": "idle"}).decode("utf-8"))
    return lines


def format_dry_run(gesture: str = "tip", *, rate: int = 22050) -> str:
    """UART text for one gesture and a short PCM block. Nothing is opened."""
    chunks = [encode_line({"op": "ping"}).decode("utf-8")]
    posed = gesture_obj(gesture)
    if posed is not None:
        chunks.append(encode_line(posed).decode("utf-8"))
    hop = max(1, int(round(rate / VISEME_HZ)))
    samples = np.full(max(hop, rate // 10), 0.5, dtype=np.float32)
    chunks.extend(playback_lines(samples, rate))
    return "".join(chunks)


# Bound before the class so the methods call these, not themselves.
_CMD_SET_COLOR = set_color
_CMD_SET_BRIGHTNESS_CAP = set_brightness_cap
_CMD_TEST_FILL = test_fill
_CMD_TEST_SWATCHES = test_swatches
_CMD_TEST_EXIT = test_exit


class FaceLink:
    """Writes command lines to a serial port, or to a fake port in tests.

    ``pace`` sleeps between visemes so a live playback tracks the buffer.
    Tests and ``--face-dry-run`` pass ``pace=False`` and call ``drive``.
    """

    def __init__(self, serial=None, *, pace: bool = True) -> None:
        self._serial = serial
        self.pace = pace
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # One peak across buffers so a loud voice stays scaled for the turn.
        self._rms = RmsNormalizer()

    def close(self) -> None:
        self.end_playback()
        ser = self._serial
        self._serial = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def write_obj(self, obj: dict | None) -> bool:
        if not obj:
            return False
        payload = encode_line(obj)
        with self._lock:
            ser = self._serial
            if ser is None:
                return False
            ser.write(payload)
        return True

    def forward_gesture(self, name: str | None) -> bool:
        return self.write_obj(gesture_obj(name))

    def set_color(self, r: int, g: int, b: int) -> bool:
        return self.write_obj(_CMD_SET_COLOR(r, g, b))

    def set_brightness_cap(self, cap: int) -> bool:
        return self.write_obj(_CMD_SET_BRIGHTNESS_CAP(cap))

    def test_fill(
        self,
        r: int,
        g: int,
        b: int,
        start: int | None = None,
        count: int | None = None,
        *,
        num_leds: int = FACE_LEDS,
    ) -> bool:
        return self.write_obj(_CMD_TEST_FILL(r, g, b, start, count, num_leds=num_leds))

    def test_swatches(self, colors: Sequence[Sequence[int]], *, num_leds: int = FACE_LEDS) -> bool:
        return self.write_obj(_CMD_TEST_SWATCHES(colors, num_leds=num_leds))

    def test_exit(self) -> bool:
        return self.write_obj(_CMD_TEST_EXIT())

    def drive(self, samples, rate: int) -> None:
        """Send the viseme lines for this buffer, then idle. Does not sleep."""
        for line in playback_lines(samples, rate):
            self.write_obj(json.loads(line))

    def begin_playback(self, samples, rate: int, *, start_delay_s: float = 0.0) -> None:
        self.end_playback()
        arr = np.array(np.asarray(samples, dtype=np.float32).reshape(-1), copy=True)
        rate = int(rate) if rate else 0
        self._stop = threading.Event()
        stop = self._stop
        self._thread = threading.Thread(
            target=self._paced,
            args=(arr, rate, stop, float(start_delay_s or 0.0)),
            name="face-viseme",
            daemon=True,
        )
        self._thread.start()

    def end_playback(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=2.0)
        self._thread = None
        self.write_obj({"op": "idle"})

    def _paced(
        self,
        samples: np.ndarray,
        rate: int,
        stop: threading.Event,
        start_delay_s: float = 0.0,
    ) -> None:
        if samples.size == 0 or rate <= 0:
            return
        # pace=False stays instant for tests. stop.wait so end_playback cuts in.
        if self.pace and start_delay_s > 0.0 and stop.wait(start_delay_s):
            return
        hop = max(1, int(round(rate / VISEME_HZ)))
        started = time.monotonic()
        sent = 0
        n = int(samples.size)
        for start in range(0, n, hop):
            if stop.is_set():
                return
            self.write_obj(viseme_obj(samples[start : start + hop], self._rms))
            sent += min(hop, n - start)
            if not self.pace:
                continue
            delay = started + (sent / float(rate)) - time.monotonic()
            if delay > 0 and stop.wait(delay):
                return


def open_face(port: str, *, baud: int = BAUD) -> FaceLink | None:
    """Open the Pi UART. Failure leaves the ESP32 on its own idle flicker."""
    port = (port or "").strip()
    if not port:
        return None
    try:
        import serial
    except ImportError:
        print("  face UART: pyserial is not installed; ESP32 stays on idle flicker")
        return None
    try:
        ser = serial.Serial(
            port,
            baudrate=int(baud),
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        )
    except Exception as exc:
        print(f"  face UART unavailable ({exc}); ESP32 stays on idle flicker")
        return None
    print(f"  face UART {port} @ {baud} 8N1")
    return FaceLink(serial=ser, pace=True)


def main(argv: list[str] | None = None) -> int:
    gesture = "tip"
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        gesture = argv[0]
    sys.stdout.write(format_dry_run(gesture))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
