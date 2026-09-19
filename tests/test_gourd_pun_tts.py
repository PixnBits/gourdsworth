from gourdsworth.tts import rewrite_puns_for_tts


def test_hyphen_form():
    assert "gourd----geous" in rewrite_puns_for_tts(
        "a bit of a gourd-geous burden"
    )


def test_space_form():
    assert "gourd----geous" in rewrite_puns_for_tts("a gourd geous burden")


def test_preserves_capital():
    out = rewrite_puns_for_tts("Gourd-geous in costume")
    assert out.startswith("Gourd----geous")


def test_leaves_other_words():
    assert rewrite_puns_for_tts("gorgeous sunset") == "gorgeous sunset"
