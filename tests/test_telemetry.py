"""Crate telemetry event cache + thermal parsing + debug spoken formatter."""

from __future__ import annotations

import socket

from gourdsworth.metrics import SessionStats, TurnMetrics, format_debug_spoken
from gourdsworth.net import CrateConnection, parse_thermal_sysfs_temp, send_msg


def test_parse_thermal_sysfs_temp():
    assert parse_thermal_sysfs_temp("47125") == 47.125
    assert parse_thermal_sysfs_temp(b"38000\n") == 38.0
    assert parse_thermal_sysfs_temp("0") is None
    assert parse_thermal_sysfs_temp("-1") is None
    assert parse_thermal_sysfs_temp("not-a-number") is None
    assert parse_thermal_sysfs_temp(None) is None
    assert parse_thermal_sysfs_temp("999999") is None


def test_crate_connection_caches_telemetry_without_event_queue():
    a, b = socket.socketpair()
    try:
        conn = CrateConnection(b)
        try:
            send_msg(a, {"event": "telemetry", "cpu_temp_c": 47.1})
            # Also send a real control event so we can wait on the queue.
            send_msg(a, {"event": "ready"})
            got = conn.wait_event("ready", timeout=2.0)
            assert got is not None
            assert got[0]["event"] == "ready"
            # Telemetry must not appear in wait_any / wait_event.
            assert conn.wait_event("telemetry", timeout=0.2) is None
            telem = conn.latest_telemetry
            assert telem is not None
            assert telem["event"] == "telemetry"
            assert telem["cpu_temp_c"] == 47.1
            # Latest wins
            send_msg(a, {"event": "telemetry", "cpu_temp_c": 51.0})
            send_msg(a, {"event": "listen"})
            assert conn.wait_event("listen", timeout=2.0) is not None
            assert conn.latest_telemetry["cpu_temp_c"] == 51.0
        finally:
            conn.close()
    finally:
        a.close()
        b.close()


def test_session_stats_running_average_excludes_zeros():
    stats = SessionStats()
    assert stats.average_response() is None

    m1 = TurnMetrics()
    m1.stt_ms = 200
    m1.to_first_audio_ms = 800
    m1.post_speech_silence_ms = 600  # first_syllable = 1600
    stats.note_completed_turn(m1)

    m_bad = TurnMetrics()  # zero / failed → ignored
    stats.note_completed_turn(m_bad)

    m2 = TurnMetrics()
    m2.stt_ms = 100
    m2.to_first_audio_ms = 500
    m2.post_speech_silence_ms = 400  # first_syllable = 1000
    stats.note_completed_turn(m2)

    avg_s, n = stats.average_response()
    assert n == 2
    assert abs(avg_s - 1.3) < 1e-6  # (1600+1000)/2/1000


def test_format_debug_spoken_includes_temp_and_average():
    stats = SessionStats()
    m = TurnMetrics()
    m.stt_ms = 200
    m.to_first_audio_ms = 800
    m.post_speech_silence_ms = 600
    stats.note_completed_turn(m)

    line = format_debug_spoken(
        uplink_first_ms=42.0,
        telemetry={"event": "telemetry", "cpu_temp_c": 47.2},
        session_stats=stats,
    )
    assert "Agent P debug channel open." in line
    assert "Uplink first 42 milliseconds." in line
    assert "Pi temperature 47 degrees." in line
    assert "Average response 1.6 seconds across 1 turns." in line


def test_format_debug_spoken_omits_missing_temp_and_average():
    line = format_debug_spoken()
    assert "Pi temperature" not in line
    assert "Average response" not in line
    assert "No still ready yet." in line


def test_format_percent_spoken_words():
    from gourdsworth.metrics import format_percent_spoken, score_as_percent

    assert score_as_percent(0.20) == 20
    assert score_as_percent(0.16) == 16
    assert format_percent_spoken(0.20) == "twenty percent"
    assert format_percent_spoken(0.16) == "sixteen percent"
    assert format_percent_spoken(0.05) == "five percent"
    assert format_percent_spoken(1.0) == "one hundred percent"


def test_format_debug_spoken_costume_percent_words_sorted():
    from gourdsworth.metrics import format_debug_spoken
    from gourdsworth.vision import VisionResult, format_visual_note

    vis = VisionResult(
        label="pirate",
        score=0.20,
        top3=[("witch", 0.11), ("pirate", 0.20), ("robot", 0.16)],
        note=format_visual_note([("pirate", 0.20), ("robot", 0.16)]),
    )
    line = format_debug_spoken(vis=vis)
    assert "Vision says pirate at twenty percent." in line
    # Sorted high→low, spoken percents (not zero point …)
    assert "Top guesses: pirate twenty percent, robot sixteen percent, witch eleven percent." in line
    assert "0.20" not in line
    assert "zero point" not in line.lower()


def test_format_debug_spoken_empty_walk_lists_top_guesses():
    from gourdsworth.metrics import format_debug_spoken
    from gourdsworth.vision import VisionResult, format_visual_note

    # persons=0 → select_costume_labels returns homemade, but spoken must be honest.
    vis = VisionResult(
        label="homemade",
        score=0.02,
        top3=[("vampire", 0.18), ("ghost", 0.09), ("witch", 0.07)],
        note=format_visual_note([("homemade", 0.02)], person_count=0),
        person_count=0,
    )
    line = format_debug_spoken(vis=vis)
    assert "Vision says the walk looks empty." in line
    assert "Vision says homemade" not in line
    assert (
        "Top guesses: vampire eighteen percent, ghost nine percent, witch seven percent."
        in line
    )


def test_format_debug_spoken_primary_follows_top3_not_stale_label():
    from gourdsworth.metrics import format_debug_spoken
    from gourdsworth.vision import VisionResult, format_visual_note

    # Live porch contradiction: label/score homemade 0.02 vs top3 vampire 0.18.
    vis = VisionResult(
        label="homemade",
        score=0.02,
        top3=[("witch", 0.07), ("vampire", 0.18), ("ghost", 0.09)],
        note=format_visual_note(
            [("vampire", 0.18), ("ghost", 0.09), ("witch", 0.07)],
            person_count=1,
        ),
        person_count=1,
    )
    line = format_debug_spoken(vis=vis)
    assert "Vision says vampire at eighteen percent." in line
    assert "Vision says homemade" not in line
    assert (
        "Top guesses: vampire eighteen percent, ghost nine percent, witch seven percent."
        in line
    )
