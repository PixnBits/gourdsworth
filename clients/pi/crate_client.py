#!/usr/bin/env python3
"""Gourdsworth crate client — Pi (or laptop loopback). I/O only; no models.

Streams 16 kHz mono s16le PCM to the desktop, plays f32le TTS, sends one
JPEG still on Talk, and prints GESTURE events. Children's audio/stills stay
on the LAN and are never written to disk.

Degraded mode (no button, no camera, no mic) is the default dry path:
keyboard Enter starts Talk; optional silence is sent if sounddevice is missing.
SIGTERM and SIGHUP stop cleanly (audio streams and the camera are closed
before exit). A nohup launch keeps SIGHUP ignored.

  PYTHONPATH=src python clients/pi/crate_client.py --host 127.0.0.1 --no-camera
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import sys
import random
import threading
import wave
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
        parse_thermal_sysfs_temp,
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


# Adaptive still ladder (long edge). 0 = native camera frame, no downscale.
_STILL_LADDER = (0, 1920, 1280, 960, 640)


def _grab_jpeg(
    index: int,
    *,
    max_edge: int = 0,
    quality: int = 90,
) -> tuple[bytes | None, tuple[int, int] | None]:
    """Grab one still. max_edge 0 = full native resolution; else fit inside max_edge.

    Returns (jpeg_bytes, (width, height)) or (None, None).
    """
    if _SHUTDOWN.is_set():
        return None, None
    try:
        import cv2
    except ImportError:
        print("  camera skipped: OpenCV not installed")
        return None, None
    with _CAMERA_LOCK:
        return _grab_jpeg_unlocked(cv2, index, max_edge=max_edge, quality=quality)


def _grab_jpeg_unlocked(
    cv2,
    index: int,
    *,
    max_edge: int = 0,
    quality: int = 90,
) -> tuple[bytes | None, tuple[int, int] | None]:
    cap = cv2.VideoCapture(int(index))
    _ACTIVE_CAPS.add(cap)
    frame = None
    try:
        if not cap.isOpened():
            print(f"  camera {index} could not be opened")
            return None, None
        # Ask for a high mode; driver may still deliver native/sensor size.
        if max_edge <= 0:
            # 1080p is enough for costume CLIP; 4K open/read heated the Pi for no gain.
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        else:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(max_edge))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(max_edge))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        ok = False
        # Two reads is enough with BUFFERSIZE=1; four was extra heat/time per open.
        for _ in range(2):
            ok, frame = cap.read()
        if not ok or frame is None:
            print(f"  camera {index} produced no frame")
            return None, None
        h, w = frame.shape[:2]
        if max_edge > 0 and max(h, w) > max_edge:
            scale = float(max_edge) / float(max(h, w))
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
            h, w = frame.shape[:2]
        ok, buf = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)],
        )
        if not ok:
            print("  jpeg encode failed")
            return None, None
        return bytes(buf), (int(w), int(h))
    finally:
        _ACTIVE_CAPS.discard(cap)
        cap.release()
        del frame


class _StillAdaptive:
    """Start at full res; step down the long-edge ladder if a send is slow."""

    def __init__(self, *, start_edge: int = 0, slow_ms: float = 800.0):
        self.edge = int(start_edge)
        self.slow_ms = float(slow_ms)
        self.quality = 90
        try:
            self._idx = _STILL_LADDER.index(self.edge)
        except ValueError:
            # Custom edge — treat as current rung; next step goes to nearest lower ladder
            self._idx = 0
            for i, e in enumerate(_STILL_LADDER):
                if e == 0:
                    continue
                if self.edge == 0 or self.edge >= e:
                    self._idx = i
                    break

    def note_send(self, nbytes: int, elapsed_ms: float) -> None:
        if nbytes <= 0:
            return
        # Slow absolute send, or sluggish effective rate on larger frames
        mbps = (nbytes * 8.0 / 1_000_000.0) / max(elapsed_ms / 1000.0, 0.001)
        if elapsed_ms < self.slow_ms and mbps >= 8.0:
            return
        if self._idx >= len(_STILL_LADDER) - 1 and self.quality <= 70:
            return
        if self._idx < len(_STILL_LADDER) - 1:
            self._idx += 1
            self.edge = int(_STILL_LADDER[self._idx])
            label = "native" if self.edge <= 0 else f"max-edge {self.edge}"
            print(
                f"  still adaptive: send {elapsed_ms:.0f}ms / {nbytes // 1024}KiB "
                f"→ next still {label}"
            )
        elif self.quality > 70:
            self.quality = max(70, self.quality - 10)
            print(
                f"  still adaptive: send {elapsed_ms:.0f}ms → JPEG quality {self.quality}"
            )


# Cached after a silent probe — USB DACs often lie about default_samplerate
# (e.g. claim 44100 but only accept 48000) and PortAudio spam stderr on each fail.
_PLAY_RATE: int | None = None
_CAPTURE_RATE: int | None = None
_AUDIO_LOCK = threading.Lock()  # PortAudio: never rec+play concurrently
_CAMERA_LOCK = threading.Lock()  # one OpenCV open at a time
_SNAP_BUSY = threading.Event()
_SHUTDOWN = threading.Event()
_SHUTDOWN_SIGNAL: int | None = None
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_DONE = False
_ACTIVE_CAPS: set = set()  # live cv2.VideoCapture objects
_PW_PLAY_PROC = None  # live pw-play Popen

_LOG_FH = None


class _ShutdownSignal(KeyboardInterrupt):
    """SIGTERM/SIGHUP delivered as KeyboardInterrupt so existing handlers exit cleanly."""

    def __init__(self, signum: int) -> None:
        self.signum = int(signum)
        super().__init__(self.signum)


def _on_shutdown_signal(signum, frame) -> None:
    """First signal raises; a second one force-closes devices and re-kills."""
    global _SHUTDOWN_SIGNAL
    if _SHUTDOWN.is_set():
        try:
            _cleanup_devices(force=True)
        except Exception:
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        return
    _SHUTDOWN.set()
    _SHUTDOWN_SIGNAL = int(signum)
    try:
        name = signal.Signals(int(signum)).name
    except Exception:
        name = str(signum)
    try:
        print(f"  caught {name} — shutting down (closing audio/camera)", flush=True)
    except Exception:
        pass
    raise _ShutdownSignal(int(signum))


def _install_signal_handlers() -> list[int]:
    """Install SIGTERM, and SIGHUP unless nohup already set SIG_IGN. Main thread only."""
    if threading.current_thread() is not threading.main_thread():
        return []
    installed: list[int] = []
    signal.signal(signal.SIGTERM, _on_shutdown_signal)
    installed.append(int(signal.SIGTERM))
    # usb_audio_watch.sh starts the client under nohup, which ignores SIGHUP on purpose.
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:
        signal.signal(signal.SIGHUP, _on_shutdown_signal)
        installed.append(int(signal.SIGHUP))
    return installed


def _cleanup_devices(*, force: bool = False, camera_wait_s: float = 2.0) -> bool:
    """Stop PortAudio, pw-play, and the camera. Idempotent; never raises.

    Does not take ``_AUDIO_LOCK`` — the uplink thread may hold it inside ``sd.wait()``.
    The camera lock, once acquired, is held so a snap cannot open a new device.
    """
    global _CLEANUP_DONE
    if not _CLEANUP_LOCK.acquire(blocking=False):
        return False
    try:
        if _CLEANUP_DONE:
            return True
        try:
            import sounddevice as sd

            sd.stop(ignore_errors=True)
        except ImportError:
            pass
        except Exception:
            pass
        try:
            proc = _PW_PLAY_PROC
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.kill()
        except Exception:
            pass
        try:
            got_lock = False
            if not force:
                got_lock = _CAMERA_LOCK.acquire(timeout=float(camera_wait_s))
            if not got_lock:
                for cap in list(_ACTIVE_CAPS):
                    try:
                        cap.release()
                    except Exception:
                        pass
                _ACTIVE_CAPS.clear()
            # got_lock: keep it. Opens happen under this lock, so nothing is live.
        except Exception:
            pass
        _CLEANUP_DONE = True
        try:
            print("  devices released (audio streams stopped, camera closed)")
        except Exception:
            pass
        return True
    finally:
        _CLEANUP_LOCK.release()


def _reset_shutdown_state_for_tests() -> None:
    """Reset signal-shutdown flags so unit tests do not leak into each other."""
    global _SHUTDOWN_SIGNAL, _CLEANUP_DONE, _PW_PLAY_PROC
    _SHUTDOWN.clear()
    _SHUTDOWN_SIGNAL = None
    _CLEANUP_DONE = False
    _ACTIVE_CAPS.clear()
    _PW_PLAY_PROC = None
    if _CAMERA_LOCK.locked():
        try:
            _CAMERA_LOCK.release()
        except RuntimeError:
            pass
    if _CLEANUP_LOCK.locked():
        try:
            _CLEANUP_LOCK.release()
        except RuntimeError:
            pass

# Porch CPU temp telemetry (Pi → desktop). Prefer sysfs; vcgencmd is optional.
_TELEMETRY_INTERVAL_S = 15.0
_THERMAL_SYSFS = Path("/sys/class/thermal/thermal_zone0/temp")


def read_cpu_temp_c() -> float | None:
    """Return SoC temperature in Celsius, or None if unavailable."""
    try:
        raw = _THERMAL_SYSFS.read_text(encoding="ascii")
    except OSError:
        raw = None
    temp = parse_thermal_sysfs_temp(raw)
    if temp is not None:
        return temp
    try:
        import subprocess

        out = subprocess.check_output(
            ["vcgencmd", "measure_temp"],
            text=True,
            timeout=1.0,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    # Typical: temp=47.8'C
    try:
        part = out.strip().split("=", 1)[1]
        part = part.replace("'C", "").replace("C", "").strip()
        val = float(part)
    except (IndexError, ValueError):
        return None
    if val <= 0 or val > 200:
        return None
    return val


def _send_telemetry(conn: CrateConnection) -> None:
    temp = read_cpu_temp_c()
    header: dict = {"event": "telemetry"}
    if temp is not None:
        header["cpu_temp_c"] = round(float(temp), 1)
    try:
        conn.send(header)
    except (ConnectionError, OSError):
        raise


def _telemetry_loop(conn: CrateConnection, stop: threading.Event, interval_s: float = _TELEMETRY_INTERVAL_S) -> None:
    """Send lightweight CPU temp after connect and every interval_s thereafter."""
    while not stop.is_set() and not conn.closed:
        try:
            _send_telemetry(conn)
        except (ConnectionError, OSError):
            return
        except Exception as exc:
            print(f"  (telemetry skipped: {exc})")
        if stop.wait(timeout=max(1.0, float(interval_s))):
            return


def _setup_log(path: str | None) -> None:
    """Tee stdout/stderr to a file so the coordinator can pull porch logs."""
    global _LOG_FH
    if not path:
        return
    log_path = Path(path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _LOG_FH = open(log_path, "a", buffering=1, encoding="utf-8")
    _LOG_FH.write(f"\n==== crate-pi start {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    _LOG_FH.flush()

    class _Tee:
        def __init__(self, stream, fh):
            self._stream = stream
            self._fh = fh

        def write(self, data):
            self._stream.write(data)
            self._fh.write(data)
            self._fh.flush()
            return len(data)

        def flush(self):
            self._stream.flush()
            self._fh.flush()

        def fileno(self):
            return self._stream.fileno()

        def isatty(self):
            return False

    sys.stdout = _Tee(sys.stdout, _LOG_FH)  # type: ignore[assignment]
    sys.stderr = _Tee(sys.stderr, _LOG_FH)  # type: ignore[assignment]
    print(f"  logging to {log_path}")


def _log(msg: str) -> None:
    print(msg)



def _resample_f32(samples, src_rate: int, dst_rate: int):
    """Cheap linear resample — USB DACs often reject Piper's 22050 Hz."""
    import numpy as np

    if src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate or samples.size == 0:
        return samples, src_rate
    n_dst = max(1, int(round(samples.size * float(dst_rate) / float(src_rate))))
    x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_dst, endpoint=False)
    out = np.interp(x_new, x_old, samples.astype(np.float64)).astype(np.float32)
    return out, dst_rate


