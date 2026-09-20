# Architecture

Privacy constraint: children's voices never leave the LAN and are never written to disk.

```
porch / laptop mic                Framework Desktop (this repo)
─────────────────                 ────────────────────────────
Talk / Enter / VAD  ──PCM RAM──►  energy VAD
USB conference mic                faster-whisper (base.en / small.en)
speakers                          Ollama 4B–8B instruct (streamed)
(later: jaw servo)  ◄──PCM RAM──  Piper (or espeak fallback)
                                  guardrails + canned deck
```

Phase 1 (now): disembodied. Same process owns mic, models, and speakers.
Phase 2: crate Pi streams PCM to this process over the LAN. Inference stays here.

## Turn

1. Immediate canned opener on session start so something happens before models think.
2. Listen 6–8 s or until Enter / silence.
3. Transcribe in RAM. Drop the ndarray.
4. If silence → canned shy line. If distress keywords → drop character, one safe line.
5. Else stream Ollama (`num_predict` 40). Time-to-first-token is logged.
6. Parse spoken line + `GESTURE:` tag. Cap 20 words. If dark, swap canned fallback.
7. Piper synthesizes to a float buffer. Play. Drop the buffer.
8. Keep at most four text turns in memory. No transcript file.

## Why not one speech-to-speech model

Native S2S is faster at barge-in. It is also how audio leaves the house. This project refuses that trade. A streamed cascade on a 128 GB Strix Halo box should land first Mayor syllable around 0.5–1.5 s after they stop talking. The metrics line after every turn tells you whether that is true on *this* machine.

## Vision sidecar (V0)

Opt-in, local, never on the voice path.

```
Talk / --snap ──► daemon thread: USB frame → JPEG bytes in RAM → CLIP vs closed labels
                         │
                         ├─ ready before LLM request: one Visual note line on this turn only
                         └─ late / missing camera / missing extras: omit the note; voice unchanged
```

- Camera opens only for that still, then releases. No ring buffer, no file, no cloud vision API.
- Closed costume list (`costume_labels.txt`). Unsure → `homemade`. No names, ages, faces.
- Metrics (text only): `vision_ms`, `vision_label`, `vision_score`, `vision_used`.
- V1+ (not here): presence / person count, Pi JPEG over LAN, optional local VLM sentence.

## What is deliberately missing in 0.1

- Servo / LED / arcade button GPIO
- Moonshine streaming STT (swap-in later; faster-whisper is the known-good AMD path)
- Kokoro TTS
- Multi-talker diarization
- Cloud failover
- Presence, YOLO person count, Pi frame-grabber, local VLM (vision V1–V3)

Those belong in later milestones. See `GROK_BUILD_PROMPT.md`.
