from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients" / "pi"))

from usb_audio_health import (  # noqa: E402
    evaluate_controller,
    evaluate_health,
    kernel_log_hc_dead,
    parse_lsusb_bus_dev,
    rewrite_crate_io_env,
    vl805_hub_present,
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


INCIDENT_DMESG = """\
[Wed Sep 23 01:03:49 2026] usb 2-2: current rate 16000 is different from the runtime rate 48000
[Wed Sep 23 01:03:50 2026] usb 2-2: timeout: still 12 active urbs on EP #84
[Wed Sep 23 01:03:52 2026] xhci_hcd 0000:01:00.0: xHCI host not responding to stop endpoint command
[Wed Sep 23 01:03:54 2026] xhci_hcd 0000:01:00.0: Host halt failed, -110
[Wed Sep 23 01:03:54 2026] xhci_hcd 0000:01:00.0: HC died; cleaning up
"""

PI4_LSUSB_HEALTHY = """\
Bus 001 Device 002: ID 2109:3431 VIA Labs, Inc. Hub
Bus 002 Device 002: ID 046d:085e Logitech, Inc. BRIO Ultra HD Webcam
"""


def test_kernel_log_hc_dead_incident_lines():
    lines = kernel_log_hc_dead(INCIDENT_DMESG)
    assert len(lines) == 3
    joined = "\n".join(lines)
    assert "xHCI host not responding to stop endpoint command" in joined
    assert "Host halt failed, -110" in joined
    assert "HC died; cleaning up" in joined
    assert "current rate" not in joined
    assert "active urbs" not in joined
    assume = kernel_log_hc_dead(
        "xhci_hcd 0000:01:00.0: xHCI host controller not responding, assume dead\n"
    )
    assert len(assume) == 1
    assert kernel_log_hc_dead("") == ()


def test_evaluate_controller_hub_missing_only_on_pi4():
    lsusb = "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
    missing = evaluate_controller("quiet\n", lsusb, expect_vl805=True)
    assert missing.dead
    assert missing.hub_missing
    assert missing.hc_died_lines == ()
    assert missing.reasons == ("vl805_hub_missing",)
    other = evaluate_controller("quiet\n", lsusb, expect_vl805=False)
    assert not other.dead
    assert not other.hub_missing
    assert other.reasons == ("controller_ok",)


def test_evaluate_controller_healthy_and_hc_died():
    spam = "usb 2-2: current rate 16000 is different from the runtime rate 48000\n"
    healthy = evaluate_controller(spam, PI4_LSUSB_HEALTHY, expect_vl805=True)
    assert not healthy.dead
    assert healthy.reasons == ("controller_ok",)
    died = evaluate_controller(INCIDENT_DMESG, PI4_LSUSB_HEALTHY, expect_vl805=True)
    assert died.dead
    assert not died.hub_missing
    assert died.reasons == ("hc_died",)
    assert len(died.hc_died_lines) == 3
    both = evaluate_controller(INCIDENT_DMESG, "no hub\n", expect_vl805=True)
    assert both.dead and both.hub_missing
    assert both.reasons == ("hc_died", "vl805_hub_missing")


def test_vl805_hub_present_sysfs_pairs():
    assert vl805_hub_present(PI4_LSUSB_HEALTHY)
    assert vl805_hub_present("no hub here", ["2109:3431"])
    assert vl805_hub_present("no hub here", [" 2109:3431 "])
    assert not vl805_hub_present("no hub here", ["0d8c:013c", "046d:085e"])
    assert not vl805_hub_present("", ())


def test_rewrite_env_only_io_keys():
    src = "# hi\nCRATE_HOST=x\nCRATE_INPUT=1\nCRATE_OUTPUT=0\nCRATE_MAX_EDGE=1280\n"
    out = rewrite_crate_io_env(src, 2)
    assert "CRATE_INPUT=2\n" in out
    assert "CRATE_OUTPUT=2\n" in out
    assert "CRATE_HOST=x\n" in out
    assert "CRATE_MAX_EDGE=1280\n" in out
    assert out.count("CRATE_INPUT=") == 1
