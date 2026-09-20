# Gourdsworth

Mayor Gourdsworth of Pumpkinville. A family-safe Halloween greeter that listens and answers **entirely on your machine**.

Phase 1 is disembodied on purpose: laptop or Framework Desktop, built-in mic and speakers, no pumpkin. Use it to measure latency and watch how kids talk to a voice with no body.

Phase 2 is the porch **crate**: a Raspberry Pi streams mic PCM and optional JPEG stills to this same process over the LAN. Inference (STT / LLM / TTS / vision) stays on the desktop. The Pi does not run Whisper, Ollama, or CLIP.

Nothing is sent to a cloud API. Audio and stills are kept in RAM and discarded after each turn. They never leave the LAN.

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
python -m gourdsworth --continuous --input 6 --output 8   # hands-free porch loop
python -m gourdsworth --dry-run
python -m gourdsworth --stt-model tiny.en --mode vad --input 6 --output 8
python -m gourdsworth --snap                 # kitchen still (needs extras + webcam)
python -m gourdsworth --vision               # Talk grabs one RAM frame in parallel
python -m gourdsworth --serve-crate          # Phase 2: listen for one Pi crate client
python -m gourdsworth --crate-echo           # protocol smoke (no models)
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
- Crate TCP **defaults to `127.0.0.1`**. Binding `0.0.0.0` or a LAN IP requires `crate.allow_lan: true` in `config.yaml`. That opt-in means anyone on that network can read children's PCM and JPEG; there is no TLS and no auth. Do not port-forward this.

Porch sign copy (vision, when enabled):

*A camera looks only when someone is at the crate. No faces are saved. Nothing leaves this house. (Startup may download CLIP/YOLO *model weights* once into `~/.cache`; that is not porch audio or frames. Later runs stay offline for Hub.)*

## Vision (opt-in, V0 kitchen still)

Voice stays the product. A USB webcam is spice. If the lens is unplugged or CLIP is slow, Talk → STT → LLM → TTS behaves exactly as it does without `--vision`. `first_syllable_ms` does **not** wait on a frame.

Install extras only when you want the camera:

```bash
pip install -e '.[vision]'
# first CLIP load downloads ViT-B-32 (~350 MB, OpenAI weights via open_clip). Offline after that.
python -m gourdsworth --snap                 # grab one frame, print top 3 labels + visual_note, exit
python -m gourdsworth --snap --dry-run       # same; prints the note, does not call the mayor
python -m gourdsworth --vision               # on Talk: fire-and-forget still; inject note if ready in time
python -m gourdsworth --vision --camera 0 --dry-run
```

Or set `vision.enabled: true` in `config.yaml`. Labels live in `src/gourdsworth/costume_labels.txt` (closed list; unsure → `homemade`). No JPEG is written. No faces, names, or ages. Neighbors' kids are the threat model.

V0 kitchen stills are USB-on-the-Framework-Desktop. Phase 2 (`--serve-crate --vision`) classifies JPEG stills the Pi sends over the crate socket instead of opening the desktop camera. Presence / PIR and a local VLM sentence are later issues.

## Character

`prompts/mayor_system.txt` is the whole personality. `canned/lines.json` covers silence, crowds, distress, and model failure. Edit those before you edit code.

## Phase 2 — crate (Pi ↔ desktop)

The desktop runs the existing turn loop. When a crate client is connected, mic/speakers are remote PCM instead of local sounddevice.

**Desktop** (this repo, models already working from Phase 1):

```bash
python -m gourdsworth --serve-crate
python -m gourdsworth --serve-crate --vision     # CLIP on Pi JPEGs; voice does not wait
```

Bind is `crate.host` / `crate.port` in `config.yaml` (default `127.0.0.1:8746`). To listen on the porch LAN:

```yaml
crate:
  host: 0.0.0.0
  port: 8746
  allow_lan: true   # required; PCM/JPEG then visible to the whole LAN, no TLS
```

**Protocol smoke** (no Whisper / Ollama / Piper; fake client or the Pi script):

```bash
python -m gourdsworth --crate-echo
# other terminal, same machine:
PYTHONPATH=src python clients/pi/crate_client.py --host 127.0.0.1 --no-camera --no-mic
# Enter = Talk (sends silence). You should see GESTURE: stamp and a play round-trip.
```

Unit tests cover framing with an in-process loopback client (`pytest tests/test_net_framing.py`) — no hardware.

**Pi** (I/O only). See `clients/pi/README.md` for apt/pip deps. Do not install the full `gourdsworth` extras on the Pi.

```bash
PYTHONPATH=src python clients/pi/crate_client.py --host 192.168.x.desktop
PYTHONPATH=src python clients/pi/crate_client.py --host 192.168.x.desktop --no-camera
PYTHONPATH=src python clients/pi/crate_client.py --host 192.168.x.desktop --button-pin 17
```

Uplink is 16 kHz mono signed 16-bit little-endian, 100 ms chunks (3200 bytes). Downlink is Piper float32 little-endian at the voice's native rate. JPEG is one length-prefixed still per Talk. Control is JSON lines: `{"event":"button","state":"down"}`, `{"event":"gesture","name":"stamp"}`.

## Hardware later

Jaw = RMS of the outgoing samples. Body = six canned gestures the model *names* (`GESTURE` JSON to the Pi; print now, servos later). Do not generate servo trajectories. See `ARCHITECTURE.md`.


## Mayor voice

Default Piper voice is `en_US-danny-low` (fun, not intimidating). Switch back with `tts.voice: en_US-lessac-medium` in `config.yaml`.

## Latency results

Measured on Framework Desktop (AMD Ryzen AI Max / Strix Halo), pipewire I/O, Piper `en_US-lessac-medium`.

| Setup | first_syllable | notes |
|-------|----------------:|-------|
| M0 · qwen2.5:14b · base.en · wait-for-full-reply | ~1650 ms | live VAD |
| M1 · early TTS + tiny.en · qwen2.5:14b | **~903 ms** | dry-run `[early-tts]` |
| M1 · early TTS + tiny.en · llama3.1:8b | **~655–821 ms** | live VAD, pipewire I/O |

`first_syllable ≈ stt + time_to_first_audio` after the kid stops talking.


## STT accuracy

Default is `base.en` (clearer on a noisy porch). `tiny.en` is faster but mangled live lines like "trick or treat" → "check our tree".

Porch bias: Whisper gets an `initial_prompt` / hotwords for candy/costume/pumpkin phrases, plus a light corrector for short near-misses. See issue #4.
