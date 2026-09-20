# Pi crate client

The Raspberry Pi is **I/O only**: USB mic, speakers, one webcam still, Talk
button. Whisper, Ollama, Piper, and CLIP stay on the Framework Desktop.
Do **not** `pip install -e .` the full gourdsworth package on the Pi (that
pulls faster-whisper / piper).

Children's PCM and JPEG stay on the LAN, in RAM, and are dropped after the
turn. Nothing is written to disk. There is no TLS — default the desktop to
`127.0.0.1` and only bind the LAN on a trusted porch network.

## Protocol (summary)

JSON lines + length-prefixed binary (`n` then raw bytes). See
`src/gourdsworth/net.py` for the full framing.

| Direction | What |
|-----------|------|
| Pi → desktop | PCM 16 kHz mono **s16le**, 100 ms chunks (1600 samples / 3200 bytes) |
| Pi → desktop | JPEG still, one frame on Talk |
| Pi → desktop | `{"event":"button","state":"down"\|"up"}` |
| desktop → Pi | TTS **f32le** (`{"event":"play","rate":…,"format":"f32le"}` + samples) |
| desktop → Pi | `{"event":"gesture","name":"stamp"\|"wave"\|…}` (print now; jaw/LED later) |

## Raspberry Pi OS deps

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv python3-numpy libportaudio2
# optional stills
sudo apt install -y python3-opencv
# optional arcade button (BCM pin)
sudo apt install -y python3-gpiozero
```

From a clone of this repo on the Pi (no package install):

```bash
python3 -m venv .venv-crate
source .venv-crate/bin/activate
pip install numpy sounddevice
# optional, if apt opencv is missing:
# pip install opencv-python-headless
```

`clients/pi/requirements.txt` is the pip set.

## Run

**Desktop** (models; default localhost bind):

```bash
python -m gourdsworth --serve-crate
python -m gourdsworth --serve-crate --vision    # classify Pi JPEGs; desktop camera unused
```

Protocol-only smoke (no Whisper/Ollama/Piper):

```bash
python -m gourdsworth --crate-echo
```

**Pi** (or the same laptop for loopback):

```bash
PYTHONPATH=src python clients/pi/crate_client.py --host 127.0.0.1 --no-camera
# dry: no mic, no camera, Enter sends a short silence
PYTHONPATH=src python clients/pi/crate_client.py --host 127.0.0.1 --no-camera --no-mic
```

On a real Pi on the porch LAN:

```bash
# desktop config.yaml — opt in, this is the dangerous bind
# crate:
#   host: 0.0.0.0
#   port: 8746
#   allow_lan: true    # anyone on this LAN can read PCM/JPEG; no TLS
PYTHONPATH=src python clients/pi/crate_client.py --host 192.168.x.desktop --button-pin 17
```

Enter starts Talk; Enter again stops (or release the GPIO button). Received
`GESTURE:` names are printed — servo / LED wiring is later.

## Degraded mode

| Missing | What happens |
|---------|----------------|
| No camera / `--no-camera` | Voice only |
| No mic / `--no-mic` / no sounddevice | Sends ~0.8 s of silence |
| No GPIO / no `--button-pin` | Enter on the keyboard |
| No speakers / no sounddevice | Prints that TTS bytes were dropped |

Jaw RMS, PIR presence, and on-Pi inference are out of scope.
