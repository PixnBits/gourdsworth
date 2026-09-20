from gourdsworth.guardrails import finish_spoken, parse_reply


def test_strips_trailing_colon():
    assert finish_spoken("Here is your Trick-or-Treat License:") == (
        "Here is your Trick-or-Treat License."
    )


def test_keeps_real_bang():
    assert finish_spoken("Off you go!").endswith("!")


def test_parse_finishes_colon():
    line, _ = parse_reply(
        "You're a shining star. Here's your Trick-or-Treat License:\nGESTURE: stamp"
    )
    assert not line.endswith(":")
    assert line.endswith(".")
