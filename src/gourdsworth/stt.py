from __future__ import annotations

from time import perf_counter

import numpy as np


class SpeechToText:
    def __init__(self, model_name: str, device: str, compute_type: str):
        from faster_whisper import WhisperModel

        if device == "auto":
            device = "cpu"
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self.model_name = model_name

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> tuple[str, float]:
        t0 = perf_counter()
        if audio.size < sample_rate * 0.2:
            return "", (perf_counter() - t0) * 1000
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        peak = float(np.max(np.abs(audio))) or 1.0
        if peak > 1.0:
            audio = audio / peak
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=True,
            without_timestamps=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text, (perf_counter() - t0) * 1000
