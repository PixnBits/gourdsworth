from gourdsworth.guardrails import looks_debug_pass


def test_exact_debug_phrases_unlock():
    assert looks_debug_pass("tri state area")
    assert looks_debug_pass("price state area")
    assert looks_debug_pass("try state area")
    assert looks_debug_pass("tri stick area")


def test_passphrase_allows_punctuation_case_and_optional_the():
    assert looks_debug_pass("The Tri-State Area!")
    assert looks_debug_pass("tri... state, area?")
    assert looks_debug_pass("the price-state area")


def test_character_names_do_not_unlock():
    assert not looks_debug_pass("Perry")
    assert not looks_debug_pass("Perry the Platypus")
    assert not looks_debug_pass("Isabella")
    assert not looks_debug_pass("Doofenshmirtz")


def test_passphrase_is_not_a_substring_match():
    assert not looks_debug_pass("Welcome to the tri-state area")
    assert not looks_debug_pass("My costume is from the tri-state area")
    assert not looks_debug_pass("The tri-state area has great candy")
    assert not looks_debug_pass("trick or treat")
    assert not looks_debug_pass("hello mayor")
