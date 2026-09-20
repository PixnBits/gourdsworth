import json
from pathlib import Path

from gourdsworth.guardrails import clip_spoken, parse_reply


def test_distress_keeps_with():
    line = "That's a big feeling. Tell a grown-up you came with."
    assert clip_spoken(line).endswith("with.")
    spoken, _ = parse_reply(line)
    assert spoken.rstrip(".").endswith("with")


def test_canned_distress_intact():
    raw = json.loads(Path("canned/lines.json").read_text())["distress"][0]
    assert raw.rstrip(".").endswith("with")
    assert clip_spoken(raw).rstrip(".").endswith("with")
