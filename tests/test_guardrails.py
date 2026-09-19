from gourdsworth.guardrails import looks_distress, model_went_dark, parse_reply


def test_distress():
    assert looks_distress("I'm lost")
    assert not looks_distress("I love candy")


def test_dark_output_caught():
    assert model_went_dark("I will chase you home")
    assert model_went_dark("What is your name, child?")


def test_parse_truncates_and_reads_gesture():
    line, gesture = parse_reply(
        "By the power of this porch you are licensed to collect many many many many many sweets tonight my dear.\nGESTURE: stamp"
    )
    assert gesture == "stamp"
    assert len(line.split()) <= 20
    assert "GESTURE" not in line.upper()


def test_parse_inline_gesture_wave():
    line, gesture = parse_reply(
        "You're a vision of sugary specterhood. GESTURE: wave"
    )
    assert gesture == "wave"
    assert "GESTURE" not in line.upper()
    assert "wave" not in line.lower() or "vision" in line.lower()
    assert line.startswith("You're a vision")


def test_parse_inline_gesture_same_line_stamp():
    line, gesture = parse_reply(
        "Your costume is a marvel. You've earned a license. GESTURE: stamp"
    )
    assert gesture == "stamp"
    assert "GESTURE" not in line.upper()
    assert "license" in line.lower()


def test_parse_unknown_gesture_defaults_stamp():
    line, gesture = parse_reply("Hello citizens. GESTURE: moonwalk")
    assert gesture == "stamp"
    assert "GESTURE" not in line.upper()
    assert "moonwalk" not in line.lower()


def test_clip_prefers_full_sentences():
    raw = (
        "A classic rhyme, well-executed! "
        "I'm delighted to issue you a Trick-or-Treat License. "
        "Off you go to the candy bowl, where."
    )
    line, _ = parse_reply(raw + "\nGESTURE: stamp")
    assert "where" not in line.lower()
    assert line.endswith(("!", "."))
    assert len(line.split()) <= 20
    # Should keep the first two sentences if they fit
    assert "classic rhyme" in line.lower()
    assert "license" in line.lower()


def test_clip_drops_dangling_where():
    from gourdsworth.guardrails import clip_spoken
    long = (
        "Off you go to the candy bowl where the treats await you "
        "and also more words padding out this sentence beyond twenty"
    )
    out = clip_spoken(long, max_words=20)
    assert not out.lower().rstrip(".").endswith("where")
    assert len(out.split()) <= 20


def test_parse_wave_colon_leak():
    line, gesture = parse_reply("Wave: wave")
    assert gesture == "wave"
    assert "wave: wave" not in line.lower()
    assert line  # some spoken fallback


def test_parse_gesture_only_line():
    line, gesture = parse_reply("GESTURE: bow")
    assert gesture == "bow"
    assert "gesture" not in line.lower()
