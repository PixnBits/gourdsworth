from __future__ import annotations

import re
from difflib import SequenceMatcher
from time import perf_counter

import numpy as np

# Bias Whisper toward porch vocabulary (does not force matches).
PORCH_PROMPT = (
    "Halloween porch in Pumpkinville. Children say trick or treat, smell my feet, "
    "candy, costume, pumpkin, jack-o'-lantern, Mayor Gourdsworth, license, please."
)
PORCH_HOTWORDS = (
    "trick or treat candy costume pumpkin Gourdsworth Pumpkinville "
    "jack-o'-lantern license smell my feet"
)

# Short utterances often mangled by tiny/base — map near-misses back.
_PORCH_PHRASES = (
    "trick or treat",
    "trick or treat smell my feet",
    "do you have candy",
    "where is the candy",
    "peter peter pumpkin eater",
)


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def porch_correct(text: str) -> str:
    """Light correction for common porch mishears without rewriting long sentences."""
    raw = (text or "").strip()
    if not raw:
        return raw
    n = _norm(raw)
    words = n.split()
    if not words or len(words) > 10:
        return raw

    # Hard aliases seen in live logs
    aliases = {
        "check our tree": "trick or treat",
        "check our treat": "trick or treat",
        "take our tree": "trick or treat",
        "take our treat": "trick or treat",
        "or trick": "trick or treat",
        "dreaming trick": "trick or treat",
        "trick boards read": "trick or treat",
        "contour": "trick or treat",
        "peter peter punk and peter": "peter peter pumpkin eater",
        "peter peter pumpkin ear": "peter peter pumpkin eater",
        "peter peter pumped in either": "peter peter pumpkin eater",
        "theater theater pump and theater": "peter peter pumpkin eater",
    }
    if n in aliases:
        return aliases[n]

    best = None
    best_score = 0.0
    for phrase in _PORCH_PHRASES:
        score = SequenceMatcher(None, n, phrase).ratio()
        if score > best_score:
            best_score = score
            best = phrase
    # Only rewrite when clearly a near-miss short phrase
    if best and best_score >= 0.72 and len(words) <= 8:
        # Preserve light punctuation style
        if best == "trick or treat":
            return "Trick or treat."
        if best == "peter peter pumpkin eater":
            return "Peter Peter pumpkin eater."
        return best.capitalize() + ("?" if raw.endswith("?") else ".")
    return raw


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
        # Prefer a bit more search than beam_size=1; still porch-fast on base.en
        beam = 1 if self.model_name.startswith("tiny") else 5
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            beam_size=beam,
            vad_filter=True,
            without_timestamps=True,
            initial_prompt=PORCH_PROMPT,
            hotwords=PORCH_HOTWORDS,
            condition_on_previous_text=False,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        text = porch_correct(text)
        return text, (perf_counter() - t0) * 1000
