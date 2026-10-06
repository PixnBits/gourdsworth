"""Pi → ESP32 face packets. No serial port, no LEDs, no servos."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "clients" / "pi"))
sys.path.insert(0, str(_REPO / "src"))

import crate_client  # noqa: E402
import face_link  # noqa: E402
from gourdsworth.guardrails import parse_reply, sanitize_spoken  # noqa: E402
from gourdsworth.net import ALLOWED_GESTURES, DEFAULT_GESTURE  # noqa: E402


ALLOWED = ("tip", "beam", "reckon", "chuckle", "attend", "twirl")
RETIRED = ("stamp", "wave", "think", "laugh", "bow", "listen")


class FakePort:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.buf += bytes(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


def _objs(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_packet_encode_shapes():
    assert face_link.encode_line({"op": "ping"}) == b'{"op":"ping"}\n'
    assert face_link.encode_line({"op": "idle"}) == b'{"op":"idle"}\n'
    viseme = face_link.viseme_obj(np.full(32, 0.5, dtype=np.float32))
    assert viseme["op"] == "viseme"
    assert viseme["id"] == "aa"
    assert viseme["rms"] == pytest.approx(0.5)
    assert set(viseme) == {"op", "id", "rms"}
    raw = face_link.encode_line(viseme)
    assert b"\n" in raw
    assert b"pixel" not in raw
    assert json.loads(raw)["id"] == "aa"


@pytest.mark.parametrize("name", ALLOWED)
def test_new_gesture_maps(name: str):
    obj = face_link.gesture_obj(name)
    assert obj == {"op": "gesture", "name": name}
    assert set(obj) == {"op", "name"}
    encoded = face_link.encode_line(obj)
    assert encoded == f'{{"op":"gesture","name":"{name}"}}\n'.encode()
    assert b"rms" not in encoded
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=False)
    assert link.forward_gesture(name.upper()) is True
    assert _objs(port.buf) == [obj]


@pytest.mark.parametrize("name", list(RETIRED) + ["moonwalk", "", None])
def test_old_and_unknown_gestures_are_ignored(name):
    assert face_link.gesture_obj(name) is None
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=False)
    assert link.forward_gesture(name) is False
    assert port.buf == b""
    # Mouth traffic still flows after an ignored name.
    link.drive(np.full(100, 0.9, dtype=np.float32), 3000)
    objs = _objs(port.buf)
    assert objs[0]["op"] == "viseme"
    assert objs[-1] == {"op": "idle"}
    assert all(item.get("op") != "gesture" for item in objs)


@pytest.mark.parametrize(
    "level,ident",
    [
        (0.0, "rest"),
        (0.0399, "rest"),
        (0.04, "mbp"),
        (0.0999, "mbp"),
        (0.10, "ee"),
        (0.1699, "ee"),
        (0.17, "oh"),
        (0.2399, "oh"),
        (0.24, "aa"),
        (1.0, "aa"),
        (2.5, "aa"),
        (-0.4, "rest"),
    ],
)
def test_rms_bins(level: float, ident: str):
    assert face_link.viseme_id_for_rms(level) == ident
    assert 0.0 <= face_link.clamp_rms(level) <= 1.0


def test_rms_of_playback_window_and_empty():
    assert face_link.rms_of(np.full(64, 0.5, dtype=np.float32)) == pytest.approx(0.5)
    assert face_link.rms_of([]) == 0.0
    assert face_link.rms_of(np.array([], dtype=np.float32)) == 0.0


def test_viseme_lines_are_about_30_hz_then_idle():
    lines = face_link.playback_lines(np.full(3000, 0.9, dtype=np.float32), 3000)
    objs = [json.loads(line) for line in lines]
    visemes = [obj for obj in objs if obj["op"] == "viseme"]
    assert len(visemes) == 30
    assert all(obj["id"] == "aa" for obj in visemes)
    assert all(set(obj) == {"op", "id", "rms"} for obj in visemes)
    assert objs[-1] == {"op": "idle"}


def test_dry_run_text_has_gesture_and_pcm_and_no_retired_name():
    text = face_link.format_dry_run("chuckle")
    objs = [json.loads(line) for line in text.splitlines()]
    assert objs[0] == {"op": "ping"}
    assert objs[1] == {"op": "gesture", "name": "chuckle"}
    assert any(obj["op"] == "viseme" and obj["id"] == "aa" for obj in objs)
    assert objs[-1] == {"op": "idle"}
    assert "stamp" not in text
    for old in RETIRED:
        assert old not in text


def test_dry_run_drops_retired_gesture_but_still_prints_pcm():
    text = face_link.format_dry_run("laugh")
    assert '"op":"gesture"' not in text
    assert '"op":"viseme"' in text
    assert text.rstrip().endswith('{"op":"idle"}')


def test_face_dry_run_cli_prints_uart_lines():
    script = _REPO / "clients" / "pi" / "crate_client.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--face-dry-run", "--face-gesture", "beam"],
        check=True,
        capture_output=True,
        text=True,
    )
    objs = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert objs[1] == {"op": "gesture", "name": "beam"}
    assert any(obj["op"] == "viseme" for obj in objs)
    assert objs[-1] == {"op": "idle"}
    assert proc.stderr == "" or "Traceback" not in proc.stderr


def test_twirl_is_parsed_and_forwarded():
    line, gesture = parse_reply("Well now.\nGESTURE: twirl")
    assert gesture == "twirl"
    assert "twirl" not in line.lower()
    obj = face_link.gesture_obj("twirl")
    assert obj == {"op": "gesture", "name": "twirl"}
    assert face_link.encode_line(obj) == b'{"op":"gesture","name":"twirl"}\n'


def test_net_vocabulary_is_the_face_set():
    assert ALLOWED_GESTURES == frozenset(ALLOWED)
    assert DEFAULT_GESTURE == "attend"
    assert set(RETIRED).isdisjoint(ALLOWED_GESTURES)


def test_mayor_prompt_no_longer_lists_stamp():
    text = (_REPO / "prompts" / "mayor_system.txt").read_text(encoding="utf-8")
    assert "Allowed gestures: tip, beam, reckon, chuckle, attend, twirl" in text
    assert "mustache twirl" in text.lower()
    assert "GESTURE: tip" in text
    assert "hat postcards, not stamp-collecting" in text
    assert "Never speak the words tip, beam, reckon, chuckle, attend, or twirl" in text
    assert "stamp, wave, think, laugh, bow" not in text
    assert text.lower().count("stamp") == 1
    for word in ("wave", "think", "laugh", "bow", "listen"):
        assert re.search(rf"\b{word}\b", text, flags=re.I) is None
    line, gesture = parse_reply("Candy is this way, citizen.\nGESTURE: reckon")
    assert gesture == "reckon"
    assert "reckon" not in line.lower()
    # Mid-sentence English stays. A leaked tag does not.
    assert sanitize_spoken("Please attend the candy bowl.") == "Please attend the candy bowl."
    assert sanitize_spoken("Candy is this way. attend") == "Candy is this way."


def test_firmware_is_esp32_classic_and_keeps_wifi_off():
    src = (_REPO / "firmware" / "esp32" / "src" / "main.cpp").read_text(encoding="utf-8")
    ini = (_REPO / "firmware" / "esp32" / "platformio.ini").read_text(encoding="utf-8")
    assert "board = esp32dev" in ini
    assert "FastLED" in ini
    assert "Adafruit PWM Servo Driver" in ini
    for token in ("18", "19", "21", "22", "23", "16", "17", "0x40"):
        assert token in src
    for name in ALLOWED:
        assert f'"{name}"' in src
    assert "CH_MUSTACHE" in src
    assert "CH_SPARE" not in src
    assert "pong" in src
    assert "FULL_WHITE_TEST" in src
    assert "WiFi.begin" not in src
    assert "WIFI_OFF" in src
    for word in RETIRED:
        assert re.search(rf"\b{word}\b", src) is None
    note = (_REPO / "hardware" / "README.md").read_text(encoding="utf-8")
    for name in ALLOWED:
        assert name in note
    assert "viseme" in note
    assert "pong" in note


_MOUTH = ("rest", "mbp", "ee", "oh", "aa")


def _danny_low_levels(n: int = 400, seed: int = 123) -> np.ndarray:
    """Seeded lognormal near danny-low: median ~0.06, p90 ~0.21, max ~0.40."""
    rng = np.random.default_rng(seed)
    mu = float(np.log(0.06))
    sigma = (float(np.log(0.21)) - mu) / 1.2815515655446004
    return np.minimum(rng.lognormal(mu, sigma, size=n), 0.40)


def _pcm_for_frame_rms(levels: np.ndarray, rate: int = 22050) -> np.ndarray:
    hop = max(1, int(round(rate / face_link.VISEME_HZ)))
    step = np.arange(hop, dtype=np.float64)
    tone = np.sin(2.0 * np.pi * step / hop)
    tone /= np.sqrt(np.mean(tone * tone))
    frames = [(tone * float(level)).astype(np.float32) for level in levels]
    return np.concatenate(frames)


def _viseme_objs(samples: np.ndarray, rate: int = 22050) -> list[dict]:
    objs = [json.loads(line) for line in face_link.playback_lines(samples, rate)]
    return [obj for obj in objs if obj["op"] == "viseme"]


def test_rms_normalizer_scales_loud_frames_and_does_not_amplify_silence():
    norm = face_link.RmsNormalizer()
    assert norm.normalize(0.0) == 0.0
    assert norm.normalize(0.05) == pytest.approx(0.05)
    assert norm.normalize(0.60) == pytest.approx(face_link.RMS_REF)
    scaled = norm.normalize(0.05)
    assert 0.0 < scaled < 0.05
    for _ in range(30 * 20):
        norm.normalize(0.0)
    assert norm.peak >= face_link.RMS_REF
    assert norm.normalize(0.05) == pytest.approx(0.05, abs=0.005)


def test_danny_low_playback_uses_every_mouth_shape():
    levels = _danny_low_levels()
    assert 0.04 <= float(np.median(levels)) <= 0.09
    assert 0.16 <= float(np.quantile(levels, 0.90)) <= 0.28
    assert 0.32 <= float(np.max(levels)) <= 0.45
    objs = _viseme_objs(_pcm_for_frame_rms(levels))
    ids = [obj["id"] for obj in objs]
    assert len(ids) == len(levels)
    assert set(ids) == set(_MOUTH)
    assert ids.count("rest") / len(ids) < 0.50
    # The UART field stays the raw frame RMS. The ESP32 scales brightness itself.
    assert max(obj["rms"] for obj in objs) == pytest.approx(float(np.max(levels)), abs=0.02)
    assert max(obj["rms"] for obj in objs) > face_link.RMS_REF


def test_louder_clipped_copy_still_uses_every_mouth_shape():
    loud = np.clip(_pcm_for_frame_rms(_danny_low_levels()) * 2.0, -1.0, 1.0)
    objs = _viseme_objs(loud.astype(np.float32))
    ids = [obj["id"] for obj in objs]
    assert set(ids) == set(_MOUTH)
    assert max(obj["rms"] for obj in objs) > 0.5


class _TimedPort(FakePort):
    def __init__(self) -> None:
        super().__init__()
        self.times: list[float] = []

    def write(self, data: bytes) -> int:
        self.times.append(time.monotonic())
        return super().write(data)


def test_paced_playback_waits_out_start_delay():
    port = _TimedPort()
    link = face_link.FaceLink(serial=port, pace=True)
    delay = 0.12
    started = time.monotonic()
    link.begin_playback(np.full(735, 0.2, dtype=np.float32), 22050, start_delay_s=delay)
    deadline = started + 0.4
    while not port.times and time.monotonic() < deadline:
        time.sleep(0.005)
    link.end_playback()
    assert port.times, "no viseme was written"
    assert port.times[0] - started >= delay - 0.02
    assert time.monotonic() - started < 0.5


def test_unpaced_playback_skips_start_delay():
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=False)
    started = time.monotonic()
    link.begin_playback(np.full(64, 0.2, dtype=np.float32), 22050, start_delay_s=0.4)
    link.end_playback()
    assert time.monotonic() - started < 0.3
    assert b'"op":"viseme"' in port.buf


def test_end_playback_interrupts_start_delay():
    port = FakePort()
    link = face_link.FaceLink(serial=port, pace=True)
    started = time.monotonic()
    link.begin_playback(np.full(735, 0.2, dtype=np.float32), 22050, start_delay_s=2.0)
    time.sleep(0.04)
    link.end_playback()
    assert time.monotonic() - started < 0.4
    assert b'"op":"viseme"' not in port.buf
    assert b'"op":"idle"' in port.buf


def test_face_start_delay_pw_includes_prime_other_backends_do_not(monkeypatch):
    monkeypatch.setattr(crate_client, "_load_local_env", lambda: {})
    monkeypatch.delenv("CRATE_FACE_LATENCY_MS", raising=False)
    monkeypatch.delenv("CRATE_BT_PRIME_MS", raising=False)
    monkeypatch.setenv("CRATE_PLAYBACK", "pw")
    assert crate_client._face_start_delay_s() == pytest.approx(0.380)
    monkeypatch.setenv("CRATE_PLAYBACK", "sounddevice")
    assert crate_client._face_start_delay_s() == pytest.approx(0.200)
    monkeypatch.setenv("CRATE_PLAYBACK", "pw")
    monkeypatch.setenv("CRATE_BT_PRIME_MS", "0")
    assert crate_client._face_start_delay_s() == pytest.approx(0.200)


def test_face_start_delay_env_override_bad_value_and_negative(monkeypatch):
    monkeypatch.setattr(
        crate_client,
        "_load_local_env",
        lambda: {"CRATE_FACE_LATENCY_MS": "10", "CRATE_BT_PRIME_MS": "10"},
    )
    monkeypatch.setenv("CRATE_PLAYBACK", "pw")
    monkeypatch.setenv("CRATE_FACE_LATENCY_MS", "40")
    monkeypatch.setenv("CRATE_BT_PRIME_MS", "20")
    assert crate_client._face_start_delay_s() == pytest.approx(0.060)

    monkeypatch.delenv("CRATE_FACE_LATENCY_MS", raising=False)
    monkeypatch.delenv("CRATE_BT_PRIME_MS", raising=False)
    monkeypatch.setenv("CRATE_PLAYBACK", "sd")
    assert crate_client._face_start_delay_s() == pytest.approx(0.010)

    monkeypatch.setenv("CRATE_FACE_LATENCY_MS", "lots")
    monkeypatch.setenv("CRATE_PLAYBACK", "sounddevice")
    assert crate_client._face_start_delay_s() == pytest.approx(0.200)

    monkeypatch.setenv("CRATE_PLAYBACK", "pw")
    monkeypatch.setenv("CRATE_FACE_LATENCY_MS", "-15")
    monkeypatch.setenv("CRATE_BT_PRIME_MS", "100")
    assert crate_client._face_start_delay_s() == pytest.approx(0.100)

    monkeypatch.setenv("CRATE_BT_PRIME_MS", "bogus")
    monkeypatch.setenv("CRATE_FACE_LATENCY_MS", "0")
    assert crate_client._face_start_delay_s() == pytest.approx(0.180)