def _probe_play_rate() -> int | None:
    """Find one sample rate the current output device accepts. Mute PortAudio noise."""
    global _PLAY_RATE
    if _PLAY_RATE is not None:
        return _PLAY_RATE
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        return None

    candidates: list[int] = []
    try:
        out_id = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        native = int(float(sd.query_devices(out_id).get("default_samplerate") or 0))
    except Exception:
        native = 0
    # Prefer 48k first — many USB PnP DACs reject 44100 even when advertised.
    for r in (48000, 44100, native, 32000, 22050, 16000):
        if r and r not in candidates:
            candidates.append(int(r))

    # PortAudio prints paInvalidSampleRate to stderr even when we catch it.
    devnull = open(os.devnull, "w")
    old_err = os.dup(2)
    try:
        os.dup2(devnull.fileno(), 2)
        for rate in candidates:
            try:
                tone = np.zeros(int(rate * 0.02), dtype=np.float32)
                with _AUDIO_LOCK:
                    sd.play(tone, rate)
                    sd.wait()
                _PLAY_RATE = rate
                break
            except Exception:
                continue
    finally:
        os.dup2(old_err, 2)
        os.close(old_err)
        devnull.close()

    if _PLAY_RATE is not None:
        print(f"  playback sample rate: {_PLAY_RATE} Hz (probed)")
    else:
        print("  (warning: could not probe a working playback sample rate)")
    return _PLAY_RATE


