"""Gesture goes out once, before the remainder of an early-flush reply is played."""

from gourdsworth.app import _handle_turn
from gourdsworth.metrics import TurnMetrics


class _Mayor:
    def reply_stream(self, user_text, history, visual_note=None):
        text = (
            "Welcome to the porch, citizens! "
            "The candy bowl is this way.\n"
            "GESTURE: beam"
        )
        yield text, 1.0, True


class _Speaker:
    def synthesize(self, text):
        return (None, 22050, 1.0, 1.0)


def test_gesture_fires_once_before_remainder_playback():
    events: list[tuple[str, str]] = []

    def play_fn(samples, rate):
        events.append(("play", ""))
        return 1.0

    def gesture_fn(name):
        events.append(("gesture", name))

    _handle_turn(
        "hello porch friend",
        _Mayor(),
        _Speaker(),
        {"fallback": ["Candy awaits, citizens."]},
        [],
        {"privacy": {"session_log": "off"}, "max_history_turns": 2},
        TurnMetrics(),
        play_fn=play_fn,
        gesture_fn=gesture_fn,
    )
    plays = [i for i, (kind, _name) in enumerate(events) if kind == "play"]
    gestures = [i for i, (kind, _name) in enumerate(events) if kind == "gesture"]
    assert len(plays) >= 2
    assert len(gestures) == 1
    assert plays[0] < gestures[0] < plays[1]
    assert events[gestures[0]][1] == "beam"
