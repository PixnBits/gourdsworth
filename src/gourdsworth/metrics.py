from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter


@dataclass
class TurnMetrics:
    t0: float = field(default_factory=perf_counter)
    record_ms: float = 0.0
    stt_ms: float = 0.0
    llm_ttft_ms: float = 0.0
    llm_total_ms: float = 0.0
    tts_first_ms: float = 0.0
    tts_total_ms: float = 0.0
    play_ms: float = 0.0
    words_in: int = 0
    words_out: int = 0
    used_canned: bool = False

    def end_to_end_ms(self) -> float:
        return (perf_counter() - self.t0) * 1000

    def first_audio_from_silence_ms(self) -> float:
        return self.stt_ms + self.llm_ttft_ms + self.tts_first_ms

    def render(self) -> str:
        return (
            f"record={self.record_ms:.0f}ms  "
            f"stt={self.stt_ms:.0f}ms  "
            f"llm_ttft={self.llm_ttft_ms:.0f}ms  "
            f"llm={self.llm_total_ms:.0f}ms  "
            f"tts_first={self.tts_first_ms:.0f}ms  "
            f"tts={self.tts_total_ms:.0f}ms  "
            f"play={self.play_ms:.0f}ms  "
            f"first_syllable≈{self.first_audio_from_silence_ms():.0f}ms  "
            f"turn={self.end_to_end_ms():.0f}ms"
            + ("  [canned]" if self.used_canned else "")
        )
