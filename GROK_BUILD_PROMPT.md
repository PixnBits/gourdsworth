# Grok Build prompt — paste this

Use the block below as the entire first prompt to Grok Build, pointed at `PixnBits/gourdsworth` (private). The repo already has a working Phase-1 loop. Build should improve it, not start over.

---

```
You are working in the existing private repo PixnBits/gourdsworth.

GOAL
Ship a family-safe, fully local Halloween voice greeter called Mayor Gourdsworth of Pumpkinville. Children's audio must never leave the machine and must never be written to disk. Phase 1 (already scaffolded) is a disembodied desktop loop so we can measure latency and watch how kids talk to a voice with no body. Phase 2 is a porch crate talking to this same process over the LAN.

DO NOT
- Call any cloud STT, LLM, or TTS (no OpenAI, Gemini Live, ElevenLabs, Cartesia, Deepgram).
- Write WAV/MP3/transcript files except an immediately-unlinked tempfile for the espeak fallback.
- Generate servo trajectories. Jaw follows PCM RMS. Body plays six canned tags: stamp, wave, think, laugh, bow, listen.
- Invent a new character. The mayor lives in prompts/mayor_system.txt and canned/lines.json.
- Add telemetry, analytics, or bind Ollama to 0.0.0.0.
- Use a 70B+ model as the default. Default is a 4B–8B instruct model via local Ollama.

CURRENT TREE (respect it)
- src/gourdsworth/   Python package: app, audio_io, stt, llm, tts, guardrails, metrics, config
- prompts/mayor_system.txt
- canned/lines.json
- config.example.yaml
- ARCHITECTURE.md, README.md, tests/test_guardrails.py

HARD REQUIREMENTS
1. `python -m gourdsworth` runs a push-to-talk loop on Linux (Framework Desktop, Ubuntu, AMD Ryzen AI Max / Strix Halo, 128 GB unified memory, Ollama already installed).
2. After every turn print:
   record_ms stt_ms llm_ttft_ms llm_total_ms tts_first_ms tts_total_ms play_ms first_syllable_ms turn_ms
3. first_syllable_ms = stt + llm_ttft + tts_first. Target <1000 ms, acceptable <1500 ms.
4. Startup refuses to run if config privacy.save_audio or save_transcripts is true.
5. Guardrails: distress keyword → canned adult-help line and stop. Dark model output → canned fallback. 20 word cap.
6. Tests for guardrails stay green. Add tests if you change parsing.

MILESTONES — do them in order, commit after each

M0 — make Phase 1 actually runnable tonight
- Fix Piper Python API differences across piper-tts versions. If PiperVoice.synthesize is missing, fall back to piper CLI writing to stdout / a NamedTemporaryFile that is unlinked.
- Auto-detect Ollama models if config model is missing; prefer already-pulled 7B/8B instruct names.
- Preload Whisper + Ollama (one keep_alive warm ping) + Piper at startup and print load times.
- `--list-devices` to print sounddevice input/output ids. `--input N --output N` to select them.
- Keep --dry-run working (type a kid line, get spoken-or-printed mayor line + metrics).

M1 — latency work that matters
- Stream Ollama tokens; start Piper on the first complete sentence or at 12 words, whichever first. Do not wait for the full reply before TTS.
- Beam size 1, vad_filter on, English only for STT.
- Optional STT model flag: tiny.en / base.en / small.en / turbo.
- Warm the LLM with a one-token discarded ping so the first real turn is not a cold start.
- Document measured numbers in README from a real run if you can run models; if not, leave a blank results table.

M2 — kid-test UX
- Spacebar or Enter PTT that works in a real terminal (no broken select-on-stdin if it flakes).
- Loud console states: LISTENING / THINKING / SPEAKING so a parent across the room can see it.
- Session summary on quit: turns, mean first_syllable_ms, canned-fallback count. Text only, in memory, printed — not saved.
- Volume cap option (float 0–1).
- Crowd heuristic: if transcript looks like two greetings smashed together, use canned crowd line.

M3 — Phase 2 stubs only (do not block M0)
- gourdsworth/net.py sketch: TCP or UDP PCM frames from a crate client. Bind 127.0.0.1 by default. Document how a Pi would stream 16 kHz mono s16le.
- GPIO interface protocol as a JSON line: {"event":"button","state":"down"} / {"event":"gesture","name":"stamp"}. No hardware required to run tests.

ACCEPTANCE
- README tells a developer who already has Ollama how to hear the mayor in under 15 minutes.
- ARCHITECTURE.md matches the code.
- No secrets, no API keys, no cloud endpoints.
- Code is boring, typed enough, and short. Prefer one obvious path over plugin frameworks (no Pipecat/LiveKit unless a later prompt asks).

When you start: read the existing files, then implement M0. Do not rewrite the character. Commit in small pieces.
```
