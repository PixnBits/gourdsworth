from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "mode": "ptt",
    "sample_rate": 16000,
    "listen_limit_s": 8.0,
    "silence_s": 0.85,
    "energy_threshold": 0.012,
    "ollama": {
        "host": "http://127.0.0.1:11434",
        "model": "llama3.1:8b",
        "num_predict": 40,
        "temperature": 0.6,
    },
    "stt": {"model": "base.en", "device": "auto", "compute_type": "int8"},
    "tts": {"engine": "piper", "voice": "en_US-lessac-medium", "speaker_id": 0},
    "privacy": {
        "bind_host": "127.0.0.1",
        "save_audio": False,
        "save_transcripts": False,
        "session_log": "memory",
    },
    "character": "mayor",
    "max_history_turns": 4,
    "cooldown_s": 0.4,
    "vision": {
        "enabled": False,
        "camera_index": 0,
        "width": 640,
        "height": 480,
        "min_score": 0.15,
        "model": "ViT-B-32",
        "pretrained": "openai",
        "labels_file": None,
    },
}


def _merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    candidate = path or (ROOT / "config.yaml")
    example = ROOT / "config.example.yaml"
    if candidate.exists():
        cfg = _merge(cfg, yaml.safe_load(candidate.read_text()) or {})
    elif example.exists():
        cfg = _merge(cfg, yaml.safe_load(example.read_text()) or {})
    return cfg


def prompt_path() -> Path:
    return ROOT / "prompts" / "mayor_system.txt"


def canned_path() -> Path:
    return ROOT / "canned" / "lines.json"
