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
# optional arcade button (BCM pin)
sudo apt install -y python3-gpiozero
```

From a clone of this repo on the Pi (no full gourdsworth package install):

```bash
python3 -m venv .venv-crate
source .venv-crate/bin/activate
pip install -r clients/pi/requirements.txt   # includes opencv-python-headless for stills
```

Apt `python3-opencv` alone is not enough — the venv will not see it unless you pass `--system-site-packages`. Prefer the pip wheel in the venv.

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

Prefer `clients/pi/local.env` (gitignored) or env `CRATE_HOST` / `CRATE_PORT` so LAN addresses never land in git. Copy from `local.env.example`.

On a real Pi on the porch LAN:

```bash
# desktop config.yaml — opt in, this is the dangerous bind
# crate:
#   host: 0.0.0.0
#   port: 8746
#   allow_lan: true    # anyone on this LAN can read PCM/JPEG; no TLS
PYTHONPATH=src python clients/pi/crate_client.py --button-pin 17   # host from local.env
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


## Audio device pick

```bash
PYTHONPATH=src python clients/pi/crate_client.py --list-devices
```

Put the ids in `local.env` as `CRATE_INPUT=` / `CRATE_OUTPUT=` (gitignored), or pass `--input N --output N`.


## Continuous porch mode — mic always on except while Mayor speaks; stills fire on speech

Hands-free after the opener — auto-listens again when the Mayor finishes:

```bash
PYTHONPATH=src python clients/pi/crate_client.py --continuous
```

Stills default to **full native camera resolution**. If encode+send is slow, the client
adaptively steps the long edge down (1920 → 1280 → 960 → 640) and may ease JPEG quality.
Cap with `--max-edge N` or `CRATE_MAX_EDGE` in `local.env` (0 = full native).


## USB DAC / odd sample rates

Piper often synthesizes at 22050 Hz. Many USB dongles reject that rate — and some
advertise 44100 but only accept 48000. On startup the client probes a working
playback rate (stderr muted) and resamples every TTS chunk to that rate.


## Porch run logs

Both sides tee to `logs/` (gitignored):

- Desktop `--serve-crate`: `logs/crate-desktop.log`
- Pi client: `logs/crate-pi.log` (override with `--log-file` / `CRATE_LOG`)

Pull them from the Framework Desktop / Pi without copy-paste.


## Porch thermal (outdoor / hot days)

Continuous mode takes **one still per utterance at speech-end** (not also at
speech-start) to cut camera open/USB churn. Prefer `CRATE_MAX_EDGE=1280` in
`local.env`. Camera stays on the Pi for now; the desktop already accepts crate
JPEGs, so a later desktop webcam path does not need a new protocol.

## Local shutdown lines

Pre-rendered WAVs in `assets/shutdown/` play when the desktop drops or the client exits (no Pi TTS).

If the desktop is not up yet (or goes away mid-porch), the client **keeps waiting** and
plays a random `disconnect_*.wav` on the first miss, then about every 45s, until
`--serve-crate` accepts the connection again. `goodbye_*.wav` is only for an explicit quit.
