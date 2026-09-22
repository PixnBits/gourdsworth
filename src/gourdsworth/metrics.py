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

    @property
    def first_syllable_ms(self) -> float:
        """Alias for the porch-facing first-audio metric (see render())."""
        return self.first_audio_from_silence_ms()

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


@dataclass
class SessionStats:
    """Running porch-session stats for spoken diagnostics (crate desktop)."""

    response_latencies_ms: list[float] = field(default_factory=list)

    def note_completed_turn(self, metrics: TurnMetrics) -> None:
        """Record a finished turn's first-syllable latency (skip zeros/failures)."""
        ms = float(metrics.first_syllable_ms or 0.0)
        if ms <= 0:
            return
        self.response_latencies_ms.append(ms)

    def average_response(self) -> tuple[float, int] | None:
        """Return (average_seconds, n_turns) or None if no samples yet."""
        samples = self.response_latencies_ms
        if not samples:
            return None
        avg_s = (sum(samples) / len(samples)) / 1000.0
        return avg_s, len(samples)


_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


def score_as_percent(score: float) -> int:
    """CLIP score 0..1 → nearest whole percent, clamped."""
    try:
        pct = int(round(float(score) * 100.0))
    except (TypeError, ValueError):
        pct = 0
    return max(0, min(100, pct))


def format_percent_spoken(score: float) -> str:
    """TTS-friendly percent: 0.20 → 'twenty percent' (not 'zero point two')."""
    pct = score_as_percent(score)
    if pct == 100:
        words = "one hundred"
    elif pct < 20:
        words = _ONES[pct]
    else:
        tens, ones = divmod(pct, 10)
        words = _TENS[tens] if ones == 0 else f"{_TENS[tens]} {_ONES[ones]}"
    return f"{words} percent"



def format_debug_spoken(
    *,
    vis=None,
    uplink_first_ms: float = 0.0,
    telemetry: dict | None = None,
    session_stats: SessionStats | None = None,
) -> str:
    """Build the spoken Agent P debug line (concise, porch-safe)."""
    bits = ["Agent P debug channel open."]
    if vis is not None and getattr(vis, "ok", False) and getattr(vis, "note", None):
        bits.append(
            f"Vision says {getattr(vis, 'label', None) or 'unknown'} "
            f"at {format_percent_spoken(float(getattr(vis, 'score', 0.0)))}."
        )
        top3 = getattr(vis, "top3", None) or ()
        if top3:
            ranked = sorted(
                ((str(n), float(s)) for n, s in list(top3)[:3]),
                key=lambda kv: kv[1],
                reverse=True,
            )
            tops = ", ".join(f"{n} {format_percent_spoken(s)}" for n, s in ranked)
            bits.append(f"Top guesses: {tops}.")
    elif vis is not None and getattr(vis, "skip_reason", None):
        bits.append(f"Vision skipped: {vis.skip_reason}.")
    else:
        bits.append("No still ready yet.")

    if uplink_first_ms:
        bits.append(f"Uplink first {uplink_first_ms:.0f} milliseconds.")

    temp_c = None
    if telemetry:
        raw = telemetry.get("cpu_temp_c")
        if raw is not None:
            try:
                temp_c = float(raw)
            except (TypeError, ValueError):
                temp_c = None
    if temp_c is not None:
        bits.append(f"Pi temperature {int(round(temp_c))} degrees.")

    avg = session_stats.average_response() if session_stats is not None else None
    if avg is not None:
        avg_s, n = avg
        bits.append(f"Average response {avg_s:.1f} seconds across {n} turns.")

    return " ".join(bits)

