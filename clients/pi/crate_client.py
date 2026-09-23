#!/usr/bin/env python3
"""Crate client entry: run name-resolved body (patch onto pinned main + audio_devices).

Loads ``origin/main`` / pinned commit body, applies ``crate_client.name-resolve.patch``,
then execs. Prefer offline ``git show``; fall back to raw.githubusercontent.com.
Explicit CRATE_INPUT / CRATE_OUTPUT ints still win over name match.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_REPO = _DIR.parents[1]
_PATCH = _DIR / "crate_client.name-resolve.patch"
# main tip before this PR replaced crate_client.py with a loader.
_BASE_REV = "79c6bc26d832fc103a294789512e5247f4f4302c"
_MAIN_URL = (
    "https://raw.githubusercontent.com/PixnBits/gourdsworth/"
    f"{_BASE_REV}/clients/pi/crate_client.py"
)


def _git_show_base() -> str | None:
    for rev in (_BASE_REV, f"origin/{_BASE_REV}", "origin/main", "main"):
        try:
            out = subprocess.check_output(
                ["git", "show", f"{rev}:clients/pi/crate_client.py"],
                cwd=str(_REPO),
                stderr=subprocess.DEVNULL,
            )
            text = out.decode("utf-8")
            # Skip if this rev already is the loader (post-merge of an older tip).
            if "crate_client.name-resolve.patch" in text and "_load_code" in text:
                continue
            return text
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue
    return None


def _apply_patch(base: str) -> str:
    if not _PATCH.is_file():
        return base
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / "clients" / "pi" / "crate_client.py"
        target.parent.mkdir(parents=True)
        target.write_text(base, encoding="utf-8")
        proc = subprocess.run(
            ["patch", "-p1", "--forward", "--batch", "-i", str(_PATCH)],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if proc.returncode not in (0, 1):
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit(f"crate_client patch failed ({proc.returncode})")
        return target.read_text(encoding="utf-8")


def _load_code() -> str:
    base = _git_show_base()
    if base is None:
        with urllib.request.urlopen(_MAIN_URL, timeout=60) as resp:
            base = resp.read().decode("utf-8")
    return _apply_patch(base)


_code = _load_code()
exec(
    compile(_code, str(_DIR / "crate_client.py"), "exec"),
    {"__name__": "__main__", "__file__": str(_DIR / "crate_client.py")},
)
