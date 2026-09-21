from gourdsworth.metrics import TurnMetrics


def test_first_syllable_includes_post_speech_silence():
    m = TurnMetrics()
    m.stt_ms = 300
    m.to_first_audio_ms = 500
    m.post_speech_silence_ms = 850
    assert abs(m.first_audio_from_silence_ms() - 1650) < 0.01


def test_first_syllable_without_tail_unchanged():
    m = TurnMetrics()
    m.stt_ms = 300
    m.llm_ttft_ms = 400
    m.tts_first_ms = 80
    assert abs(m.first_audio_from_silence_ms() - 780) < 0.01
