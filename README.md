# Gourdsworth

![Mayor Gourdsworth on the porch](docs/images/mayor-porch.jpg)

Mayor Gourdsworth of Pumpkinville greets trick-or-treaters on the porch. Halloween 2026 was its first live night.

The Raspberry Pi 4 handles only the mic, the speaker, the camera, the Talk button, and the face. The face is a UART link to an ESP32 that drives the LEDs and the servos. All inference (STT, LLM, TTS, vision) runs locally on the Framework Desktop. No cloud. Audio and stills stay in RAM, and nothing leaves the house.

## Halloween 2026 live night

The desktop server log for the night, after the last server start, records 132 turns. 131 used the streaming early-TTS path. 1 was a canned line. The table is all 132 turns.

`first_syllable` is the silence after the kid stops talking until the first Mayor audio.

| | median | p90 |
| --- | ---: | ---: |
| first_syllable | 1634 ms | 2619 ms |
| stt | 214 ms | 317 ms |
| llm_ttft | 250 ms | 294 ms |
| tts_first | 182 ms | 269 ms |

Models on that start: faster-whisper `base.en`, Ollama `llama3.1:8b`, Piper `en_US-danny-low`, CLIP ViT-B-32 costume hints, and an optional YOLOv8n person count.

## Turn path

```mermaid
flowchart LR
  mic["USB mic"] --> pi["crate_client.py"]
  cam["Webcam stills"] --> pi
  btn["gpiozero Talk button"] --> pi
  pi --> spk["Speaker via PipeWire pw-play / Bluetooth JBL"]
  pi -->|"viseme from playback RMS, gesture forward"| uart["face_link UART /dev/serial0"]
  uart --> esp["ESP32"]
  esp --> lvl["74AHCT125"]
  lvl --> leds["WS2812B mouth and eyes"]
  esp --> pca["PCA9685"]
  pca --> servos["Servos"]
  pi <-->|"TCP 8746 JSON lines, 16 kHz PCM uplink, JPEG stills, Piper float32 downlink, gesture events"| net["net.py listener, trusted_peers allowlist"]
  net --> stt["stt.py faster-whisper"]
  stt --> guard["guardrails and canned/lines.json"]
  guard --> llm["llm.py Ollama at 127.0.0.1:11434, streaming"]
  guard --> tts["tts.py Piper"]
  llm --> early["early_speakable flush"]
  early --> tts
  tts --> net
  net --> clip["vision.py CLIP on Pi JPEGs"]
  clip -->|"visual note, does not block voice"| llm
```

The Pi client is `clients/pi/crate_client.py`. The desktop command is `python -m gourdsworth --serve-crate --vision`. CLIP may attach a visual note when the LLM starts. Voice keeps going while CLIP runs. See [ARCHITECTURE.md](ARCHITECTURE.md) and [firmware/esp32/README.md](firmware/esp32/README.md).

## Hardware (BOM)

Summary from [hardware/BOM.md](hardware/BOM.md) and the other hardware docs. Full parts list and the wiring diagram: [hardware/BOM.md](hardware/BOM.md), [hardware/wiring.svg](hardware/wiring.svg), [hardware/README.md](hardware/README.md).

LED 5 V, servo 5 V, and the Pi USB-C supply stay on separate rails. Grounds meet at one star point.

| Part | Role |
| --- | --- |
| Framework Desktop (AMD Ryzen AI Max / Strix Halo) | Inference host |
| Raspberry Pi 4 | Mic, speaker, camera, Talk button, face UART |
| ESP32 DevKit V1, WROOM-32, 30-pin | UART from the Pi; drives LEDs and servos |
| 74AHCT125 | Level shifter, LED data |
| PCA9685 16-channel | Servo pulses |
| 4× micro servos (SG90 / MG90S) | Brow L, brow R, hat, mustache |
| BTF-LIGHTING WS2812B 8×32 panel | Mouth |
| BTF-LIGHTING WS2812B 60 LED/m strip | Eyes, 2×16 px |
| Mean Well LRS-150-5 | LED supply. BOM also lists LRS-150F-5 |
| 5 V, 8–10 A brick | Servo supply |
| Raspberry Pi 15 W USB-C supply | Pi only |
| JBL Bluetooth speaker | TODO: confirm model |
| USB webcam | Code mentions BRIO. TODO: confirm |
| USB mic | Code mentions a BRIO mic and a CM108. TODO: confirm |
| Talk button | gpiozero, BCM pin set by `--button-pin`. TODO: confirm part and pin |
| Anker Solix power station | TODO: confirm model |
| Pumpkin shell / enclosure | TODO: confirm |

