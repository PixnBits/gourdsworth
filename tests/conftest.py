"""Prefer this checkout's sources over a venv editable install of another tree."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "clients" / "pi"):
    _s = str(_p)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
