"""usb_audio_watch.sh controller-dead path. Stubs stand in for lsusb/dmesg — no real USB."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "clients" / "pi" / "usb_audio_watch.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")

INCIDENT_DMESG = """\
[Wed Sep 23 01:03:49 2026] usb 2-2: current rate 16000 is different from the runtime rate 48000
[Wed Sep 23 01:03:50 2026] usb 2-2: timeout: still 12 active urbs on EP #84
[Wed Sep 23 01:03:52 2026] xhci_hcd 0000:01:00.0: xHCI host not responding to stop endpoint command
[Wed Sep 23 01:03:54 2026] xhci_hcd 0000:01:00.0: Host halt failed, -110
[Wed Sep 23 01:03:54 2026] xhci_hcd 0000:01:00.0: HC died; cleaning up
"""

LSUSB_CM108 = """\
Bus 001 Device 002: ID 2109:3431 VIA Labs, Inc. Hub
Bus 001 Device 004: ID 0d8c:013c C-Media Electronics, Inc. CM108 Audio Controller
Bus 002 Device 002: ID 046d:085e Logitech, Inc. BRIO Ultra HD Webcam
"""

LSUSB_NO_HUB = """\
Bus 001 Device 004: ID 0d8c:013c C-Media Electronics, Inc. CM108 Audio Controller
Bus 002 Device 002: ID 046d:085e Logitech, Inc. BRIO Ultra HD Webcam
"""

ARECORD_OK = """\
**** List of CAPTURE Hardware Devices ****
card 2: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

APLAY_OK = """\
**** List of PLAYBACK Hardware Devices ****
card 2: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
"""

BENIGN_DMESG = "usb 2-2: current rate 16000 is different from the runtime rate 48000\n"


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _world(tmp_path: Path, *, dmesg: str, lsusb: str, arecord: str, aplay: str, model: bytes):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "model").write_bytes(model)
    (tmp_path / "boot_id").write_text("boot-test-1\n")
    (tmp_path / "sysfs").mkdir()
    sudo_marker = tmp_path / "sudo-was-called"

    def q(path: Path) -> str:
        return shlex.quote(str(path))

    fix = tmp_path / "fix"
    fix.mkdir()
    (fix / "dmesg.txt").write_text(dmesg)
    (fix / "lsusb.txt").write_text(lsusb)
    (fix / "arecord.txt").write_text(arecord)
    (fix / "aplay.txt").write_text(aplay)
    _exe(bin_dir / "dmesg", f"#!/bin/sh\ncat {q(fix / 'dmesg.txt')}\n")
    _exe(bin_dir / "lsusb", f"#!/bin/sh\ncat {q(fix / 'lsusb.txt')}\n")
    _exe(bin_dir / "arecord", f"#!/bin/sh\ncat {q(fix / 'arecord.txt')}\n")
    _exe(bin_dir / "aplay", f"#!/bin/sh\ncat {q(fix / 'aplay.txt')}\n")
    _exe(bin_dir / "journalctl", "#!/bin/sh\nexit 0\n")
    _exe(bin_dir / "sudo", f"#!/bin/sh\necho \"$@\" >> {q(sudo_marker)}\n")
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["CRATE_USB_LOG_DIR"] = str(tmp_path / "logs")
    env["CRATE_USB_STATE_DIR"] = str(tmp_path / "state")
    env["CRATE_LOCAL_ENV"] = str(tmp_path / "missing-local.env")
    env["CRATE_USB_DT_MODEL_FILE"] = str(tmp_path / "model")
    env["CRATE_USB_BOOT_ID_FILE"] = str(tmp_path / "boot_id")
    env["CRATE_USB_SYSFS_DIR"] = str(tmp_path / "sysfs")
    env.pop("CRATE_USB_ALLOW_REBOOT", None)
    return env, sudo_marker


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _log(tmp_path: Path) -> str:
    path = tmp_path / "logs" / "usb-audio-watch.log"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_check_hc_died_exit_3_logs_once(tmp_path):
    env, _sudo = _world(
        tmp_path,
        dmesg=INCIDENT_DMESG,
        lsusb=LSUSB_CM108,
        arecord=ARECORD_OK,
        aplay=APLAY_OK,
        model=b"Raspberry Pi 4 Model B Rev 1.1\x00",
    )
    first = _run(["check"], env)
    second = _run(["check"], env)
    assert first.returncode == 3, first.stderr
    assert second.returncode == 3, second.stderr
    assert "reboot required" in first.stdout
    assert _log(tmp_path).count("USB_CONTROLLER_DEAD") == 1


def test_check_pi4_missing_vl805_hub(tmp_path):
    env, _sudo = _world(
        tmp_path,
        dmesg=BENIGN_DMESG,
        lsusb=LSUSB_NO_HUB,
        arecord=ARECORD_OK,
        aplay=APLAY_OK,
        model=b"Raspberry Pi 4 Model B Rev 1.4\x00",
    )
    result = _run(["check"], env)
    assert result.returncode == 3, result.stderr
    assert "vl805_hub_missing" in result.stdout
    assert "reboot required" in result.stdout


def test_recover_dead_controller_does_not_sudo_without_allow(tmp_path):
    env, sudo_marker = _world(
        tmp_path,
        dmesg=INCIDENT_DMESG,
        lsusb=LSUSB_CM108,
        arecord=ARECORD_OK,
        aplay=APLAY_OK,
        model=b"Raspberry Pi 4 Model B Rev 1.1\x00",
    )
    first = _run(["recover"], env)
    second = _run(["recover"], env)
    assert first.returncode == 3, first.stderr
    assert second.returncode == 3, second.stderr
    assert not sudo_marker.exists()
    assert _log(tmp_path).count("reboot decision=denied") == 1


def test_pi4_sysfs_hub_counts_as_present(tmp_path):
    env, _sudo = _world(
        tmp_path,
        dmesg=BENIGN_DMESG,
        lsusb=LSUSB_NO_HUB,
        arecord=ARECORD_OK,
        aplay=APLAY_OK,
        model=b"Raspberry Pi 4 Model B Rev 1.1\x00",
    )
    hub = tmp_path / "sysfs" / "1-1"
    hub.mkdir()
    (hub / "idVendor").write_text("2109\n")
    (hub / "idProduct").write_text("3431\n")
    result = _run(["check"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "vl805_hub_missing" not in result.stdout


def test_check_healthy_cm108_exit_0(tmp_path):
    env, sudo_marker = _world(
        tmp_path,
        dmesg=BENIGN_DMESG,
        lsusb=LSUSB_CM108,
        arecord=ARECORD_OK,
        aplay=APLAY_OK,
        model=b"Raspberry Pi 4 Model B Rev 1.1\x00",
    )
    result = _run(["check"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK:" in result.stdout
    assert "controller_dead=0" in result.stdout
    assert not sudo_marker.exists()
