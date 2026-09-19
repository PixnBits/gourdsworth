from __future__ import annotations

from time import perf_counter

import numpy as np


def _sd():
    import sounddevice as sd

    return sd


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


def list_devices() -> str:
    """Return a human-readable list of sounddevice input/output devices."""
    sd = _sd()
    lines = ["sounddevice devices (use --input N / --output N):", ""]
    devices = sd.query_devices()
    default_in, default_out = sd.default.device
    for i, dev in enumerate(devices):
        marks = []
        if i == default_in:
            marks.append("default-in")
        if i == default_out:
            marks.append("default-out")
        mark = f"  [{', '.join(marks)}]" if marks else ""
        lines.append(
            f"  {i:3d}: {dev['name']}  "
            f"in={dev['max_input_channels']} out={dev['max_output_channels']}  "
            f"{dev['default_samplerate']}Hz{mark}"
        )
    return "\n".join(lines)


def set_devices(input_id: int | None = None, output_id: int | None = None) -> None:
    sd = _sd()
    cur_in, cur_out = sd.default.device
    if isinstance(cur_in, (list, tuple)):
        cur_in = cur_in[0] if cur_in else None
    if isinstance(cur_out, (list, tuple)):
        cur_out = cur_out[0] if cur_out else None
    sd.default.device = (
        cur_in if input_id is None else input_id,
        cur_out if output_id is None else output_id,
    )


def record_ptt(
    sample_rate: int,
    limit_s: float,
    input_device: int | None = None,
) -> tuple[np.ndarray, float]:
    """Record until Enter is pressed again, or limit_s."""
    sd = _sd()
    print("  listening… press Enter when done (or wait for the cap)")
    chunks: list[np.ndarray] = []
    started = perf_counter()
    block = int(sample_rate * 0.1)
    kwargs = {}
    if input_device is not None:
        kwargs["device"] = input_device

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        chunks.append(indata.copy().reshape(-1))

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        callback=callback,
        blocksize=block,
        **kwargs,
    ):
        while perf_counter() - started < limit_s:
            remaining = limit_s - (perf_counter() - started)
            try:
                import select
                import sys

                ready, _, _ = select.select([sys.stdin], [], [], min(0.1, remaining))
                if ready:
                    sys.stdin.readline()
                    break
            except (ImportError, OSError):
                sd.sleep(int(min(0.1, remaining) * 1000))
    audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
    return audio, (perf_counter() - started) * 1000


def record_vad(
    sample_rate: int,
    limit_s: float,
    silence_s: float,
    energy_threshold: float,
    input_device: int | None = None,
) -> tuple[np.ndarray, float]:
    sd = _sd()
    print("  listening… speak, then pause")
    chunks: list[np.ndarray] = []
    started = perf_counter()
    voiced = False
    silent_run = 0.0
    block_s = 0.1
    block = int(sample_rate * block_s)
    kwargs = {}
    if input_device is not None:
        kwargs["device"] = input_device
    while perf_counter() - started < limit_s:
        frame = sd.rec(block, samplerate=sample_rate, channels=1, dtype="float32", **kwargs)
        sd.wait()
        mono = frame.reshape(-1)
        chunks.append(mono)
        level = rms(mono)
        if level >= energy_threshold:
            voiced = True
            silent_run = 0.0
        elif voiced:
            silent_run += block_s
            if silent_run >= silence_s:
                break
    audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
    return audio, (perf_counter() - started) * 1000


def play(samples: np.ndarray, sample_rate: int, output_device: int | None = None) -> float:
    sd = _sd()
    t0 = perf_counter()
    if samples.size == 0:
        return 0.0
    kwargs = {}
    if output_device is not None:
        kwargs["device"] = output_device
    sd.play(samples.astype(np.float32), sample_rate, **kwargs)
    sd.wait()
    return (perf_counter() - t0) * 1000
