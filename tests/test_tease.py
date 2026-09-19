from gourdsworth.guardrails import looks_tease, looks_distress


def test_tease_poo_poo():
    assert looks_tease("you\'re a poo-poo head")
    assert looks_tease("you are a dummy")
    assert looks_tease("smell my feet")
    assert looks_tease("give me all your candy")
    assert looks_tease("I\'m taking all of it")


def test_tease_not_normal_trick():
    assert not looks_tease("trick or treat")
    assert not looks_tease("hello mayor")
    assert not looks_tease("nice costume")


def test_tease_not_distress():
    assert looks_distress("I\'m scared")
    assert not looks_tease("I\'m scared")
