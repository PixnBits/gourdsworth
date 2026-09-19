from __future__ import annotations

import re

BANNED = re.compile(
    r"\b("
    r"kill|murder|blood|gore|guts|stab|gun|knife|bomb|"
    r"suicide|die|dead kid|eat you|chase you|trap you|"
    r"where you live|address|phone number|school name|"
    r"take off|undress|secret password only you"
    r")\b",
    re.I,
)

DISTRESS = re.compile(
    r"\b(help me|i'm lost|im lost|i'm hurt|im hurt|it hurts|"
    r"i'm scared|im scared|don't leave|dont leave)\b",
    re.I,
)

NAME_FISHING = re.compile(
    r"\b(what('?s| is) your name|how old are you|what school|where do you live)\b",
    re.I,
)


def looks_distress(text: str) -> bool:
    return bool(DISTRESS.search(text or ""))


def looks_banned(text: str) -> bool:
    return bool(BANNED.search(text or ""))


def model_went_dark(text: str) -> bool:
    return looks_banned(text) or NAME_FISHING.search(text or "") is not None


def strip_markdown(text: str) -> str:
    text = text.replace("*", "").replace("#", "").replace("`", "")
    return " ".join(text.split())


GESTURE_RE = re.compile(
    r"(?:^|\s)GESTURE\s*:\s*([A-Za-z]+)\b",
    re.I | re.M,
)
ALLOWED_GESTURES = frozenset({"stamp", "wave", "think", "laugh", "bow", "listen"})


def _extract_gesture(raw: str) -> tuple[str, str]:
    """Return (text_without_gesture, gesture_name). Default gesture is stamp."""
    gesture = "stamp"
    matches = list(GESTURE_RE.finditer(raw or ""))
    if matches:
        name = matches[-1].group(1).strip().lower()
        if name in ALLOWED_GESTURES:
            gesture = name
        # Strip all GESTURE tags from spoken text
        cleaned = GESTURE_RE.sub(" ", raw or "")
    else:
        cleaned = raw or ""
    return cleaned, gesture


def parse_reply(raw: str) -> tuple[str, str]:
    cleaned, gesture = _extract_gesture(raw)
    # Drop empty lines left behind after tag removal
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    line = strip_markdown(" ".join(lines))
    # Belt-and-suspenders: never speak the word GESTURE
    line = re.sub(r"\bGESTURE\b\s*:?", "", line, flags=re.I)
    line = strip_markdown(line)
    words = line.split()
    if len(words) > 22:
        line = " ".join(words[:20]).rstrip(".,;") + "."
    return line, gesture


def early_speakable(raw: str, *, min_words: int = 12) -> str | None:
    """Return a speakable prefix once we have a sentence or min_words (M1 early TTS)."""
    if not raw:
        return None
    cleaned, _gesture = _extract_gesture(raw)
    text = strip_markdown(cleaned).strip()
    if not text:
        return None
    # Prefer first sentence boundary after a few words
    for i, ch in enumerate(text):
        if ch in ".!?" and len(text[: i + 1].split()) >= 3:
            return text[: i + 1].strip()
    words = text.split()
    if len(words) >= min_words:
        return " ".join(words[:min_words])
    return None


def remainder_after(full: str, spoken_prefix: str) -> str:
    """Words in full not already covered by spoken_prefix."""
    full_words = (full or "").split()
    pref_words = (spoken_prefix or "").split()
    if not pref_words:
        return full or ""
    # If full starts with prefix words, drop them
    n = len(pref_words)
    if full_words[:n] == pref_words:
        return " ".join(full_words[n:]).strip()
    # Fallback: if prefix is a substring, cut once
    if spoken_prefix and spoken_prefix in (full or ""):
        return (full.split(spoken_prefix, 1)[1]).strip(" .,;:")
    return ""
