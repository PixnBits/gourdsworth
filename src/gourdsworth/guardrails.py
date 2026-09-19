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


def parse_reply(raw: str) -> tuple[str, str]:
    gesture = "stamp"
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    spoken: list[str] = []
    for line in lines:
        if line.upper().startswith("GESTURE:"):
            gesture = line.split(":", 1)[1].strip().lower() or "stamp"
            continue
        spoken.append(line)
    line = strip_markdown(" ".join(spoken))
    words = line.split()
    if len(words) > 22:
        line = " ".join(words[:20]).rstrip(".,;") + "."
    return line, gesture
