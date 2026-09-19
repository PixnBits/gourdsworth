from gourdsworth.guardrails import early_speakable, remainder_after, parse_reply


def test_early_on_sentence():
    raw = "Welcome to Pumpkinville, citizens!\nGESTURE: stamp"
    got = early_speakable(raw, min_words=12)
    assert got == "Welcome to Pumpkinville, citizens!"
    assert "GESTURE" not in got


def test_early_strips_inline_gesture():
    raw = "Welcome to Pumpkinville, citizens! GESTURE: wave More later."
    got = early_speakable(raw, min_words=12)
    assert got is not None
    assert "GESTURE" not in got.upper()
    assert "wave" not in got.lower()


def test_early_on_twelve_words():
    words = " ".join(f"w{i}" for i in range(15))
    got = early_speakable(words, min_words=12)
    assert got == " ".join(f"w{i}" for i in range(12))


def test_early_none_while_short():
    assert early_speakable("Hi there", min_words=12) is None


def test_remainder_after():
    full = "Ah a fine costume for Pumpkinville please proceed to candy"
    pref = "Ah a fine costume for Pumpkinville please proceed to"
    assert remainder_after(full, pref) == "candy"


def test_parse_then_speak_never_says_gesture():
    line, gesture = parse_reply(
        "A fine young citizen! GESTURE: stamp"
    )
    assert gesture == "stamp"
    assert "gesture" not in line.lower()
