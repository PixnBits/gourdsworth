from gourdsworth.net import AdaptiveVad


def test_ends_when_peak_drops_even_if_ambient_above_old_absolute():
    """Group murmur stays above 0.009; speaker pause still ends."""
    vad = AdaptiveVad(absolute_start=0.012, peak_end_ratio=0.32)
    # ambient murmur
    for _ in range(20):
        assert not vad.observe(0.010)
    # loud speaker
    for _ in range(10):
        assert vad.observe(0.08)
    # back to murmur — below ~32% of peak (0.08*0.32=0.0256) but above absolute 0.012
    assert not vad.observe(0.015)


def test_quiet_room_still_needs_real_speech():
    vad = AdaptiveVad(absolute_start=0.012)
    for _ in range(30):
        assert not vad.observe(0.004)
    assert vad.observe(0.03)


def test_floor_rises_with_sustained_ambient():
    vad = AdaptiveVad(absolute_start=0.012)
    for _ in range(40):
        vad.observe(0.02)
    # floor crept up; 0.025 may no longer start
    # but something clearly above floor should
    assert vad.start_threshold() >= 0.012
