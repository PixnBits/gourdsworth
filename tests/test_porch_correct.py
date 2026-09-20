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
