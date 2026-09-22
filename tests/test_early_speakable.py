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


def test_early_no_bare_word_chop():
    words = " ".join(f"w{i}" for i in range(15))
    assert early_speakable(words, min_words=12) is None


def test_early_none_while_short():
    assert early_speakable("Hi there", min_words=12) is None


def test_early_comma_after_min_words():
    raw = (
        "You look truly wonderful tonight in that handmade cape of many bright colors, "
        "and I hereby cheer your costume"
    )
    got = early_speakable(raw, min_words=12)
    assert got is not None
    assert got.endswith(",")
    assert "colors," in got


def test_early_cheer_line_does_not_split_at_to():
    partial = (
        'Here\'s your Trick-or-Treat welcome: "Gourd-geous in costume, you are '
        "hereby permitted to "
    )
    got = early_speakable(partial, min_words=12)
    if got:
        assert not got.rstrip().endswith("to")
        assert "permitted to" not in got


def test_early_cheer_full_flushes_at_sentence():
    raw = (
        'Here\'s your Trick-or-Treat welcome: "Gourd-geous in costume, you are '
        'hereby permitted to collect treats in Pumpkinville."'
    )
    got = early_speakable(raw, min_words=12)
    assert got is not None
    assert "Pumpkinville" in got
    assert not got.rstrip().endswith("to")


def test_remainder_after():
    full = "Ah a fine costume for Pumpkinville please proceed to candy"
    pref = "Ah a fine costume for Pumpkinville please proceed to"
    assert remainder_after(full, pref) == "candy"


def test_parse_then_speak_never_says_gesture():
    line, gesture = parse_reply("A fine young citizen! GESTURE: stamp")
    assert gesture == "stamp"
    assert "gesture" not in line.lower()
