#!/usr/bin/env python3
"""Gourdsworth crate client entry — loads audio_devices-aware body."""
from __future__ import annotations

from pathlib import Path

_dir = Path(__file__).resolve().parent
_code = (_dir / "crate_client_impl.py.part0").read_text(encoding="utf-8") + (
    _dir / "crate_client_impl.py.part1"
).read_text(encoding="utf-8")
exec(
    compile(_code, str(_dir / "crate_client_impl.py"), "exec"),
    {"__name__": "__main__", "__file__": str(_dir / "crate_client.py")},
)
