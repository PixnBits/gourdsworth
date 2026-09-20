#!/usr/bin/env python3
"""Gourdsworth crate client — Pi (or laptop loopback). I/O only; no models.

Streams 16 kHz mono s16le PCM to the desktop, plays f32le TTS, sends one
JPEG still on Talk, and prints GESTURE events. Children's audio/stills stay
on the LAN and are never written to disk.

Degraded mode (no button, no camera, no mic) is the default dry path:
keyboard Enter starts Talk; optional silence is sent if sounddevice is missing.

  PYTHONPATH=src python clients/pi/crate_client.py --host 127.0.0.1 --no-camera
"""

from __future__ import annotations

import argparse
import select
import socket
import sys
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from gourdsworth.net import (
        DEFAULT_PORT,
        PROTO,
        UPLINK_CHUNK_BYTES,
        UPLINK_CHUNK_SAMPLES,
        UPLINK_FORMAT,
        UPLINK_RATE,
        CrateConnection,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import gourdsworth.net. Clone the repo and run with "
        "PYTHONPATH=src, or copy src/gourdsworth/net.py next to this tree. "
        f"({exc})"
    ) from exc


def _stdin_ready(timeout: float) -> bool:
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)
    except (OSError, ValueError):
        return False


def _grab_jpeg(index: int, width: int, height: int) -> bytes | None:
    try:
        import cv2
    except ImportError:
        print("  camera skipped: OpenCV not installed")
        return None
    cap = cv2.VideoCapture(int(index))
    frame = None
    try:
        if not cap.isOpened():
            print(f"  camera {index} could not be opened")
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        ok = False
        for _ in range(3):
            ok, frame = cap.read()
        if not ok or frame is None:
            print(f"  camera {index} produced no frame")
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            print("  jpeg encode failed")
            return None
        return bytes(buf)
    finally:
        cap.release()
        del frame


def _play_tts(header: dict, payload: bytes | None) -> None:
    if not payload:
        return
    rate = int(header.get("rate") or 22050)
    fmt = str(header.get("format") or "f32le")
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        print(f"  (no sounddevice; dropped {len(payload)} bytes of TTS)")
        return
    if fmt == "s16le":
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    else:
        samples = np.frombuffer(payload, dtype="<f4")
    duration = float(samples.size) / float(rate) if rate else 0.0
    done = threading.Event()

    def _run() -> None:
        try:
            sd.play(samples, rate)
            sd.wait()
        except Exception as exc:
            print(f"  (playback failed: {exc})")
        finally:
            done.set()

    threading.Thread(target=_run, name="crate-play", daemon=True).start()
    if not done.wait(timeout=max(1.0, duration + 2.0)):
        print("  (playback timed out)")
        try:
            sd.stop()
        except Exception:
            pass
    del samples


def _send_silence(conn: CrateConnection, seconds: float = 0.8) -> None:
    n = max(1, int(seconds * 1000 / 100))
    chunk = b"\x00" * UPLINK_CHUNK_BYTES
    header = {
        "event": "pcm",
        "rate": UPLINK_RATE,
        "channels": 1,
        "format": UPLINK_FORMAT,
    }
    for _ in range(n):
        conn.send(header, chunk)
        time.sleep(0.1)


def _stream_mic(conn: CrateConnection, should_stop, limit_s: float) -> None:
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        print("  (no sounddevice; sending silence)")
        _send_silence(conn, min(0.8, limit_s))
        return
    header = {
        "event": "pcm",
        "rate": UPLINK_RATE,
        "channels": 1,
        "format": UPLINK_FORMAT,
    }
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s and not should_stop():
        frame = sd.rec(
            UPLINK_CHUNK_SAMPLES,
            samplerate=UPLINK_RATE,
            channels=1,
            dtype="int16",
        )
        sd.wait()
        payload = np.asarray(frame, dtype="<i2").reshape(-1).tobytes()
        conn.send(header, payload)
        del payload
        del frame


def _try_gpio(pin: int | None):
    if pin is None:
        return None
    try:
        from gpiozero import Button
    except ImportError:
        print(f"  GPIO pin {pin} requested but gpiozero is missing; using Enter")
        return None
    try:
        btn = Button(int(pin), pull_up=True, bounce_time=0.05)
        print(f"  Talk button on BCM {pin} (hold to talk)")
        return btn
    except Exception as exc:
        print(f"  GPIO button unavailable ({exc}); using Enter")
        return None


def _pump(conn: CrateConnection, ready: threading.Event, end_talk: threading.Event, stop: threading.Event) -> None:
    while not stop.is_set() and not conn.closed:
        item = conn.wait_any(timeout=0.2)
        if item is None:
            continue
        header, payload = item
        ev = header.get("event")
        if ev == "play":
            _play_tts(header, payload)
            try:
                conn.send({"event": "play_done"})
            except (ConnectionError, OSError):
                break
        elif ev == "gesture":
            print(f"GESTURE: {header.get('name') or '?'}")
        elif ev == "ready":
            ready.set()
            print("ready.")
        elif ev == "listen":
            print("LISTENING")
        elif ev in {"thinking", "speaking"}:
            end_talk.set()
            print(str(ev).upper())
        elif ev == "hello":
            pass
        elif ev == "error":
            print(f"  desktop error: {header.get('message')}")
        # payload dropped here — never written


