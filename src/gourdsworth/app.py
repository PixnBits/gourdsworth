from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from gourdsworth.audio_io import play, record_ptt, record_vad
from gourdsworth.config import canned_path, load_config, prompt_path
from gourdsworth.guardrails import looks_distress, model_went_dark, parse_reply
from gourdsworth.llm import LocalMayor
from gourdsworth.metrics import TurnMetrics
from gourdsworth.stt import SpeechToText
from gourdsworth.tts import Speaker


def _load_canned() -> dict:
    return json.loads(canned_path().read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mayor Gourdsworth — local voice greeter")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--mode", choices=["ptt", "vad"], default=None)
    parser.add_argument("--model", default=None, help="Override Ollama model name")
    parser.add_argument("--dry-run", action="store_true", help="Skip audio; type lines instead")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    if args.model:
        cfg["ollama"]["model"] = args.model

    if cfg["privacy"].get("save_audio") or cfg["privacy"].get("save_transcripts"):
        print("Refusing to start: save_audio / save_transcripts must stay false.")
        return 2

    system = prompt_path().read_text()
    canned = _load_canned()
    print("Gourdsworth 0.1 — local only, audio stays in RAM")
    print(f"  mode={cfg['mode']}  ollama={cfg['ollama']['model']}  stt={cfg['stt']['model']}")

    mayor = LocalMayor(
        host=cfg["ollama"]["host"],
        model=cfg["ollama"]["model"],
        num_predict=int(cfg["ollama"]["num_predict"]),
        temperature=float(cfg["ollama"]["temperature"]),
        system=system,
    )
    try:
        mayor.ping()
    except Exception as exc:
        print(f"Ollama not ready at {cfg['ollama']['host']}: {exc}")
        print("Start ollama and pull a small instruct model, e.g. `ollama pull llama3.1:8b`")
        return 1

    stt = None
    speaker = None
    if not args.dry_run:
        print("  loading STT…")
        stt = SpeechToText(cfg["stt"]["model"], cfg["stt"]["device"], cfg["stt"]["compute_type"])
        print("  loading TTS…")
        try:
            speaker = Speaker(cfg["tts"]["engine"], cfg["tts"]["voice"])
        except Exception as exc:
            print(f"  Piper failed ({exc}); trying espeak fallback")
            speaker = Speaker("espeak", cfg["tts"]["voice"])

    history: list[dict] = []
    print()
    print("Disembodied test loop. Kids hear a voice with no pumpkin yet — that is the point.")
    print("  Enter  = start a turn" + (" (then Enter again to stop listening)" if cfg["mode"] == "ptt" else ""))
    print("  q      = quit")
    print()

    opener = random.choice(canned["opener"])
    if not args.dry_run and speaker:
        audio, rate, _, _ = speaker.synthesize(opener)
        print(f"Mayor: {opener}")
        play(audio, rate)
    else:
        print(f"Mayor: {opener}")

    while True:
        try:
            cmd = input("ready> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if cmd in {"q", "quit", "exit", "goodnight"}:
            line = random.choice(canned["goodnight"])
            print(f"Mayor: {line}")
            break
        if cmd not in {"", "go", "talk", "t"}:
            user_text = cmd
            metrics = TurnMetrics()
            _handle_turn(user_text, mayor, speaker, canned, history, cfg, metrics, typed=True)
            continue

        metrics = TurnMetrics()
        if args.dry_run:
            user_text = input("type the kid> ").strip()
            metrics.record_ms = 0.0
        elif cfg["mode"] == "vad":
            audio_in, metrics.record_ms = record_vad(
                cfg["sample_rate"],
                cfg["listen_limit_s"],
                cfg["silence_s"],
                cfg["energy_threshold"],
            )
            user_text, metrics.stt_ms = stt.transcribe(audio_in, cfg["sample_rate"])
            del audio_in
        else:
            audio_in, metrics.record_ms = record_ptt(cfg["sample_rate"], cfg["listen_limit_s"])
            user_text, metrics.stt_ms = stt.transcribe(audio_in, cfg["sample_rate"])
            del audio_in

        _handle_turn(user_text, mayor, speaker, canned, history, cfg, metrics, typed=args.dry_run)
        time.sleep(float(cfg["cooldown_s"]))

    return 0


def _handle_turn(user_text, mayor, speaker, canned, history, cfg, metrics, typed=False):
    user_text = (user_text or "").strip()
    metrics.words_in = len(user_text.split())
    print(f"Heard: {user_text!r}" if user_text else "Heard: (silence)")

    if looks_distress(user_text):
        line = canned["distress"][0]
        gesture = "listen"
        metrics.used_canned = True
    elif not user_text:
        line = random.choice(canned["shy"])
        gesture = "stamp"
        metrics.used_canned = True
    else:
        raw, metrics.llm_ttft_ms, metrics.llm_total_ms = mayor.reply(user_text, history)
        line, gesture = parse_reply(raw)
        if not line or model_went_dark(line):
            line = random.choice(canned["fallback"])
            gesture = "stamp"
            metrics.used_canned = True

    metrics.words_out = len(line.split())
    print(f"Mayor: {line}")
    print(f"Gesture: {gesture}")

    if speaker is not None:
        samples, rate, metrics.tts_first_ms, metrics.tts_total_ms = speaker.synthesize(line)
        metrics.play_ms = play(samples, rate)
        del samples

    if cfg["privacy"]["session_log"] == "memory" and user_text and not metrics.used_canned:
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": line})
        cap = int(cfg["max_history_turns"]) * 2
        del history[:-cap]

    print(metrics.render())
    print()


if __name__ == "__main__":
    sys.exit(main())
