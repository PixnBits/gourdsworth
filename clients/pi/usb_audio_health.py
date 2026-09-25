"""Pure helpers for USB audio health detection (CM108 / USB PnP).

Used by tests and optionally by operators; the night-of CLI is
``usb_audio_watch.sh`` (bash, no venv required for check/recover).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

CM108_VIDPID = "0d8c:013c"
CM108_ALSA_NAME = "USB PnP Sound Device"
VL805_HUB_VIDPID = "2109:3431"

# xHCI gave up. usbreset / driver rebind cannot bring the bus back; reboot.
_HC_DEAD_PATTERNS = (
    re.compile(r"HC died", re.IGNORECASE),
    re.compile(r"Host halt failed", re.IGNORECASE),
    re.compile(r"xHCI host not responding to stop endpoint command", re.IGNORECASE),
    re.compile(r"xHCI host controller not responding, assume dead", re.IGNORECASE),
)


@dataclass(frozen=True)
class HealthSnapshot:
    ok: bool
    vidpid_present: bool
    alsa_usb_pnp: bool
    alsa_card_index: Optional[int]
    reasons: tuple[str, ...]


_LSUSB_CM108 = re.compile(r"0d8c:013c", re.I)
_ARECORD_CARD = re.compile(
    r"^card\s+(\d+):\s+.*\[USB PnP Sound Device\]", re.I | re.M
)


def cm108_in_lsusb(lsusb_text: str) -> bool:
    return bool(_LSUSB_CM108.search(lsusb_text or ""))


def usb_pnp_in_arecord(arecord_text: str) -> bool:
    return bool(_ARECORD_CARD.search(arecord_text or ""))


def alsa_card_index(arecord_or_aplay_text: str) -> Optional[int]:
    m = _ARECORD_CARD.search(arecord_or_aplay_text or "")
    if not m:
        return None
    return int(m.group(1))


def parse_lsusb_bus_dev(lsusb_text: str, vidpid: str = CM108_VIDPID) -> Optional[str]:
    """Return 'BBB/DDD' path segment for usbreset, or None."""
    pat = re.compile(
        rf"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+{re.escape(vidpid)}",
        re.I,
    )
    m = pat.search(lsusb_text or "")
    if not m:
        return None
    return f"{int(m.group(1)):03d}/{int(m.group(2)):03d}"


def evaluate_health(lsusb_text: str, arecord_text: str, aplay_text: str = "") -> HealthSnapshot:
    vid = cm108_in_lsusb(lsusb_text)
    cards = (arecord_text or "") + "\n" + (aplay_text or "")
    alsa = usb_pnp_in_arecord(cards)
    idx = alsa_card_index(cards)
    reasons: list[str] = []
    if not vid:
        reasons.append("cm108_absent")
    if not alsa:
        reasons.append("alsa_usb_pnp_absent")
    if not reasons:
        reasons.append("healthy")
    return HealthSnapshot(
        ok=vid and alsa,
        vidpid_present=vid,
        alsa_usb_pnp=alsa,
        alsa_card_index=idx,
        reasons=tuple(reasons),
    )


def kernel_log_hc_dead(kernel_log: str) -> tuple[str, ...]:
    """Return stripped kernel lines that show a dead xHCI host controller."""
    found: list[str] = []
    for line in (kernel_log or "").splitlines():
        stripped = line.strip()
        if stripped and any(pat.search(stripped) for pat in _HC_DEAD_PATTERNS):
            found.append(stripped)
    return tuple(found)


def vl805_hub_present(lsusb_text: str, sysfs_vidpids: Iterable[str] = ()) -> bool:
    """True if the Pi 4 VL805 hub (2109:3431) is in lsusb text or sysfs vid:pid pairs."""
    target = VL805_HUB_VIDPID.lower()
    if target and target in (lsusb_text or "").lower():
        return True
    for raw in sysfs_vidpids:
        token = str(raw).strip().lower().replace(" ", "")
        if token == target:
            return True
    return False


@dataclass(frozen=True)
class ControllerSnapshot:
    dead: bool
    hc_died_lines: tuple[str, ...]
    hub_missing: bool
    reasons: tuple[str, ...]


def evaluate_controller(
    kernel_log: str,
    lsusb_text: str,
    *,
    expect_vl805: bool,
    sysfs_vidpids: Iterable[str] = (),
) -> ControllerSnapshot:
    """Dead if the kernel says the HC died, or a Pi 4's VL805 hub is missing.

    ``hub_missing`` is only set when ``expect_vl805`` is true — other boards
    have no VL805, so an absent hub is not a failure there.
    """
    lines = kernel_log_hc_dead(kernel_log)
    hub_missing = bool(expect_vl805) and not vl805_hub_present(lsusb_text, sysfs_vidpids)
    reasons: list[str] = []
    if lines:
        reasons.append("hc_died")
    if hub_missing:
        reasons.append("vl805_hub_missing")
    if not reasons:
        reasons.append("controller_ok")
    return ControllerSnapshot(
        dead=bool(lines) or hub_missing,
        hc_died_lines=lines,
        hub_missing=hub_missing,
        reasons=tuple(reasons),
    )


def rewrite_crate_io_env(env_text: str, card_index: int) -> str:
    """Safely rewrite only CRATE_INPUT / CRATE_OUTPUT lines."""
    lines = env_text.splitlines(keepends=True)
    out: list[str] = []
    seen_in = seen_out = False
    for line in lines:
        if re.match(r"^CRATE_INPUT=", line):
            out.append(f"CRATE_INPUT={card_index}\n" if line.endswith("\n") else f"CRATE_INPUT={card_index}")
            seen_in = True
        elif re.match(r"^CRATE_OUTPUT=", line):
            out.append(f"CRATE_OUTPUT={card_index}\n" if line.endswith("\n") else f"CRATE_OUTPUT={card_index}")
            seen_out = True
        else:
            out.append(line)
    body = "".join(out)
    if not body.endswith("\n") and body:
        body += "\n"
    if not seen_in:
        body += f"CRATE_INPUT={card_index}\n"
    if not seen_out:
        body += f"CRATE_OUTPUT={card_index}\n"
    return body
