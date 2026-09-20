from gourdsworth.guardrails import sanitize_spoken, parse_reply, looks_distress


def test_keeps_stamp_verb():
    assert sanitize_spoken("I'll stamp your Trick-or-Treat License.") == (
        "I'll stamp your Trick-or-Treat License."
    )


def test_keeps_let_me_stamp():
    assert sanitize_spoken("Here, let me stamp it.") == "Here, let me stamp it."


def test_keeps_stamp_applied_fallback():
    assert sanitize_spoken("Stamp applied. Candy awaits.") == "Stamp applied. Candy awaits."


def test_strip_trailing_stamp_word():
    assert "stamp" not in sanitize_spoken("Let me get that for you. stamp").lower()


def test_strip_leading_wave():
    out = sanitize_spoken("Wave. You're a natural-born civic leader.")
    assert not out.lower().startswith("wave")
    assert "natural" in out.lower()


def test_all_caps_calmed():
    out = sanitize_spoken("AGE IS JUST A NUMBER, BUT A LICENSE IS REQUIRED!")
    assert out != out.upper()
    assert "Age" in out or "age" in out.lower()


def test_parse_strips_stamp_dialogue():
    line, g = parse_reply("Here you go. stamp\nGESTURE: stamp")
    assert "stamp" not in line.lower().split()
    assert g == "stamp"


def test_skeleton_story_not_distress_keyword():
    assert not looks_distress("Last year a skeleton took my candy.")
    assert not looks_distress("Dreaming trick!")
    assert looks_distress("I'm scared")


def test_drops_shouty_lone_gesture():
    assert sanitize_spoken("STAMP") == ""
