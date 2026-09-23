"""Pure helpers for USB audio health detection (CM108 / USB PnP).

Used by tests and optionally by operators; the night-of CLI is
``usb_audio_watch.sh`` (bash, no venv required for check/recover).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

CM108_VIDPID = "0d8c:013c"
CM108_ALSA_NAME = "USB PnP Sound Device"


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