def _capture_rate_candidates(native: int, override: int | None = None) -> list[int]:
    """Devices that are natively 16 kHz open at 16 kHz with no resample; everything else
    (BRIO webcam mic = 48 kHz, CM108 = 48 kHz) opens at a native hardware rate and
    ``_i16_to_uplink`` resamples to 16 kHz.
    """
    raw: list[int] = []
    if override:
        raw.append(int(override))
    if int(native) == int(UPLINK_RATE):
        raw.append(int(UPLINK_RATE))
    raw.extend((48000, 44100, int(native) if native else 0, int(UPLINK_RATE)))
    out: list[int] = []
    for item in raw:
        try:
            rate = int(item)
        except (TypeError, ValueError):
            continue
        if rate <= 0 or rate in out:
            continue
        out.append(rate)
    return out


def _probe_capture_rate() -> int | None:
    """Find one sample rate the current input device accepts. Mute PortAudio noise."""
    global _CAPTURE_RATE
    if _CAPTURE_RATE is not None:
        return _CAPTURE_RATE
    try:
        import sounddevice as sd
    except ImportError:
        return None

    in_id = None
    native = 0
    dev_name = ""
    try:
        dev = sd.default.device
        in_id = dev[0] if isinstance(dev, (list, tuple)) else dev
        info = sd.query_devices(in_id)
        native = int(float(info.get("default_samplerate") or 0))
        dev_name = str(info.get("name") or "")
    except Exception:
        in_id = None
        native = 0
        dev_name = ""
    try:
        override = _env_int("CRATE_INPUT_RATE")
    except (TypeError, ValueError):
        print("  (warning: CRATE_INPUT_RATE is not an integer; ignoring)")
        override = None
    candidates = _capture_rate_candidates(native, override)

    devnull = open(os.devnull, "w")
    old_err = os.dup(2)
    try:
        os.dup2(devnull.fileno(), 2)
        for rate in candidates:
            try:
                kwargs = {"samplerate": rate, "channels": 1, "dtype": "int16"}
                if in_id is not None:
                    kwargs["device"] = in_id
                sd.check_input_settings(**kwargs)
                _CAPTURE_RATE = rate
                break
            except Exception:
                if override is not None and int(rate) == int(override):
                    print(
                        f"  (warning: CRATE_INPUT_RATE={int(override)} rejected "
                        "by input device; trying other capture rates)"
                    )
                continue
    finally:
        os.dup2(old_err, 2)
        os.close(old_err)
        devnull.close()

    if _CAPTURE_RATE is not None:
        if int(_CAPTURE_RATE) == int(UPLINK_RATE):
            how = "no resample"
        else:
            how = f"resample → {UPLINK_RATE} uplink"
        named = f"{dev_name}; " if dev_name else ""
        print(
            f"  capture sample rate: {_CAPTURE_RATE} Hz "
            f"({named}device native {native}; {how})"
        )
    else:
        print("  (warning: could not probe a working capture sample rate)")
    return _CAPTURE_RATE


