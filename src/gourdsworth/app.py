from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from time import perf_counter

from gourdsworth.audio_io import list_devices, play, record_ptt, record_vad, set_devices
from gourdsworth.config import canned_path, load_config, prompt_path
from gourdsworth.guardrails import (
    early_speakable,
    looks_distress,
    model_went_dark,
    parse_reply,
    remainder_after,
)
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
    parser.add_argument(
        "--stt-model",
        default=None,
        choices=["tiny.en", "base.en", "small.en", "turbo"],
        help="Override faster-whisper model",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip audio; type lines instead")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print sounddevice input/output ids and exit",
    )
    parser.add_argument("--input", type=int, default=None, metavar="N", help="Input device id")
    parser.add_argument("--output", type=int, default=None, metavar="N", help="Output device id")
    args = parser.parse_args(argv)

    if args.list_devices:
        print(list_devices())
        return 0

    if args.input is not None or args.output is not None:
        try:
            set_devices(args.input, args.output)
        except OSError as exc:
            print(f"Could not set audio devices ({exc})")
            if not args.dry_run:
                return 1

    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    if args.model:
        cfg["ollama"]["model"] = args.model
    if args.stt_model:
        cfg["stt"]["model"] = args.stt_model
    cfg["_input_device"] = args.input
    cfg["_output_device"] = args.output

    if cfg["privacy"].get("save_audio") or cfg["privacy"].get("save_transcripts"):
        print("Refusing to start: save_audio / save_transcripts must stay false.")
        return 2

    system = prompt_path().read_text()
    canned = _load_canned()
    print("Gourdsworth 0.1 — local only, audio stays in RAM")
    print(f"  mode={cfg['mode']}  ollama={cfg['ollama']['model']}  stt={cfg['stt']['model']}")
    if args.input is not None or args.output is not None:
        print(f"  devices: input={args.input} output={args.output}")

    mayor = LocalMayor(
        host=cfg["ollama"]["host"],
        model=cfg["ollama"]["model"],
        num_predict=int(cfg["ollama"]["num_predict"]),
        temperature=float(cfg["ollama"]["temperature"]),
        system=system,
    )

    # --- Preload: Ollama resolve + keep_alive warm, Whisper, Piper ---
    print("  loading Ollama…")
    t0 = perf_counter()
    try:
        chosen = mayor.ping()
        cfg["ollama"]["model"] = chosen
        ollama_resolve_ms = (perf_counter() - t0) * 1000
        print(f"  ollama model={chosen}  resolve={ollama_resolve_ms:.0f}ms")
        t1 = perf_counter()
        warm_ms = mayor.warm()
        print(f"  ollama warm(keep_alive)={warm_ms:.0f}ms  (ping wall={(perf_counter()-t1)*1000:.0f}ms)")
    except Exception as exc:
        print(f"Ollama not ready at {cfg['ollama']['host']}: {exc}")
        print("Start ollama and pull a small instruct model, e.g. `ollama pull qwen2.5:14b`")
        return 1

    stt = None
    speaker = None
    # Preload STT + TTS even in dry-run so load times are visible; dry-run still skips mic/play.
    print("  loading STT…")
    t0 = perf_counter()
    try:
        stt = SpeechToText(cfg["stt"]["model"], cfg["stt"]["device"], cfg["stt"]["compute_type"])
        print(f"  stt load={((perf_counter()-t0)*1000):.0f}ms  model={cfg['stt']['model']}")
    except Exception as exc:
        if args.dry_run:
            print(f"  STT load skipped/failed in dry-run ({exc})")
        else:
            print(f"  STT failed: {exc}")
            return 1

    print("  loading TTS…")
    t0 = perf_counter()
    try:
        speaker = Speaker(cfg["tts"]["engine"], cfg["tts"]["voice"])
        print(f"  tts load={((perf_counter()-t0)*1000):.0f}ms  engine={cfg['tts']['engine']}")
    except Exception as exc:
        print(f"  Piper failed ({exc}); trying espeak fallback")
        try:
            t0 = perf_counter()
            speaker = Speaker("espeak", cfg["tts"]["voice"])
            print(f"  tts load={((perf_counter()-t0)*1000):.0f}ms  engine=espeak")
        except Exception as exc2:
            if args.dry_run:
                print(f"  TTS unavailable in dry-run ({exc2}); will print only")
                speaker = None
            else:
                print(f"  TTS failed: {exc2}")
                return 1

    history: list[dict] = []
    print()
    print("Disembodied test loop. Kids hear a voice with no pumpkin yet — that is the point.")
    print("  Enter  = start a turn" + (" (then Enter again to stop listening)" if cfg["mode"] == "ptt" else " (VAD listens until you pause)"))
    print("  or type a kid line to skip the mic (speakers still play)")
    print("  q      = quit")
    print()

    opener = random.choice(canned["opener"])
    if not args.dry_run and speaker:
        audio, rate, _, _ = speaker.synthesize(opener)
        print(f"Mayor: {opener}")
        play(audio, rate, cfg.get("_output_device"))
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
            # Typed kid-proxy line: still run TTS/speakers (only --dry-run sets typed=True)
            user_text = cmd
            metrics = TurnMetrics()
            _handle_turn(
                user_text, mayor, speaker, canned, history, cfg, metrics, typed=args.dry_run
            )
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
                input_device=cfg.get("_input_device"),
            )
            user_text, metrics.stt_ms = stt.transcribe(audio_in, cfg["sample_rate"])
            del audio_in
        else:
            audio_in, metrics.record_ms = record_ptt(
                cfg["sample_rate"],
                cfg["listen_limit_s"],
                input_device=cfg.get("_input_device"),
            )
            user_text, metrics.stt_ms = stt.transcribe(audio_in, cfg["sample_rate"])
            del audio_in

        _handle_turn(user_text, mayor, speaker, canned, history, cfg, metrics, typed=args.dry_run)
        time.sleep(float(cfg["cooldown_s"]))

    return 0



