#!/usr/bin/env python3
"""UART face link, Pi → ESP32.

The Pi does not bit-bang WS2812 and does not pulse servos. It writes one
JSON line per command (newline terminated, 115200 8N1) on the UART the
ESP32 already owns:

    {"op":"viseme","id":"rest|aa|ee|oh|mbp","rms":0.0}
    {"op":"gesture","name":"tip|beam|reckon|chuckle|attend|twirl"}
    {"op":"idle"}
    {"op":"ping"}

``rms`` is 0.0–1.0 from the downlink PCM this process is playing. A gesture
line names a pose. It never carries pixels. Unknown names, including the
retired body set, are dropped so the mouth keeps moving.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import numpy as np

BAUD = 115200
VISEME_HZ = 30

ALLOWED_GESTURES = ("tip", "beam", "reckon", "chuckle", "attend", "twirl")
RETIRED_GESTURES = ("stamp", "wave", "think", "laugh", "bow", "listen")

# RMS bins. The upper edge belongs to the next shape, except 1.0 → aa.
_RMS_BINS = (
    (0.08, "rest"),
    (0.22, "mbp"),
    (0.45, "ee"),
    (0.70, "oh"),
)


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


def viseme_obj(samples) -> dict:
    rms = round(rms_of(samples), 4)
    return {"op": "viseme", "id": viseme_id_for_rms(rms), "rms": rms}


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
    lines = []
    for start in range(0, int(arr.size), hop):
        lines.append(encode_line(viseme_obj(arr[start : start + hop])).decode("utf-8"))
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

    def drive(self, samples, rate: int) -> None:
        """Send the viseme lines for this buffer, then idle. Does not sleep."""
        for line in playback_lines(samples, rate):
            self.write_obj(json.loads(line))

    def begin_playback(self, samples, rate: int) -> None:
        self.end_playback()
        arr = np.array(np.asarray(samples, dtype=np.float32).reshape(-1), copy=True)
        rate = int(rate) if rate else 0
        self._stop = threading.Event()
        stop = self._stop
        self._thread = threading.Thread(
            target=self._paced,
            args=(arr, rate, stop),
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

    def _paced(self, samples: np.ndarray, rate: int, stop: threading.Event) -> None:
        if samples.size == 0 or rate <= 0:
            return
        hop = max(1, int(round(rate / VISEME_HZ)))
        started = time.monotonic()
        sent = 0
        n = int(samples.size)
        for start in range(0, n, hop):
            if stop.is_set():
                return
            self.write_obj(viseme_obj(samples[start : start + hop]))
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
