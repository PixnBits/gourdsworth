from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter

import numpy as np
import soundfile as sf


class Speaker:
    def __init__(self, engine: str, voice: str):
        self.engine = engine
        self.voice = voice
        self._piper = None
        self._voice_path: Path | None = None
        if engine == "piper":
            self._init_piper(voice)

    def _init_piper(self, voice: str) -> None:
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise RuntimeError("piper-tts is not installed") from exc
        cache = Path.home() / ".local" / "share" / "piper" / "voices"
        cache.mkdir(parents=True, exist_ok=True)
        onnx = cache / f"{voice}.onnx"
        if not onnx.exists():
            print(f"  downloading Piper voice {voice}…")
            subprocess.check_call(["python", "-m", "piper.download_voices", voice])
            for candidate in (Path.cwd() / f"{voice}.onnx", onnx):
                if candidate.exists():
                    if candidate != onnx:
                        shutil.move(str(candidate), onnx)
                        json_src = candidate.with_suffix(".onnx.json")
                        if json_src.exists():
                            shutil.move(str(json_src), onnx.with_suffix(".onnx.json"))
                    break
        if not onnx.exists():
            found = list(Path.home().rglob(f"{voice}.onnx"))
            if found:
                onnx = found[0]
        if not onnx.exists():
            raise RuntimeError(f"Could not locate Piper voice {voice}")
        self._voice_path = onnx
        self._piper = PiperVoice.load(str(onnx))

    def synthesize(self, text: str) -> tuple[np.ndarray, int, float, float]:
        t0 = perf_counter()
        first = None
        if self.engine == "espeak" or self._piper is None:
            audio, rate = self._espeak(text)
            first = (perf_counter() - t0) * 1000
            return audio, rate, first, (perf_counter() - t0) * 1000

        chunks: list[np.ndarray] = []
        rate = 22050
        for chunk in self._piper.synthesize(text):
            if first is None:
                first = (perf_counter() - t0) * 1000
            samples = np.asarray(chunk.audio_float_array, dtype=np.float32)
            rate = int(chunk.sample_rate)
            chunks.append(samples)
        audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        return audio, rate, (first or 0.0), (perf_counter() - t0) * 1000

    def _espeak(self, text: str) -> tuple[np.ndarray, int]:
        if not shutil.which("espeak-ng") and not shutil.which("espeak"):
            raise RuntimeError("No Piper voice and no espeak/espeak-ng fallback")
        bin_ = shutil.which("espeak-ng") or shutil.which("espeak")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            subprocess.check_call(
                [bin_, "-v", "en-us", "-s", "155", "-w", tmp.name, text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            audio, rate = sf.read(tmp.name, dtype="float32")
        return np.asarray(audio, dtype=np.float32), int(rate)
