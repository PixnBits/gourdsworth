from gourdsworth.stt import porch_correct


def test_check_our_tree():
    assert porch_correct("Check our tree.").lower().startswith("trick or treat")


def test_check_our_treat():
    assert "trick or treat" in porch_correct("Check our treat.").lower()


def test_leaves_long_alone():
    long = "Last year a skeleton took my candy on the big porch."
    assert porch_correct(long) == long


def test_peter_near_miss():
    out = porch_correct("Peter Peter pumpkin ear")
    assert "pumpkin eater" in out.lower()


def test_have_a_paper_negation():
    assert "don't" in porch_correct("Have a paper.").lower()
    assert "don't" in porch_correct("Have a paper").lower()


def test_keeps_explicit_dont_have_paper():
    assert "don't" in porch_correct("I don't have a paper.").lower()


def test_porch_correct_clipped_trick():
    from gourdsworth.stt import porch_correct

    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum() or ch.isspace()).split()

    assert norm(porch_correct("Trick.")) == ["trick", "or", "treat"]
    assert norm(porch_correct("trick")) == ["trick", "or", "treat"]
    assert norm(porch_correct("trick or")) == ["trick", "or", "treat"]