def _wait_trigger(gpio, stop: threading.Event) -> str:
    print("Enter = Talk" + (" (or hold the button)" if gpio is not None else "") + ", q = quit")
    while not stop.is_set():
        if gpio is not None and gpio.is_pressed:
            return "gpio"
        if _stdin_ready(0.1):
            line = sys.stdin.readline()
            if not line or line.strip().lower() in {"q", "quit", "exit"}:
                return "quit"
            return "enter"
    return "quit"


def _talk(
    conn: CrateConnection,
    *,
    trigger: str,
    gpio,
    camera: bool,
    camera_index: int,
    no_mic: bool,
    limit_s: float,
    end_talk: threading.Event,
) -> None:
    end_talk.clear()
    if camera:
        jpeg = _grab_jpeg(camera_index, 640, 480)
        if jpeg:
            conn.send({"event": "jpeg"}, jpeg)
            del jpeg
        else:
            conn.send({"event": "still"})
    conn.send({"event": "button", "state": "down"})
    print("TALK — Enter again to stop (or release the button / wait)")

    def should_stop() -> bool:
        if end_talk.is_set():
            return True
        if trigger == "gpio" and gpio is not None and not gpio.is_pressed:
            return True
        if trigger == "enter" and _stdin_ready(0):
            sys.stdin.readline()
            return True
        return False

    if no_mic:
        _send_silence(conn, min(0.8, limit_s))
        # still honor a held GPIO / second Enter if the user is using a mic-less dry run
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.2 and not should_stop():
            time.sleep(0.05)
    else:
        _stream_mic(conn, should_stop, limit_s)
    try:
        conn.send({"event": "button", "state": "up"})
    except (ConnectionError, OSError):
        return



def _load_local_env() -> dict[str, str]:
    """Optional clients/pi/local.env — never commit; keeps LAN hosts out of git."""
    path = Path(__file__).resolve().parent / "local.env"
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _env_host() -> str:
    import os

    local = _load_local_env()
    return os.environ.get("CRATE_HOST") or local.get("CRATE_HOST") or "127.0.0.1"


def _env_port() -> int:
    import os

    local = _load_local_env()
    raw = os.environ.get("CRATE_PORT") or local.get("CRATE_PORT")
    if raw:
        return int(raw)
    return int(DEFAULT_PORT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gourdsworth Pi crate client — I/O only (no Whisper/Ollama/CLIP)"
    )
    parser.add_argument(
        "--host",
        default=_env_host(),
        help="Desktop crate host (env CRATE_HOST, or clients/pi/local.env; default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_port(),
        help="Desktop crate port (env CRATE_PORT / local.env; default from protocol)",
    )
    parser.add_argument("--no-camera", action="store_true", help="Voice-only; do not grab a JPEG")
    parser.add_argument("--no-mic", action="store_true", help="Send silence instead of capturing a mic")
    parser.add_argument("--camera", type=int, default=0, metavar="N", help="OpenCV camera index")
    parser.add_argument(
        "--button-pin",
        type=int,
        default=None,
        metavar="BCM",
        help="gpiozero Talk button BCM pin (omit for Enter)",
    )
    parser.add_argument("--listen-s", type=float, default=8.0, help="Talk cap in seconds")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("Gourdsworth crate client — I/O only. No models on this machine.")
    print(f"  desktop {args.host}:{args.port}")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("  LAN mode: PCM/JPEG cross the house network, not the internet. No TLS.")
    gpio = _try_gpio(args.button_pin)
    try:
        sock = socket.create_connection((args.host, args.port), timeout=8)
    except OSError as exc:
        print(f"Could not connect to {args.host}:{args.port}: {exc}")
        print("On the desktop: python -m gourdsworth --serve-crate   (or --crate-echo)")
        return 1
    conn = CrateConnection(sock)
    stop = threading.Event()
    ready = threading.Event()
    end_talk = threading.Event()
    pump = threading.Thread(
        target=_pump, args=(conn, ready, end_talk, stop), name="crate-pump", daemon=True
    )
    pump.start()
    try:
        conn.send({"event": "hello", "role": "crate", "proto": PROTO})
        if not ready.wait(timeout=20):
            print("No ready from desktop (is --serve-crate / --crate-echo running?)")
            return 1
        while True:
            ready.clear()
            trig = _wait_trigger(gpio, stop)
            if trig == "quit":
                try:
                    conn.send({"event": "bye"})
                except (ConnectionError, OSError):
                    pass
                break
            _talk(
                conn,
                trigger=trig,
                gpio=gpio,
                camera=not args.no_camera,
                camera_index=args.camera,
                no_mic=args.no_mic,
                limit_s=float(args.listen_s),
                end_talk=end_talk,
            )
            if not ready.wait(timeout=float(args.listen_s) + 15):
                print("  (timed out waiting for desktop ready)")
                if conn.closed:
                    break
    except (KeyboardInterrupt, ConnectionError, OSError) as exc:
        if not isinstance(exc, KeyboardInterrupt):
            print(f"disconnected: {exc}")
        print()
    finally:
        stop.set()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
