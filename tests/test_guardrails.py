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
