from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "pi"))

from usb_audio_health import (  # noqa: E402
    evaluate_health,
    parse_lsusb_bus_dev,
    rewrite_crate_io_env,
)


LSUSB_OK = """
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 001 Device 004: ID 0d8c:013c C-Media Electronics, Inc. CM108 Audio Controller
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
"""

LSUSB_DEAD = """
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
"""

ARECORD_OK = """
**** List of CAPTURE Hardware Devices ****
card 2: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

ARECORD_EMPTY = """
**** List of CAPTURE Hardware Devices ****
"""


def test_healthy_cm108():
    h = evaluate_health(LSUSB_OK, ARECORD_OK)
    assert h.ok
    assert h.vidpid_present
    assert h.alsa_usb_pnp
    assert h.alsa_card_index == 2
    assert h.reasons == ("healthy",)


def test_unhealthy_missing_device():
    h = evaluate_health(LSUSB_DEAD, ARECORD_EMPTY)
    assert not h.ok
    assert "cm108_absent" in h.reasons
    assert "alsa_usb_pnp_absent" in h.reasons


def test_parse_bus_dev():
    assert parse_lsusb_bus_dev(LSUSB_OK) == "001/004"
    assert parse_lsusb_bus_dev(LSUSB_DEAD) is None


def test_rewrite_env_only_io_keys():
    src = "# hi\nCRATE_HOST=x\nCRATE_INPUT=1\nCRATE_OUTPUT=0\nCRATE_MAX_EDGE=1280\n"
    out = rewrite_crate_io_env(src, 2)
    assert "CRATE_INPUT=2\n" in out
    assert "CRATE_OUTPUT=2\n" in out
    assert "CRATE_HOST=x\n" in out
    assert "CRATE_MAX_EDGE=1280\n" in out
    assert out.count("CRATE_INPUT=") == 1