## Setup and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e '.[vision]'
cp config.example.yaml config.yaml
ollama pull llama3.1:8b
```

Ollama must already be running on the desktop. Set `ollama.model` in `config.yaml` to the pulled name. If that name is not pulled, startup auto-selects the best already-pulled instruct model (prefers about 7–8B; 14B is accepted when that is the smallest). The example voice is Piper `en_US-danny-low`.

Desktop crate block. `192.0.2.55` is a documentation address for the Pi. Use the Pi's address.

```yaml
crate:
  host: 0.0.0.0
  port: 8746
  allow_lan: true
  trusted_peers: [192.0.2.55]
```

`trusted_peers` is an allowlist of IP addresses. Other addresses are closed at accept. Put only the Pi on the list. A second trusted address can replace the Pi and hold it in the 30 second backoff. Entries split on commas or whitespace. A CIDR, a hostname, or `*` exits 2, and the error names the bad entry. An IPv4-mapped address (`::ffff:a.b.c.d`) compares as IPv4. A non-loopback bind with an empty trusted list exits with code 2. A loopback bind with an empty list still starts. `CRATE_TRUSTED_PEERS` is read once at startup and merged with `crate.trusted_peers`.

Pin the Pi in the user unit:

```bash
systemctl --user edit gourdsworth-server
```

```
[Service]
Environment=CRATE_TRUSTED_PEERS=192.0.2.55
```

Run the desktop:

```bash
python -m gourdsworth --serve-crate --vision
```

On the Pi, see [clients/pi/README.md](clients/pi/README.md). `192.0.2.10` is a documentation address for the desktop.

```bash
PYTHONPATH=src python clients/pi/crate_client.py --host 192.0.2.10 --continuous
```

The user unit [clients/pi/systemd/gourdsworth-porch.service](clients/pi/systemd/gourdsworth-porch.service) runs `clients/pi/crate_client.py --continuous` and reads `clients/pi/local.env`. Copy that file from [clients/pi/local.env.example](clients/pi/local.env.example).

Protocol smoke, same machine, no Whisper, Ollama, or Piper:

```bash
python -m gourdsworth --crate-echo
PYTHONPATH=src python clients/pi/crate_client.py --host 127.0.0.1 --no-camera --no-mic
```

Enter is Talk and sends silence. The client prints `GESTURE: tip` and plays a round trip.

`--dry-run` skips the mic and speaker playback. Type a kid line and the process still reports LLM and TTS timing. Startup still preloads Whisper, warms Ollama, and loads Piper.

```bash
python -m gourdsworth --list-devices
python -m gourdsworth --dry-run
```

Every spoken turn prints a metrics line:

```
record=…ms  stt=…ms  llm_ttft=…ms  llm=…ms  tts_first=…ms  tts=…ms  play=…ms  first_syllable≈…ms  turn=…ms
```

A crate turn can also print `uplink_first`, `uplink_jitter`, `[early-tts]` or `[canned]`, and vision fields. `first_syllable` is the porch wait defined above.

### Privacy

Startup exits with code 2 if `save_audio` or `save_transcripts` is true. Ollama stays on `127.0.0.1:11434`. History is four turns of text, in process memory. Crate TCP has no TLS and no auth. With `allow_lan: true`, anyone on that network can read the PCM and the JPEGs. Keep the port on the porch LAN.

### Character

[prompts/mayor_system.txt](prompts/mayor_system.txt) is the personality. [canned/lines.json](canned/lines.json) covers silence, crowds, distress, and model failure.

### Logs

Both sides write under `logs/`, which is gitignored.

- Desktop `--serve-crate`: `logs/crate-desktop.log`
- Pi client: `logs/crate-pi.log` (override with `--log-file` or `CRATE_LOG`)

### Tests

```bash
python -m pytest -q
```

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [hardware/README.md](hardware/README.md)
- [firmware/esp32/README.md](firmware/esp32/README.md)
- [clients/pi/README.md](clients/pi/README.md)
