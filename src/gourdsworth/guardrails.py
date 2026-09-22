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


# Model sometimes emits "GESTURE: wave", "Gesture: wave", or even "Wave: wave".
GESTURE_RE = re.compile(
    r"(?:^|\s)(?:GESTURE|Gesture|gesture|Wave|WAVE)\s*:\s*([A-Za-z]+)\b",
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
        # Note: "with" is intentionally NOT here — distress must keep
        # "Tell a grown-up you came with."
        "a", "an", "the", "to", "of", "and", "or", "but", "for",
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



_GESTURE_WORDS = frozenset({"stamp", "wave", "think", "laugh", "bow", "listen"})



def finish_spoken(line: str) -> str:
    """Avoid hanging mid-thought endings like '...License:'."""
    line = (line or "").strip()
    if not line:
        return line
    line = line.rstrip(" :;—–-")
    if line and line[-1] not in ".!?":
        line = line + "."
    return line


def sanitize_spoken(line: str) -> str:
    """Strip leaked gesture *tags*, not English verbs like "stamp" / "wave".

    Keep mid-sentence words ("I'll stamp your license"). Only drop:
    - leading Gesture:/Wave:/Stamp: prefixes
    - a gesture word dangling after sentence-end punctuation
    - a lone ALL-CAPS gesture token, or a whole utterance that is just one
    """
    line = (line or "").strip()
    if not line:
        return line
    line = re.sub(
        r"^(?:GESTURE|Gesture|Wave|Stamp|Think|Laugh|Bow|Listen)\s*[:.]\s*",
        "",
        line,
        flags=re.I,
    ).strip()
    # "...for you. stamp" / "...candy. Wave" — leaked tag after a sentence
    line = re.sub(
        r"(?<=[.!?])\s+(?:stamp|wave|think|laugh|bow|listen)\s*[.!?]?\s*$",
        "",
        line,
        flags=re.I,
    ).strip()
    words = line.split()
    cleaned: list[str] = []
    for w in words:
        core = w.strip(".,!?;:\"'")
        low = core.lower()
        if low in _GESTURE_WORDS and core.isupper() and len(core) > 1:
            continue
        cleaned.append(w)
    line = " ".join(cleaned).strip()
    cores = [w.strip(".,!?;:\"'").lower() for w in line.split()]
    if len(cores) == 1 and cores[0] in _GESTURE_WORDS:
        line = ""
    letters = [c for c in line if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7 and len(letters) > 4:
        line = line.title()
    return strip_markdown(line)

def parse_reply(raw: str) -> tuple[str, str]:
    cleaned, gesture = _extract_gesture(raw)
    # Drop empty lines left behind after tag removal
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    line = strip_markdown(" ".join(lines))
    # Belt-and-suspenders: never speak the word GESTURE
    line = re.sub(r"\bGESTURE\b\s*:?", "", line, flags=re.I)
    line = strip_markdown(line)
    line = clip_spoken(line, max_words=20)
    line = sanitize_spoken(line)
    line = finish_spoken(line)
    # If the model only emitted a gesture tag, don't speak "Wave: wave"
    if not line:
        line = "Stamp applied. Candy awaits."
    # Catch residual "Wave: wave" / "Gesture: stamp" left as spoken text
    only_gesture = re.fullmatch(
        r"(?:GESTURE|Gesture|gesture|Wave|WAVE)\s*:\s*([A-Za-z]+)\s*",
        line,
        flags=re.I,
    )
    if only_gesture:
        name = only_gesture.group(1).strip().lower()
        if name in ALLOWED_GESTURES:
            gesture = name
        line = "Stamp applied. Candy awaits."
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


# Spoken unlock for porch diagnostics (Phineas and Ferb). Keep the list small.
_DEBUG_PASS = {
    "perry",
    "perry the platypus",
    "agent p",
    "agent perry",
    "doofenshmirtz",
    "heinz doofenshmirtz",
    "candace",
    "phineas",
    "ferb",
    "isabella",
    "baljeet",
    "buford",
    "major monogram",
    "dr doofenshmirtz",
}


def looks_debug_pass(text: str) -> bool:
    """True when the kid (or operator) says a secret character passcode."""
    n = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in (text or ""))
    n = " ".join(n.split())
    if not n:
        return False
    if n in _DEBUG_PASS:
        return True
    # Allow "hey perry" / "code perry the platypus"
    for phrase in _DEBUG_PASS:
        if phrase in n:
            return True
    return False


# Personal trivia about the Mayor — fixed lore, not costume talk.
_BACKSTORY_KEYS = (
    ("favorite_color", re.compile(
        r"\b(favou?rite|fav)\s+colou?r\b|"
        r"\bwhat colou?r (do you like|is your favou?rite|are you)\b|"
        r"\byour favou?rite colou?r\b",
        re.I,
    )),
    ("age", re.compile(
        r"\bhow old (are you|is the mayor)\b|"
        r"\bwhat('?s| is) your age\b|"
        r"\bare you (old|ancient)\b",
        re.I,
    )),
    ("what_are_you", re.compile(
        r"\b(are you|what are you) (a )?(real )?(pumpkin|gourd|jack[- ]?o[- ]?lantern)\b|"
        r"\bare you (real|alive|a robot|ai|an ai)\b|"
        r"\bwhat are you\b",
        re.I,
    )),
    ("food", re.compile(
        r"\b(favou?rite|fav)\s+(food|snack|candy|treat)\b|"
        r"\bdo you eat\b|"
        r"\bwhat do you (eat|like to eat)\b",
        re.I,
    )),
    ("pet", re.compile(
        r"\b(do you have|got) (a )?(pet|pets|cat|dog)\b|"
        r"\byour (pet|pets)\b",
        re.I,
    )),
    ("job", re.compile(
        r"\bwhat do you do\b|"
        r"\b(are you|you('?re| are)) (the )?mayor\b|"
        r"\bhow (did|do) you (become|get to be) mayor\b",
        re.I,
    )),
    ("home", re.compile(
        r"\bwhere (do you live|are you from)\b|"
        r"\byour (home|hometown|town)\b",
        re.I,
    )),
)


def match_backstory(text: str) -> str | None:
    """Return a backstory key if the visitor asked personal trivia about the Mayor."""
    raw = text or ""
    for key, rx in _BACKSTORY_KEYS:
        if rx.search(raw):
            return key
    return None

