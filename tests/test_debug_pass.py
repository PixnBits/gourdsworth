from gourdsworth.guardrails import looks_debug_pass


def test_young_mayor_gag_unlocks():
    assert looks_debug_pass("Aren't you a little young to be a mayor?")
    assert looks_debug_pass("aren't you a little young to be mayor")
    assert looks_debug_pass("Say, aren't you a little young to be a mayor?")
    assert looks_debug_pass("Say unto you a little young to be a mayor.")  # STT


def test_bit_young_and_are_you_variants():
    assert looks_debug_pass("Aren't you a bit young to be a mayor?")
    assert looks_debug_pass("Are you a little young to be a mayor?")


def test_names_and_tri_state_do_not_unlock():
    assert not looks_debug_pass("Perry the Platypus")
    assert not looks_debug_pass("Isabella")
    assert not looks_debug_pass("tri state area")
    assert not looks_debug_pass("The Tri-State Area!")


def test_not_a_substring_or_normal_chatter():
    assert not looks_debug_pass(
        "Trick or treat. Aren't you a little young to be a mayor?"
    )
    assert not looks_debug_pass("How old are you mister")
    assert not looks_debug_pass("hello mayor")
    assert not looks_debug_pass("1912")
