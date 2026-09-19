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


# Light porch teasing / middle-school digs aimed at the Mayor — not distress, not banned dark content.
TEASE = re.compile(
    r"\b("
    r"poo[- ]?poo|poopy|butt|fart|stinky|smell my feet|"
    r"you('?re| are) (a )?(dummy|idiot|stupid|dumb|fool|loser|poop|butthead|poo|"
    r"stupid head|poo-poo head|poopo head)|"
    r"stupid (pumpkin|mayor|gourd)|"
    r"you suck|shut up|nerd|noob|"
    r"give me all (your|the) candy|i('?m| am) taking (all|everything)"
    r")\b",
    re.I,
)


def looks_tease(text: str) -> bool:
    """True for light insults / candy demands aimed at the Mayor bit."""
    return bool(TEASE.search(text or ""))



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



_DANGLING = frozenset(
    {
        "a", "an", "the", "to", "of", "and", "or", "but", "for", "with",
        "your", "my", "our", "in", "on", "at", "as", "by", "from", "into",
        "uh", "um", "where", "when", "who", "whom", "whose", "which", "that",
        "this", "these", "those", "so", "if", "than", "then", "very",
    }
)


def _trim_dangling(words: list[str]) -> list[str]:
    out = list(words)
    while len(out) > 8 and out[-1].lower().rstrip(".,!;:…") in _DANGLING:
        out.pop()
    return out


def clip_spoken(line: str, max_words: int = 20) -> str:
    """Keep ≤max_words, preferring whole sentences; never end on a dangling word."""
    line = (line or "").strip()
    if not line:
        return line
    words = line.split()
    if len(words) <= max_words:
        trimmed = _trim_dangling(words)
        if len(trimmed) < len(words):
            return " ".join(trimmed).rstrip(".,;:") + "."
        return line

    parts = re.split(r"(?<=[.!?])\s+", line)
    kept: list[str] = []
    count = 0
    for sent in parts:
        w = sent.split()
        if not w:
            continue
        if count + len(w) <= max_words:
            kept.append(sent.strip())
            count += len(w)
        else:
            break
    if kept:
        last = kept[-1].split()
        if last and last[-1].lower().rstrip(".,!;:…") in _DANGLING:
            if len(kept) > 1:
                kept = kept[:-1]
            else:
                trimmed = _trim_dangling(last)
                return " ".join(trimmed).rstrip(".,;:") + "."
        return " ".join(kept)

    chunk = _trim_dangling(words[:max_words])
    return " ".join(chunk).rstrip(".,;:") + "."


def parse_reply(raw: str) -> tuple[str, str]:
    cleaned, gesture = _extract_gesture(raw)
    # Drop empty lines left behind after tag removal
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    line = strip_markdown(" ".join(lines))
    # Belt-and-suspenders: never speak the word GESTURE
    line = re.sub(r"\bGESTURE\b\s*:?", "", line, flags=re.I)
    line = strip_markdown(line)
    line = clip_spoken(line, max_words=20)
    return line, gesture


def early_speakable(raw: str, *, min_words: int = 12) -> str | None:
    """Return a speakable prefix at a prosodic boundary (M1 early TTS).

    Flush only at `.?!` or a comma — never a bare N-word chop mid-phrase
    (that was splitting "...permitted to" / "collect treats...").
    `min_words` is a floor before we accept a comma flush so we don't
    speak a tiny fragment.
    """
    if not raw:
        return None
    cleaned, _gesture = _extract_gesture(raw)
    text = strip_markdown(cleaned).strip()
    if not text:
        return None
    # Strong boundary: first sentence end after a few words
    for i, ch in enumerate(text):
        if ch in ".!?" and len(text[: i + 1].split()) >= 3:
            return text[: i + 1].strip()
    # Softer boundary: comma, but only once we have enough words
    for i, ch in enumerate(text):
        if ch == "," and len(text[: i + 1].split()) >= min_words:
            return text[: i + 1].strip()
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
