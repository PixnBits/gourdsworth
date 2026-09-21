from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter


@dataclass
class TurnMetrics:
    t0: float = field(default_factory=perf_counter)
    record_ms: float = 0.0
    uplink_first_ms: float = 0.0  # button-down → first PCM chunk from Pi
    uplink_jitter_ms: float = 0.0  # max inter-chunk gap while listening
    stt_ms: float = 0.0
    llm_ttft_ms: float = 0.0
    llm_total_ms: float = 0.0
    tts_first_ms: float = 0.0
    tts_total_ms: float = 0.0
    play_ms: float = 0.0
    # Wall time from end of STT to first audio sample leaving TTS (early flush aware)
    to_first_audio_ms: float = 0.0
    words_in: int = 0
    words_out: int = 0
    used_canned: bool = False
    early_flush: bool = False
    vision_ms: float = 0.0
    vision_label: str = ""
    vision_score: float = 0.0
    vision_used: bool = False
    person_count: int | None = None
    # VAD tail after last voiced frame (porch-perceived wait includes this)
    post_speech_silence_ms: float = 0.0
    had_voice: bool = False

    def end_to_end_ms(self) -> float:
        return (perf_counter() - self.t0) * 1000

    def first_audio_from_silence_ms(self) -> float:
        """Approx porch wait from end of user speech → first Mayor audio.

        Includes the VAD silence tail (often ~0.85s) that happens after the kid
        stops talking but before we close the listen window — without it the
        number looks ~1s while the porch feels ~2s.
        """
        if self.to_first_audio_ms > 0:
            base = self.stt_ms + self.to_first_audio_ms
        else:
            base = self.stt_ms + self.llm_ttft_ms + self.tts_first_ms
        return base + self.post_speech_silence_ms

    def render(self) -> str:
        return (
            f"record={self.record_ms:.0f}ms  "
            + (f"uplink_first={self.uplink_first_ms:.0f}ms  " if self.uplink_first_ms else "")
            + (f"uplink_jitter={self.uplink_jitter_ms:.0f}ms  " if self.uplink_jitter_ms else "")
            + f"stt={self.stt_ms:.0f}ms  "
            f"llm_ttft={self.llm_ttft_ms:.0f}ms  "
            f"llm={self.llm_total_ms:.0f}ms  "
            f"tts_first={self.tts_first_ms:.0f}ms  "
            f"tts={self.tts_total_ms:.0f}ms  "
            f"play={self.play_ms:.0f}ms  "
            f"first_syllable≈{self.first_audio_from_silence_ms():.0f}ms  "
            f"turn={self.end_to_end_ms():.0f}ms"
            + ("  [early-tts]" if self.early_flush else "")
            + ("  [canned]" if self.used_canned else "")
            + (
                (
                    f"  vision={self.vision_ms:.0f}ms"
                    f"  vision_label={self.vision_label or '-'}"
                    f"  vision_score={self.vision_score:.2f}"
                    f"  vision_used={int(self.vision_used)}" + (f"  persons={self.person_count}" if self.person_count is not None else "")
                )
                if (self.vision_ms or self.vision_label or self.vision_used)
                else ""
            )
        )
