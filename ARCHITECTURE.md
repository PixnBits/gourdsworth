# Architecture

Privacy constraint: children's voices never leave the LAN and are never written to disk.

## Phase 1 (disembodied)

```
porch / laptop mic                Framework Desktop (this repo)
─────────────────                 ────────────────────────────
Talk / Enter / VAD  ──PCM RAM──►  energy VAD
USB conference mic                faster-whisper (base.en / small.en)
speakers                          Ollama 4B–8B instruct (streamed)
(later: jaw servo)  ◄──PCM RAM──  Piper (or espeak fallback)
                                  guardrails + canned deck
```

Phase 1: same process owns mic, models, and speakers. `python -m gourdsworth`.

## Phase 2 (crate)

Inference stays on the Framework Desktop. The Pi is I/O only — it does **not**
run Whisper, Ollama, Piper, or CLIP.

```
Pi 4 (crate)                          Framework Desktop (gourdsworth)
─────────────────                     ────────────────────────────────
USB mic ──PCM 16k mono s16le────────► net server → existing STT/LLM
USB cam ──JPEG on Talk/still────────► vision sidecar (optional, RAM)
speakers ◄──f32le / s16le chunks───── Piper TTS
button   ──JSON event lines─────────► Talk / listen
jaw/LED  ◄──GESTURE JSON───────────── parse_reply gesture tags
          (print now; servos later)
```

Default bind is `127.0.0.1:8746`. A LAN bind (`0.0.0.0` or a LAN IP) requires
`crate.allow_lan: true`. That opt-in means anyone on the network can read kids'
PCM/JPEG; there is no TLS and no auth. Never write audio, stills, or
transcripts to disk — RAM only, drop after the turn.

Framing lives in `src/gourdsworth/net.py` (JSON lines + length-prefixed
binary). Pi client: `clients/pi/crate_client.py`. Desktop:
`python -m gourdsworth --serve-crate` (models) or `--crate-echo` (protocol
smoke, no models). Local mic mode is unchanged when the crate listener is off.

## Turn

1. Immediate canned opener on session start so something happens before models think.
2. Listen 6–8 s or until Enter / silence. (Crate: Pi button down/up or Enter.)
3. Transcribe in RAM. Drop the ndarray.
4. If silence → canned shy line. If distress keywords → drop character, one safe line.
5. Else stream Ollama (`num_predict` 40). Time-to-first-token is logged.
6. Parse spoken line + `GESTURE:` tag. Cap 20 words. If dark, swap canned fallback.
7. Piper synthesizes to a float buffer. Play (local speakers, or crate `play` frames). Drop the buffer.
8. Keep at most four text turns in memory. No transcript file.

## Why not one speech-to-speech model

Native S2S is faster at barge-in. It is also how audio leaves the house. This project refuses that trade. A streamed cascade on a 128 GB Strix Halo box should land first Mayor syllable around 0.5–1.5 s after they stop talking. The metrics line after every turn tells you whether that is true on *this* machine.

## Vision sidecar (V0 / crate stills)

Opt-in, local, never on the voice path.

```
Talk / --snap ──► daemon thread: USB frame → JPEG bytes in RAM → CLIP vs closed labels
                         │
Crate Talk    ──► same thread, but JPEG arrived over LAN (desktop camera unused)
                         │
                         ├─ ready before LLM request: one Visual note line on this turn only
                         └─ late / missing camera / missing extras: omit the note; voice unchanged
```

- Camera (desktop or Pi) opens only for that still, then releases. No ring buffer, no file, no cloud vision API.
- Closed costume list (`costume_labels.txt`). Unsure → `homemade`. No names, ages, faces.
- Metrics (text only): `vision_ms`, `vision_label`, `vision_score`, `vision_used`.
- Voice does not await vision. Same privacy sign as issue #19.
- Later: presence / PIR, optional local VLM sentence.

## What is deliberately missing in 0.1 / Phase 2

- Servo / LED trajectories (jaw follows PCM RMS later; crate only prints `GESTURE`)
- PIR presence loop
- On-Pi inference (Hailo / Whisper / Ollama)
- Moonshine streaming STT (swap-in later; faster-whisper is the known-good AMD path)
- Kokoro TTS
- Multi-talker diarization
- Cloud failover
- Recording for review

Those belong in later milestones. See `GROK_BUILD_PROMPT.md`.
