# Gourdsworth

Mayor Gourdsworth of Pumpkinville. A family-safe Halloween greeter that listens and answers **entirely on your machine**.

Phase 1 is disembodied on purpose: laptop or Framework Desktop, built-in mic and speakers, no pumpkin. Use it to measure latency and watch how kids talk to a voice with no body.

Nothing is sent to a cloud API. Audio is kept in RAM and discarded after each turn.

## Quick start

```bash
git clone git@github.com:PixnBits/gourdsworth.git
cd gourdsworth
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
```

Ollama must already be running with a small instruct model:

```bash
ollama pull llama3.1:8b
# or whatever 4B–14B instruct model you already keep warm (e.g. qwen2.5:14b)
```

Set `ollama.model` in `config.yaml` to that name. If the configured name is not pulled, startup auto-selects the best already-pulled instruct model (prefers ~7–8B; 14B is accepted when that is the smallest).

```bash
python -m gourdsworth --list-devices   # pick mic/speaker ids
python -m gourdsworth --input N --output N
python -m gourdsworth                  # push-to-talk: Enter to listen, Enter to stop
python -m gourdsworth --mode vad
python -m gourdsworth --dry-run
python -m gourdsworth --stt-model tiny.en --mode vad --input 6 --output 8
```

`--dry-run` skips the mic and speaker playback so you can type kid-proxy lines and still see LLM (+ optional TTS synth) timing. Startup still preloads Whisper, warms Ollama (`keep_alive`), and loads Piper, printing load times.

Every spoken turn prints a metrics line:

```
record=…ms  stt=…ms  llm_ttft=…ms  llm=…ms  tts_first=…ms  tts=…ms  play=…ms  first_syllable≈…ms  turn=…ms
```

`first_syllable` is the number that matters for porch magic: silence → first Mayor audio. Target under 1000 ms. Under 1500 ms is usable. Above 2500 ms, shrink the Whisper model or the LLM.

## Privacy

- `save_audio` and `save_transcripts` are hard-refused at startup if set true
- Ollama is called at `127.0.0.1` only
- History is four turns of **text**, in process memory
- No WAV files in the project workflow (espeak / Piper-CLI fallback may use a NamedTemporaryFile that is unlinked immediately)

## Character

`prompts/mayor_system.txt` is the whole personality. `canned/lines.json` covers silence, crowds, distress, and model failure. Edit those before you edit code.

## Hardware later

Jaw = RMS of the outgoing samples. Body = six canned gestures the model *names*. Do not generate servo trajectories. See `ARCHITECTURE.md`.


## Latency results

Measured on Framework Desktop (AMD Ryzen AI Max / Strix Halo), pipewire I/O, Piper `en_US-lessac-medium`.

| Setup | first_syllable | notes |
|-------|----------------:|-------|
| M0 · qwen2.5:14b · base.en · wait-for-full-reply | ~1650 ms | live VAD |
| M1 · early TTS + tiny.en · qwen2.5:14b | **~903 ms** | dry-run `[early-tts]`; live TBD |

`first_syllable ≈ stt + time_to_first_audio` after the kid stops talking.
