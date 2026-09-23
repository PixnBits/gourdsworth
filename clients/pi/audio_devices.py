"""Name-based sounddevice resolve for the Pi crate client.

Prefer stable name match over fragile indices (USB card reorder, BT sink).
Explicit CRATE_INPUT / CRATE_OUTPUT ints still win when set.
"""
from __future__ import annotations

# Prefer USB CM108 dongle for capture.
INPUT_NAME_HINTS = ("USB PnP", "CM108", "C-Media")
# pipewire-alsa exposes ALSA "default" + "pipewire"; prefer default (follows wpctl sink).
OUTPUT_NAME_HINTS = ("default", "pipewire")


def match_sounddevice_index(
    devices: list,
    *,
    role: str,
    substrings: tuple[str, ...] | list[str],
) -> int | None:
    """Return first sounddevice index whose name contains a substring (case-insensitive).

    ``role`` is ``"input"`` or ``"output"``; devices with zero channels for that
    role are skipped. Earlier substrings win (so ``default`` before ``pipewire``).
    """
    needles = [s.casefold() for s in substrings if s and str(s).strip()]
    if not needles:
        return None
    for needle in needles:
        for i, d in enumerate(devices):
            name = str(d.get("name", "")).casefold()
            if needle not in name:
                continue
            if role == "input" and int(d.get("max_input_channels") or 0) <= 0:
                continue
            if role == "output" and int(d.get("max_output_channels") or 0) <= 0:
                continue
            return int(i)
    return None


def resolve_audio_device_ids(
    input_id: int | None,
    output_id: int | None,
    *,
    input_name: str | None = None,
    output_name: str | None = None,
    devices: list | None = None,
) -> tuple[int | None, int | None]:
    """Resolve sounddevice ids: explicit ints win; else name substrings / porch hints."""
    if devices is None:
        try:
            import sounddevice as sd
        except ImportError:
            return input_id, output_id
        devices = list(sd.query_devices())

    if input_id is None:
        if input_name:
            hints: tuple[str, ...] = (input_name,)
        else:
            hints = INPUT_NAME_HINTS
        input_id = match_sounddevice_index(devices, role="input", substrings=hints)

    if output_id is None:
        if output_name:
            hints = (output_name,)
        else:
            hints = OUTPUT_NAME_HINTS
        output_id = match_sounddevice_index(devices, role="output", substrings=hints)

    return input_id, output_id
