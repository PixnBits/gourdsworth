from gourdsworth.tts import split_bang_ending


def test_split_comma_tag():
    raw = "A trick-or-treater who\'s on top of their game, I see!"
    assert split_bang_ending(raw) == (
        "A trick-or-treater who\'s on top of their game. I see!"
    )


def test_split_emdash_tag():
    raw = "What fine costumes — splendid!"
    assert split_bang_ending(raw) == "What fine costumes. Splendid!"


def test_no_split_without_bang():
    raw = "Candy is that way."
    assert split_bang_ending(raw) == raw


def test_no_split_long_tail():
    raw = (
        "Welcome friends, you are all hereby invited to celebrate "
        "with many treats tonight!"
    )
    assert split_bang_ending(raw) == raw


def test_no_split_short_whole():
    assert split_bang_ending("Stamp!") == "Stamp!"
