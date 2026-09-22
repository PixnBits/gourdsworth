# Spoken unlock for porch diagnostics. Match only a complete normalized phrase;
# character names and ordinary mentions inside longer sentences must not unlock it.
_DEBUG_PASS = {
    "tri state area",
    "price state area",
    "try state area",
    "tri stick area",
}


def looks_debug_pass(text: str) -> bool:
    """True only for the exact tri-state-area phrase or observed STT variants."""
    normalized = "".join(
        ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in (text or "")
    )
    normalized = " ".join(normalized.split())
    if normalized.startswith("the "):
        normalized = normalized[4:]
    return normalized in _DEBUG_PASS