def _speakable_remainder(full: str, spoken_prefix: str) -> str:
    """Tail after early flush; ignore quote/punct-only leftovers."""
    rem = remainder_after(full, spoken_prefix).strip()
    if not rem:
        return ""
    if all(ch in " \t.,!?;:\"'“”‘’…" for ch in rem):
        return ""
    return rem


def _handle_turn(user_text, mayor, speaker, canned, history, cfg, metrics, typed=False):
    user_text = (user_text or "").strip()
    metrics.words_in = len(user_text.split())
    print(f"Heard: {user_text!r}" if user_text else "Heard: (silence)")

    gesture = "stamp"
    line = ""
    t_post_stt = perf_counter()

    if looks_distress(user_text):
        line = canned["distress"][0]
        gesture = "listen"
        metrics.used_canned = True
    elif not user_text:
        line = random.choice(canned["shy"])
        gesture = "stamp"
        metrics.used_canned = True
    else:
        # M1: stream tokens; start TTS on first sentence or 12 words — do not wait for full reply
        parts: list[str] = []
        early: str | None = None
        ttft = None
        t_llm0 = perf_counter()
        for piece, piece_ttft, done in mayor.reply_stream(user_text, history):
            if piece_ttft is not None and ttft is None:
                ttft = piece_ttft
                metrics.llm_ttft_ms = ttft
            if piece:
                parts.append(piece)
            buf = "".join(parts)
            if early is None:
                early = early_speakable(buf, min_words=12)
                if early and speaker is not None:
                    metrics.early_flush = True
                    samples, rate, tts_first, tts_total = speaker.synthesize(early)
                    if metrics.tts_first_ms == 0:
                        metrics.tts_first_ms = tts_first
                    metrics.tts_total_ms += tts_total
                    if metrics.to_first_audio_ms == 0:
                        metrics.to_first_audio_ms = (perf_counter() - t_post_stt) * 1000
                    if not typed:
                        metrics.play_ms += play(samples, rate, cfg.get("_output_device"))
                    del samples
            if done:
                break
        metrics.llm_total_ms = (perf_counter() - t_llm0) * 1000
        raw = "".join(parts).strip()
        line, gesture = parse_reply(raw)
        if not line or model_went_dark(line):
            line = random.choice(canned["fallback"])
            gesture = "stamp"
            metrics.used_canned = True
            early = None  # speak full canned below
        elif early:
            # Speak only the not-yet-spoken tail (if any)
            rem = _speakable_remainder(line, early)
            if rem and speaker is not None and not typed:
                samples, rate, tts_first, tts_total = speaker.synthesize(rem)
                metrics.tts_total_ms += tts_total
                metrics.play_ms += play(samples, rate, cfg.get("_output_device"))
                del samples
            elif rem and speaker is not None and typed:
                samples, rate, tts_first, tts_total = speaker.synthesize(rem)
                if metrics.tts_first_ms == 0:
                    metrics.tts_first_ms = tts_first
                metrics.tts_total_ms += tts_total
                del samples

    metrics.words_out = len(line.split())
    print(f"Mayor: {line}")
    print(f"Gesture: {gesture}")

    # Canned / no-early path: synthesize full line once
    if speaker is not None and (metrics.used_canned or not metrics.early_flush):
        if typed:
            samples, rate, metrics.tts_first_ms, metrics.tts_total_ms = speaker.synthesize(line)
            if metrics.to_first_audio_ms == 0:
                metrics.to_first_audio_ms = (perf_counter() - t_post_stt) * 1000
            del samples
        else:
            samples, rate, metrics.tts_first_ms, metrics.tts_total_ms = speaker.synthesize(line)
            if metrics.to_first_audio_ms == 0:
                metrics.to_first_audio_ms = (perf_counter() - t_post_stt) * 1000
            metrics.play_ms = play(samples, rate, cfg.get("_output_device"))
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
