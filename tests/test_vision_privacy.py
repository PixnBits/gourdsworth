import re
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path
from time import perf_counter

from gourdsworth.llm import format_user_message
from gourdsworth.metrics import TurnMetrics
from gourdsworth import vision as vision_module
from gourdsworth.vision import (
    DEFAULT_LABELS,
    NOTE_PREFIX,
    VisionResult,
    VisionSidecar,
    attach_visual_note,
    classify_costume,
    format_visual_note,
    load_labels,
    missing_vision_deps,
    note_contains_identity,
    run_still,
    safe_visual_note,
    take_ready,
)


def _high_firefighter(jpeg, labels):
    assert isinstance(jpeg, (bytes, bytearray))
    return [(lab, 0.91 if lab == "firefighter" else 0.03) for lab in labels]


def test_default_labels_are_the_closed_costume_list():
    labels = load_labels()
    assert labels == list(DEFAULT_LABELS)
    assert "homemade" in labels
    assert "firefighter" in labels
    for lab in labels:
        assert not note_contains_identity(lab)


def test_every_default_label_note_is_identity_free():
    for lab in DEFAULT_LABELS:
        note = format_visual_note(lab)
        assert note.startswith(NOTE_PREFIX)
        assert not note_contains_identity(note)
        assert "years old" not in note.lower()
        assert "named" not in note.lower()


def test_boilerplate_name_word_is_not_an_identity_hit():
    note = format_visual_note("pirate")
    assert "never name a person" in note
    assert not note_contains_identity(note)
    assert safe_visual_note(note) == note


def test_identity_words_in_notes_are_rejected():
    bad = f"{NOTE_PREFIX} a girl named Emma, 8 years old, face visible."
    assert note_contains_identity(bad)
    assert safe_visual_note(bad) is None
    assert attach_visual_note("trick or treat", bad) == "trick or treat"

    coerced = format_visual_note("girl named emma")
    assert coerced == format_visual_note("homemade")
    assert "emma" not in coerced.lower()
    assert "girl" not in coerced[len(NOTE_PREFIX) :].lower()


def test_unknown_or_low_score_defaults_homemade():
    def low(jpeg, labels):
        return [(lab, 0.02) for lab in labels]

    result = classify_costume(b"x", DEFAULT_LABELS, classify_fn=low, min_score=0.15)
    assert result.label == "homemade"
    assert "homemade" in result.note
    assert not note_contains_identity(result.note)

    def outsider(jpeg, labels):
        return [("secret identity", 0.99)]

    result2 = classify_costume(b"x", DEFAULT_LABELS, classify_fn=outsider)
    assert result2.label == "homemade"


def test_run_still_leaves_no_tempfiles(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    jpeg = b"\xff\xd8in-ram-only\xff\xd9"
    result = run_still(
        capture_fn=lambda: jpeg,
        classify_fn=_high_firefighter,
        labels=DEFAULT_LABELS,
    )
    assert list(tmp_path.iterdir()) == []
    assert result.label == "firefighter"
    assert result.top3[0][0] == "firefighter"
    assert "firefighter" in result.note
    assert not any(isinstance(v, (bytes, bytearray, memoryview)) for v in vars(result).values())


def test_vision_module_has_no_disk_or_tempfile_api():
    src = Path(vision_module.__file__).read_text(encoding="utf-8")
    assert "tempfile" not in src
    assert "NamedTemporaryFile" not in src
    assert "mkstemp" not in src
    assert "TemporaryDirectory" not in src
    assert re.search(r"imwrite\s*\(", src) is None
    assert "write_bytes" not in src
    assert "np.save" not in src


def test_take_ready_never_waits():
    fut: Future = Future()
    t0 = perf_counter()
    assert take_ready(fut) is None
    assert take_ready(None) is None
    assert (perf_counter() - t0) < 0.05

    done: Future = Future()
    done.set_result(VisionResult(label="ninja", note=format_visual_note("ninja"), score=0.4))
    got = take_ready(done)
    assert got is not None
    assert got.label == "ninja"


def test_first_syllable_ignores_vision_ms():
    m = TurnMetrics()
    m.stt_ms = 100
    m.to_first_audio_ms = 200
    m.vision_ms = 9000
    m.vision_used = True
    m.vision_label = "firefighter"
    m.vision_score = 0.8
    assert m.first_audio_from_silence_ms() == 300
    rendered = m.render()
    assert "vision=" in rendered
    assert "vision_used=1" in rendered
    assert "firefighter" in rendered


def test_metrics_omit_vision_when_unused():
    assert "vision=" not in TurnMetrics().render()


def test_visual_note_attaches_only_to_this_turn():
    note = format_visual_note("witch")
    user = attach_visual_note("trick or treat", note)
    assert user.startswith(NOTE_PREFIX)
    assert user.endswith("trick or treat")
    assert format_user_message("trick or treat", note).endswith("trick or treat")
    assert format_user_message("hello", None) == "hello"


def test_submit_snap_skips_in_flight_turn():
    sidecar = VisionSidecar(labels=DEFAULT_LABELS)
    started = threading.Event()
    release = threading.Event()

    def slow_snap():
        started.set()
        release.wait(timeout=2)
        return VisionResult(label="robot", note=format_visual_note("robot"), score=0.5)

    sidecar.snap = slow_snap  # type: ignore[method-assign]
    first = sidecar.submit_snap()
    assert first is not None
    assert started.wait(timeout=2)
    assert sidecar.submit_snap() is None
    release.set()
    assert first.result(timeout=2).label == "robot"


def test_missing_deps_message_points_at_extras():
    msg = missing_vision_deps()
    if msg is not None:
        assert "gourdsworth[vision]" in msg


def test_help_lists_snap_and_vision(capsys):
    from gourdsworth.app import main

    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--snap" in out
    assert "--vision" in out
    assert "--camera" in out