def _capture_chunk_samples(capture_rate: int) -> int:
    if capture_rate <= 0:
        return int(UPLINK_CHUNK_SAMPLES)
    return max(1, int(round(float(UPLINK_CHUNK_SAMPLES) * float(capture_rate) / float(UPLINK_RATE))))


def _i16_to_uplink(arr, capture_rate: int):
    import numpy as np

    if capture_rate == UPLINK_RATE:
        return np.asarray(arr, dtype="<i2").reshape(-1)
    f32 = np.asarray(arr, dtype=np.float32).reshape(-1) / 32768.0
    f32, _ = _resample_f32(f32, capture_rate, UPLINK_RATE)
    return np.clip(np.rint(f32 * 32768.0), -32768, 32767).astype("<i2")




def _shutdown_wav_dir() -> Path:
    return Path(__file__).resolve().parent / "assets" / "shutdown"


def _load_wav_f32(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if sw == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        raise RuntimeError(f"unsupported wav width {sw} in {path}")
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return arr, int(rate)



def _playback_backend() -> str:
    """pw = PipeWire pw-play (stable on Bluetooth); sounddevice = PortAudio."""
    raw = (os.environ.get("CRATE_PLAYBACK") or _load_local_env().get("CRATE_PLAYBACK") or "auto").strip().lower()
    if raw in {"pw", "pipewire", "pw-play"}:
        return "pw"
    if raw in {"sd", "sounddevice", "portaudio"}:
        return "sounddevice"
    try:
        import sounddevice as sd
        out_id = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        name = str(sd.query_devices(out_id).get("name") or "").lower()
        if "pipewire" in name or name.strip() == "default":
            return "pw"
    except Exception:
        pass
    return "sounddevice"


def _play_pcm_f32(samples, rate: int) -> None:
    """Play mono float32 PCM. Prefer pw-play for PipeWire/Bluetooth sinks."""
    global _PW_PLAY_PROC
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size == 0 or rate <= 0:
        return
    # Bluetooth A2DP often clips the first ~100ms — prime with short silence.
    prime_ms = int(os.environ.get("CRATE_BT_PRIME_MS") or _load_local_env().get("CRATE_BT_PRIME_MS") or "180")
    backend = _playback_backend()
    if backend == "pw" and prime_ms > 0:
        n_prime = max(1, int(rate * (prime_ms / 1000.0)))
        samples = np.concatenate([np.zeros(n_prime, dtype=np.float32), samples])
    if backend == "pw":
        import subprocess
        import tempfile
        import wave

        clipped = np.clip(samples, -1.0, 1.0)
        pcm = np.clip(np.rint(clipped * 32767.0), -32768, 32767).astype("<i2")
        fd, wav_path = tempfile.mkstemp(prefix="crate-play-", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(rate))
                wf.writeframes(pcm.tobytes())
            env = os.environ.copy()
            env.setdefault("PIPEWIRE_LATENCY", "1024/48000")
            with _AUDIO_LOCK:
                # Popen (not run) so signal cleanup can terminate an in-flight pw-play.
                proc = subprocess.Popen(
                    ["pw-play", wav_path],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _PW_PLAY_PROC = proc
                try:
                    proc.wait()
                finally:
                    if proc.poll() is None:  # interrupted (signal) — don't orphan it
                        try:
                            proc.terminate()
                            proc.wait(timeout=1)
                        except Exception:
                            proc.kill()
                    if _PW_PLAY_PROC is proc:
                        _PW_PLAY_PROC = None
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
        return
    import sounddevice as sd

    with _AUDIO_LOCK:
        try:
            sd.play(samples, int(rate), blocking=True)
        except TypeError:
            sd.play(samples, int(rate))
            sd.wait()


def _play_local_shutdown(*, kind: str = "disconnect") -> None:
    """Play a canned Mayor line from disk — no desktop, no TTS on the Pi."""
    if _SHUTDOWN.is_set():
        print("  (signal shutdown: skipping canned goodbye audio)")
        return
    try:
        import numpy as np  # noqa: F401 — used via _load_wav_f32
        import sounddevice as sd
    except ImportError:
        print("  (no sounddevice; skip local shutdown line)")
        return
    root = _shutdown_wav_dir()
    if kind == "goodbye":
        paths = sorted(root.glob("goodbye_*.wav"))
        if not paths:
            paths = sorted(root.glob("disconnect_*.wav"))
    else:
        paths = sorted(root.glob("disconnect_*.wav"))
        if not paths:
            paths = sorted(root.glob("goodbye_*.wav"))
    if not paths:
        print("  (no local shutdown wavs)")
        return
    path = random.choice(paths)
    try:
        samples, rate = _load_wav_f32(path)
        play_rate = _probe_play_rate() or rate
        if play_rate != rate:
            samples, play_rate = _resample_f32(samples, rate, play_rate)
        _play_pcm_f32(samples, play_rate)
        print(f"  shutdown line: {path.name}  playback={_playback_backend()}")
    except Exception as exc:
        print(f"  (shutdown wav failed: {exc})")



def _wait_for_desktop(
    host: str,
    port: int,
    quit_ev: threading.Event,
    *,
    retry_s: float = 5.0,
    announce_every_s: float = 45.0,
    connect_timeout_s: float = 8.0,
    announce_immediately: bool = True,
    play=None,
) -> socket.socket | None:
    """Block until the desktop accepts TCP. Play generic disconnect lines while waiting.

    Returns a connected socket, or None if quit_ev is set. Announces on first failure
    when announce_immediately is True; later announcements are spaced by announce_every_s.
    """
    play_fn = play or _play_local_shutdown
    next_announce = 0.0 if announce_immediately else (time.monotonic() + float(announce_every_s))
    printed_hint = False
    while not quit_ev.is_set() and not _SHUTDOWN.is_set():
        try:
            sock = socket.create_connection((host, port), timeout=connect_timeout_s)
            print(f"  connected to {host}:{port}")
            return sock
        except OSError as exc:
            now = time.monotonic()
            print(f"  waiting for desktop {host}:{port}: {exc}")
            if not printed_hint:
                print("  On the desktop: python -m gourdsworth --serve-crate   (or --crate-echo)")
                printed_hint = True
            if now >= next_announce:
                play_fn(kind="disconnect")
                next_announce = now + float(announce_every_s)
            if quit_ev.wait(timeout=max(0.5, float(retry_s))):
                return None
    return None


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
        samples = np.frombuffer(payload, dtype="<f4").copy()

    play_rate = _probe_play_rate() or 48000
    if play_rate != rate:
        samples, play_rate = _resample_f32(samples, rate, play_rate)

    duration = float(samples.size) / float(play_rate) if play_rate else 0.0
    # Play on this thread under the audio lock — never overlap sd.rec (double-free).
    try:
        _play_pcm_f32(samples, play_rate)
    except Exception as exc:
        print(f"  (playback failed: {exc})")
        return
    del samples
    _log(f"play done ({duration:.1f}s @ {play_rate} Hz) backend={_playback_backend()}")


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


def _stream_mic(
    conn: CrateConnection,
    should_stop,
    limit_s: float,
    *,
    vad: bool = False,
    energy: float = 0.012,
    silence_s: float = 0.55,
) -> None:
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
    capture_rate = _probe_capture_rate() or UPLINK_RATE
    capture_n = _capture_chunk_samples(capture_rate)
    chunk_s = float(UPLINK_CHUNK_SAMPLES) / float(UPLINK_RATE)
    need_silent = max(1, int(silence_s / chunk_s))
    silent = 0
    heard = False
    t0 = time.monotonic()
    # Continuous VAD: wait for speech forever (until should_stop); only then
    # apply silence-end. One-shot Talk still caps at limit_s.
    while not should_stop() and not _SHUTDOWN.is_set():
        if (not vad) and (time.monotonic() - t0 >= limit_s):
            break
        if vad and heard and (time.monotonic() - t0 >= limit_s):
            break
        with _AUDIO_LOCK:
            frame = sd.rec(
                capture_n,
                samplerate=capture_rate,
                channels=1,
                dtype="int16",
            )
            sd.wait()
        arr = _i16_to_uplink(frame, capture_rate)
        if vad:
            peak = float(np.max(np.abs(arr.astype(np.float32)))) / 32768.0
            if peak >= energy:
                if not heard:
                    # Reset the listen clock once speech starts so limit_s is speech cap
                    t0 = time.monotonic()
                heard = True
                silent = 0
            elif heard:
                silent += 1
                if silent >= need_silent:
                    payload = arr.tobytes()
                    conn.send(header, payload)
                    del payload
                    break
            # Before first speech: keep streaming soft noise so desktop VAD can also hear
        elif time.monotonic() - t0 >= limit_s:
            payload = arr.tobytes()
            conn.send(header, payload)
            del payload
            break
        payload = arr.tobytes()
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


def _pump(
    conn: CrateConnection,
    ready: threading.Event,
    end_talk: threading.Event,
    stop: threading.Event,
    pause_mic: threading.Event | None = None,
) -> None:
    while not stop.is_set() and not conn.closed:
        item = conn.wait_any(timeout=0.2)
        if item is None:
            continue
        header, payload = item
        ev = header.get("event")
        if ev == "play":
            if pause_mic is not None:
                pause_mic.set()
            _play_tts(header, payload)
            try:
                conn.send({"event": "play_done"})
            except (ConnectionError, OSError):
                break
        elif ev == "gesture":
            print(f"GESTURE: {header.get('name') or '?'}")
        elif ev == "ready":
            end_talk.set()
            if pause_mic is not None:
                pause_mic.clear()  # unmute after Mayor finishes
            ready.set()
            print("ready.")
        elif ev == "listen":
            print("LISTENING")
        elif ev == "speaking":
            end_talk.set()
            if pause_mic is not None:
                pause_mic.set()
            print("SPEAKING")
        elif ev == "thinking":
            end_talk.set()
            print("THINKING")
        elif ev == "hello":
            pass
        elif ev == "error":
            print(f"  desktop error: {header.get('message')}")
        # payload dropped here — never written


def _wait_trigger(gpio, stop: threading.Event) -> str:
    print("Enter = Talk" + (" (or hold the button)" if gpio is not None else "") + ", q = quit")
    while not stop.is_set() and not _SHUTDOWN.is_set():
        if gpio is not None and gpio.is_pressed:
            return "gpio"
        if _stdin_ready(0.1):
            line = sys.stdin.readline()
            if not line or line.strip().lower() in {"q", "quit", "exit"}:
                return "quit"
            return "enter"
    return "quit"



def _always_on_uplink(
    conn: CrateConnection,
    stop: threading.Event,
    pause_mic: threading.Event,
    *,
    energy: float = 0.015,
    camera: bool,
    camera_index: int,
    still: _StillAdaptive | None,
) -> None:
    """Stream PCM forever except while pause_mic (SPEAKING). Snap still on speech start and end."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        print("  (no sounddevice; always-on uplink unavailable)")
        return
    header = {
        "event": "pcm",
        "rate": UPLINK_RATE,
        "channels": 1,
        "format": UPLINK_FORMAT,
    }
    capture_rate = _probe_capture_rate() or UPLINK_RATE
    capture_n = _capture_chunk_samples(capture_rate)
    speech_hot = False
    cool = 0
    was_paused = False
    need_quiet = 6  # ~0.6s — align with desktop silence_s

    def _snap_bg(tag: str, _still=still) -> None:
        if not camera or _still is None:
            return
        # Skip if a snap is already in flight (start may still be busy when end fires — OK).
        if _SNAP_BUSY.is_set():
            return

        def _run() -> None:
            _SNAP_BUSY.set()
            try:
                jpeg, wh = _grab_jpeg(
                    camera_index, max_edge=_still.edge, quality=_still.quality
                )
                if jpeg:
                    t_send = time.monotonic()
                    conn.send({"event": "jpeg"}, jpeg)
                    _still.note_send(len(jpeg), (time.monotonic() - t_send) * 1000)
                    if wh:
                        print(
                            f"  still {wh[0]}x{wh[1]}  {len(jpeg) // 1024}KiB  "
                            f"({tag})"
                        )
                    del jpeg
            except Exception as exc:
                print(f"  (still failed: {exc})")
            finally:
                _SNAP_BUSY.clear()

        threading.Thread(target=_run, name="crate-still", daemon=True).start()

    while not stop.is_set() and not conn.closed and not _SHUTDOWN.is_set():
        if pause_mic.is_set():
            # Mayor often answers before our quiet timer — snap on duck.
            if speech_hot and not was_paused:
                _snap_bg("speech-end")
                speech_hot = False
            was_paused = True
            time.sleep(0.05)
            continue
        was_paused = False
        try:
            with _AUDIO_LOCK:
                if pause_mic.is_set():
                    continue
                frame = sd.rec(
                    capture_n,
                    samplerate=capture_rate,
                    channels=1,
                    dtype="int16",
                )
                sd.wait()
        except Exception as exc:
            print(f"  (mic error: {exc})")
            time.sleep(0.2)
            continue
        arr = _i16_to_uplink(frame, capture_rate)
        peak = float(np.max(np.abs(arr.astype(np.float32)))) / 32768.0

        if peak >= energy:
            if not speech_hot:
                # Early snap so encode/CLIP can race the rest of the utterance.
                _snap_bg("speech-start")
            speech_hot = True
            cool = 0
        elif speech_hot:
            cool += 1
            if cool >= need_quiet:
                _snap_bg("speech-end")
                speech_hot = False

        try:
            conn.send(header, arr.tobytes())
        except (ConnectionError, OSError):
            break
        del frame


def _talk(
    conn: CrateConnection,
    *,
    trigger: str,
    gpio,
    camera: bool,
    camera_index: int,
    still: _StillAdaptive | None,
    no_mic: bool,
    limit_s: float,
    end_talk: threading.Event,
    continuous: bool = False,
) -> None:
    end_talk.clear()
    if camera:
        assert still is not None
        t_jpg = time.monotonic()
        jpeg, wh = _grab_jpeg(
            camera_index, max_edge=still.edge, quality=still.quality
        )
        if jpeg:
            encode_ms = (time.monotonic() - t_jpg) * 1000
            t_send = time.monotonic()
            conn.send({"event": "jpeg"}, jpeg)
            send_ms = (time.monotonic() - t_send) * 1000
            still.note_send(len(jpeg), send_ms)
            if wh:
                print(
                    f"  still {wh[0]}x{wh[1]}  {len(jpeg) // 1024}KiB  "
                    f"encode {encode_ms:.0f}ms  send {send_ms:.0f}ms"
                )
            del jpeg
        else:
            conn.send({"event": "still"})
    conn.send({"event": "button", "state": "down"})
    if continuous:
        print("TALK — listening (pause when done; q aborts)")
    else:
        print("TALK — Enter again to stop (or release the button / wait)")

    def should_stop() -> bool:
        if end_talk.is_set():
            return True
        if trigger == "gpio" and gpio is not None and not gpio.is_pressed:
            return True
        if (not continuous) and trigger == "enter" and _stdin_ready(0):
            sys.stdin.readline()
            return True
        if continuous and _stdin_ready(0):
            line = sys.stdin.readline()
            if line and line.strip().lower() in {"q", "quit", "exit"}:
                return True
        return False

    if no_mic:
        _send_silence(conn, min(0.8, limit_s))
        # still honor a held GPIO / second Enter if the user is using a mic-less dry run
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.2 and not should_stop():
            time.sleep(0.05)
    else:
        _stream_mic(conn, should_stop, limit_s, vad=continuous)
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


def _env_int(name: str) -> int | None:
    import os

    local = _load_local_env()
    raw = os.environ.get(name) or local.get(name)
    if raw is None or raw == "":
        return None
    return int(raw)


def _list_audio_devices() -> None:
    import sounddevice as sd

    for i, d in enumerate(sd.query_devices()):
        print(
            f"{i}: {d['name']}  in={d['max_input_channels']} out={d['max_output_channels']}"
        )


def _apply_audio_devices(input_id: int | None, output_id: int | None) -> None:
    """Pin sounddevice defaults so BRIO mic + TRS speakers stick on the Pi."""
    global _PLAY_RATE, _CAPTURE_RATE
    try:
        import sounddevice as sd
    except ImportError:
        return
    if input_id is None and output_id is None:
        return
    cur_in, cur_out = sd.default.device
    if isinstance(cur_in, (list, tuple)):
        cur_in, cur_out = cur_in[0], cur_in[1] if len(cur_in) > 1 else cur_out
    sd.default.device = (
        cur_in if input_id is None else int(input_id),
        cur_out if output_id is None else int(output_id),
    )
    print(f"  audio devices: input={sd.default.device[0]} output={sd.default.device[1]}")
    _PLAY_RATE = None  # device changed — re-probe
    _CAPTURE_RATE = None
    _probe_play_rate()
    _probe_capture_rate()


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
    parser.add_argument(
        "--log-file",
        default=os.environ.get("CRATE_LOG")
        or str(_REPO / "logs" / "crate-pi.log"),
        help="Tee stdout/stderr to this file (env CRATE_LOG; default logs/crate-pi.log)",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Hands-free porch loop: after each reply, listen again (Ctrl+C / q to quit; SIGTERM/SIGHUP stop cleanly)",
    )
    parser.add_argument(
        "--max-edge",
        type=int,
        default=_env_int("CRATE_MAX_EDGE") if _env_int("CRATE_MAX_EDGE") is not None else 0,
        metavar="PX",
        help="Max JPEG long edge (0=full native; env CRATE_MAX_EDGE). Adaptive may step down if sends are slow.",
    )
    parser.add_argument(
        "--still-slow-ms",
        type=float,
        default=float(_env_int("CRATE_STILL_SLOW_MS") or 800),
        help="If encode+send exceeds this, step down still size next turn (default 800)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print sounddevice input/output ids and exit",
    )
    parser.add_argument(
        "--input",
        type=int,
        default=_env_int("CRATE_INPUT"),
        metavar="N",
        help="sounddevice input id (env CRATE_INPUT / local.env)",
    )
    parser.add_argument(
        "--output",
        type=int,
        default=_env_int("CRATE_OUTPUT"),
        metavar="N",
        help="sounddevice output id (env CRATE_OUTPUT / local.env)",
    )
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    _setup_log(args.log_file)
    _install_signal_handlers()

    if args.list_devices:
        _list_audio_devices()
        raise SystemExit(0)
    _apply_audio_devices(args.input, args.output)

    print("Gourdsworth crate client — I/O only. No models on this machine.")
    print(f"  desktop {args.host}:{args.port}")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("  LAN mode: PCM/JPEG cross the house network, not the internet. No TLS.")
    gpio = _try_gpio(args.button_pin)
    quit_ev = threading.Event()

    def _run_connected_session(conn: CrateConnection) -> str:
        """Run one desktop session. Returns 'goodbye' (stop client) or 'disconnect' (retry)."""
        stop = threading.Event()
        ready = threading.Event()
        end_talk = threading.Event()
        pause_mic = threading.Event()
        shutdown_played = False
        pump = threading.Thread(
            target=_pump,
            args=(conn, ready, end_talk, stop, pause_mic),
            name="crate-pump",
            daemon=True,
        )
        pump.start()
        uplink: threading.Thread | None = None
        exit_kind = "disconnect"
        try:
            hello = {"event": "hello", "role": "crate", "proto": PROTO}
            if args.continuous:
                hello["continuous"] = True
            conn.send(hello)
            if not ready.wait(timeout=20):
                print("No ready from desktop (is --serve-crate / --crate-echo running?)")
                return "disconnect"
            telemetry = threading.Thread(
                target=_telemetry_loop,
                args=(conn, stop),
                name="crate-telemetry",
                daemon=True,
            )
            telemetry.start()
            still_adapt = _StillAdaptive(
                start_edge=int(args.max_edge),
                slow_ms=float(args.still_slow_ms),
            )
            if not args.no_camera:
                edge = int(args.max_edge)
                print(
                    "  stills: full native (adaptive downscale if sends are slow)"
                    if edge <= 0
                    else f"  stills: max long-edge {edge}px (adaptive)"
                )
            if args.continuous:
                print(
                    "Continuous porch mode — mic always on except while the Mayor speaks."
                )
                print("  Speak anytime. Ctrl+C or q = quit")
                pause_mic.clear()
                if args.no_mic:
                    print("  (--no-mic set; continuous needs a mic)")
                    return "goodbye"
                conn.send({"event": "button", "state": "down"})
                uplink = threading.Thread(
                    target=_always_on_uplink,
                    kwargs={
                        "conn": conn,
                        "stop": stop,
                        "pause_mic": pause_mic,
                        "energy": 0.015,
                        "camera": not args.no_camera,
                        "camera_index": int(args.camera),
                        "still": still_adapt,
                    },
                    name="crate-uplink",
                    daemon=True,
                )
                uplink.start()
                while (
                    not stop.is_set()
                    and not conn.closed
                    and not quit_ev.is_set()
                    and not _SHUTDOWN.is_set()
                ):
                    if _stdin_ready(0.25):
                        line = sys.stdin.readline()
                        if not line:
                            if not sys.stdin.isatty():
                                time.sleep(0.5)
                                continue
                            break
                        if line.strip().lower() in {"q", "quit", "exit"}:
                            try:
                                conn.send({"event": "bye"})
                            except (ConnectionError, OSError):
                                pass
                            exit_kind = "goodbye"
                            break
                else:
                    if conn.closed or quit_ev.is_set():
                        exit_kind = "disconnect"
                stop.set()
                pause_mic.set()
                if exit_kind == "goodbye":
                    _play_local_shutdown(kind="goodbye")
                    shutdown_played = True
                return exit_kind

            while not quit_ev.is_set():
                ready.clear()
                trig = _wait_trigger(gpio, stop)
                if trig == "quit":
                    try:
                        conn.send({"event": "bye"})
                    except (ConnectionError, OSError):
                        pass
                    _play_local_shutdown(kind="goodbye")
                    shutdown_played = True
                    return "goodbye"
                _talk(
                    conn,
                    trigger=trig,
                    gpio=gpio,
                    camera=not args.no_camera,
                    camera_index=args.camera,
                    still=still_adapt,
                    no_mic=args.no_mic,
                    limit_s=float(args.listen_s),
                    end_talk=end_talk,
                    continuous=False,
                )
                if not ready.wait(timeout=float(args.listen_s) + 15):
                    print("  (timed out waiting for desktop ready)")
                    if conn.closed:
                        return "disconnect"
            return "disconnect"
        except (KeyboardInterrupt, ConnectionError, OSError) as exc:
            kind = "goodbye" if isinstance(exc, KeyboardInterrupt) else "disconnect"
            if isinstance(exc, KeyboardInterrupt):
                quit_ev.set()
            elif not isinstance(exc, KeyboardInterrupt):
                print(f"disconnected: {exc}")
            if not shutdown_played and kind == "goodbye":
                try:
                    pause_mic.set()
                except Exception:
                    pass
                _play_local_shutdown(kind="goodbye")
                shutdown_played = True
            return kind
        finally:
            stop.set()
            pause_mic.set()
            if _SHUTDOWN.is_set():
                try:
                    conn.send({"event": "bye"})
                except Exception:
                    pass
            try:
                conn.send({"event": "button", "state": "up"})
            except Exception:
                pass
            conn.close()
            if uplink is not None and uplink.is_alive():
                try:
                    import sounddevice as sd

                    sd.stop(ignore_errors=True)
                except Exception:
                    pass
                uplink.join(timeout=1.5)
            if _SHUTDOWN.is_set():
                pump.join(timeout=1.0)

    try:
        announce_now = True
        while not quit_ev.is_set():
            sock = _wait_for_desktop(
                args.host,
                args.port,
                quit_ev,
                announce_immediately=announce_now,
            )
            if sock is None:
                return 0
            conn = CrateConnection(sock)
            try:
                reason = _run_connected_session(conn)
            except (KeyboardInterrupt, ConnectionError, OSError) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    quit_ev.set()
                    _play_local_shutdown(kind="goodbye")
                    break
                print(f"disconnected: {exc}")
                reason = "disconnect"
            if reason == "goodbye" or quit_ev.is_set():
                break
            # Desktop dropped or never became ready — announce, then wait again.
            print("  desktop gone; will retry when it returns")
            _play_local_shutdown(kind="disconnect")
            announce_now = False  # already spoke; next wait is quieter until interval
    except KeyboardInterrupt:
        quit_ev.set()
        _play_local_shutdown(kind="goodbye")
        print()
    finally:
        # Any exit path (Ctrl+C, SIGTERM/SIGHUP, desktop gone): close audio + camera.
        _cleanup_devices()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Signal/Ctrl+C before the connect loop (e.g. during the device probe).
        _cleanup_devices()
        raise SystemExit(0 if _SHUTDOWN.is_set() else 130)
