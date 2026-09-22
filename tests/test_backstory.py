from gourdsworth.guardrails import match_backstory


def test_favorite_color():
    assert match_backstory("What is your favorite color?") == "favorite_color"
    assert match_backstory("what's your favourite colour") == "favorite_color"


def test_age_and_job():
    assert match_backstory("How old are you?") == "age"
    assert match_backstory("How did you become mayor?") == "job"


def test_not_costume():
    assert match_backstory("I am a witch") is None
    assert match_backstory("trick or treat") is None
