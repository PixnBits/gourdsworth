from __future__ import annotations

from time import perf_counter

import numpy as np
import sounddevice as sd


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


def record_ptt(sample_rate: int, limit_s: float) -> tuple[np.ndarray, float]:
    """Record until Enter is pressed again, or limit_s."""
    print("  listening… press Enter when done (or wait for the cap)")
    chunks: list[np.ndarray] = []
    started = perf_counter()
    block = int(sample_rate * 0.1)

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        chunks.append(indata.copy().reshape(-1))

    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32", callback=callback, blocksize=block):
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
) -> tuple[np.ndarray, float]:
    print("  listening… speak, then pause")
    chunks: list[np.ndarray] = []
    started = perf_counter()
    voiced = False
    silent_run = 0.0
    block_s = 0.1
    block = int(sample_rate * block_s)
    while perf_counter() - started < limit_s:
        frame = sd.rec(block, samplerate=sample_rate, channels=1, dtype="float32")
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


def play(samples: np.ndarray, sample_rate: int) -> float:
    t0 = perf_counter()
    if samples.size == 0:
        return 0.0
    sd.play(samples.astype(np.float32), sample_rate)
    sd.wait()
    return (perf_counter() - t0) * 1000
