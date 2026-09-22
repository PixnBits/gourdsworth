from gourdsworth.guardrails import looks_debug_pass


def test_perry():
    assert looks_debug_pass("Perry the Platypus")
    assert looks_debug_pass("hey agent p")
    assert looks_debug_pass("Doofenshmirtz")
    assert not looks_debug_pass("trick or treat")
    assert not looks_debug_pass("hello mayor")
