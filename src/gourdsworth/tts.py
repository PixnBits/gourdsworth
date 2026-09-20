from __future__ import annotations

import re

import shutil
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter

import numpy as np
import soundfile as sf

try:
    from piper.config import SynthesisConfig
except Exception:  # pragma: no cover
    SynthesisConfig = None  # type: ignore




_GOURD_GEOUS = re.compile(r"\b[Gg]ourd[-\s]*geous\b")


def rewrite_puns_for_tts(text: str) -> str:
    """Speak porch puns / names so Piper lands them (display text can stay normal)."""
    def repl(m: re.Match[str]) -> str:
        raw = m.group(0)
        if raw[0].isupper():
            return "Gourd----geous"
        return "gourd----geous"

    text = _GOURD_GEOUS.sub(repl, text or "")
    # Help "Gourdsworth" land as GORRD-sworth (not "goats worth")
    text = re.sub(r"\bGourdsworth\b", "Gorrdsworth", text)
    text = re.sub(r"\bgourdsworth\b", "gorrdsworth", text)
    return text


def split_bang_ending(text: str) -> str:
    """Make trailing ! land as its own short beat (Piper often flattens ', I see!').

    Preferred porch shape: '...their game. I see!' instead of '...game, I see!'.
    Only splits when the final tag after a comma/dash is short (1–5 words).
    """
    text = (text or "").strip()
    if not text.endswith("!"):
        return text
    body = text[:-1].rstrip()
    for sep in (", ", " — ", " – ", " - "):
        if sep not in body:
            continue
        head, tail = body.rsplit(sep, 1)
        tail = tail.strip()
        n = len(tail.split())
        if 1 <= n <= 5 and head.strip():
            if tail and tail[0].islower():
                tail = tail[0].upper() + tail[1:]
            return f"{head.rstrip(' .,;:')}. {tail}!"
    return text


class Speaker:
    def __init__(self, engine: str, voice: str):
        self.engine = engine
        self.voice = voice
        self._piper = None
        self._voice_path: Path | None = None
        self._use_cli = False
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
        # Some piper-tts builds expose synthesize; others only CLI / stream_raw.
        if not hasattr(self._piper, "synthesize"):
            self._use_cli = True
            print("  PiperVoice.synthesize missing; will use piper CLI fallback")

    def synthesize(self, text: str) -> tuple[np.ndarray, int, float, float]:
        t0 = perf_counter()
        first = None
        text = split_bang_ending(text)
        text = rewrite_puns_for_tts(text)
        if self.engine == "espeak" or (self._piper is None and not self._use_cli):
            audio, rate = self._espeak(text)
            first = (perf_counter() - t0) * 1000
            return audio, rate, first, (perf_counter() - t0) * 1000

        if self._use_cli or not hasattr(self._piper, "synthesize"):
            audio, rate = self._piper_cli(text)
            first = (perf_counter() - t0) * 1000
            return audio, rate, first, (perf_counter() - t0) * 1000

        try:
            chunks: list[np.ndarray] = []
            rate = 22050
            syn_cfg = None
            if SynthesisConfig is not None and text.rstrip().endswith("!"):
                # Slightly snappier on exclamations; split_bang_ending did the prosody shape
                syn_cfg = SynthesisConfig(length_scale=0.92)
            for chunk in self._piper.synthesize(text, syn_config=syn_cfg):
                if first is None:
                    first = (perf_counter() - t0) * 1000
                samples = self._chunk_to_float(chunk)
                rate = int(getattr(chunk, "sample_rate", rate) or rate)
                chunks.append(samples)
            audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
            return audio, rate, (first or 0.0), (perf_counter() - t0) * 1000
        except Exception as exc:
            print(f"  Piper Python synthesize failed ({exc}); falling back to CLI")
            self._use_cli = True
            audio, rate = self._piper_cli(text)
            first = (perf_counter() - t0) * 1000
            return audio, rate, first, (perf_counter() - t0) * 1000

    @staticmethod
    def _chunk_to_float(chunk) -> np.ndarray:
        if hasattr(chunk, "audio_float_array"):
            return np.asarray(chunk.audio_float_array, dtype=np.float32)
        if hasattr(chunk, "audio_int16_bytes"):
            raw = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
            return (raw.astype(np.float32) / 32768.0)
        if isinstance(chunk, (bytes, bytearray)):
            raw = np.frombuffer(chunk, dtype=np.int16)
            return raw.astype(np.float32) / 32768.0
        return np.asarray(chunk, dtype=np.float32)

    def _piper_cli(self, text: str) -> tuple[np.ndarray, int]:
        """Fall back to `piper` CLI writing WAV to an unlinked NamedTemporaryFile."""
        if self._voice_path is None:
            raise RuntimeError("No Piper voice path for CLI fallback")
        piper_bin = shutil.which("piper") or shutil.which("piper-tts")
        if not piper_bin:
            raise RuntimeError(
                "PiperVoice.synthesize unavailable and no `piper` CLI on PATH"
            )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                [
                    piper_bin,
                    "--model",
                    str(self._voice_path),
                    "--output_file",
                    tmp_path,
                ],
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if proc.returncode != 0:
                proc2 = subprocess.run(
                    [
                        piper_bin,
                        "--model",
                        str(self._voice_path),
                        "--output_file",
                        "-",
                    ],
                    input=text.encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if proc2.returncode != 0 or not proc2.stdout:
                    err = (proc.stderr or proc2.stderr or b"").decode("utf-8", "replace")
                    raise RuntimeError(f"piper CLI failed: {err.strip() or proc.returncode}")
                rate = int(getattr(self._piper, "config", None) and getattr(self._piper.config, "sample_rate", 22050) or 22050)
                raw = np.frombuffer(proc2.stdout, dtype=np.int16)
                return raw.astype(np.float32) / 32768.0, rate
            audio, rate = sf.read(tmp_path, dtype="float32")
            return np.asarray(audio, dtype=np.float32).reshape(-1), int(rate)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

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
