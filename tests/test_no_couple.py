from gourdsworth.guardrails import parse_reply, scrub_relationship_words


def test_scrub_clever_couple():
    out = scrub_relationship_words(
        "A clever couple, I'm sure. Your handiwork is impressive."
    )
    assert "couple" not in out.lower()
    assert "citizen" in out.lower()


def test_parse_strips_couple():
    line, _ = parse_reply(
        "A clever couple, I'm sure. Help yourselves to the candy bowl.\nGESTURE: stamp"
    )
    assert "couple" not in line.lower()
    assert "candy" in line.lower() or "citizen" in line.lower()
