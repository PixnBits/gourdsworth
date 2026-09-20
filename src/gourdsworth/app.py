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
    looks_tease,
    model_went_dark,
    parse_reply,
    remainder_after,
)
from gourdsworth.llm import LocalMayor
from gourdsworth.metrics import TurnMetrics
from gourdsworth.stt import SpeechToText
from gourdsworth.tts import Speaker
from gourdsworth.vision import (
    PRIVACY_SIGN,
    VisionSidecar,
    format_top3,
    take_ready,
)


def _load_canned() -> dict:
    return json.loads(canned_path().read_text())


def _kick_vision(sidecar: VisionSidecar | None):
    """Start a still; never wait. None if vision is off or the previous still is still running."""
    if sidecar is None:
        return None
    return sidecar.submit_snap()


def _kitchen_still(cfg: dict, dry_run: bool = False) -> int:
    print("Gourdsworth vision V0 — kitchen still (RAM only, local CLIP)")
    print(PRIVACY_SIGN)
    sidecar = VisionSidecar.from_config(cfg)
    ok, reason = sidecar.prepare()
    if not ok:
        print(f"vision skipped: {reason}")
        sidecar.close()
        return 1
    print(
        f"  CLIP {sidecar.model_name} ({sidecar.pretrained})  "
        f"load={sidecar.load_ms:.0f}ms  camera={sidecar.camera_index}"
    )
    result = sidecar.snap()
    sidecar.close()
    if result.skip_reason:
        print(f"vision skipped: {result.skip_reason}")
        return 1
    print(f"  top: {format_top3(result.top3)}")
    print(
        f"  vision_ms={result.vision_ms:.0f}  "
        f"vision_label={result.label}  "
        f"vision_score={result.score:.2f}  "
        f"vision_used=0"
    )
    print(result.note)
    if dry_run:
        print("  (dry-run: note not sent to the mayor)")
    return 0


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
        "--continuous",
        action="store_true",
        help="Hands-free porch loop: auto-listen after each reply (implies vad; Ctrl+C to quit)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print sounddevice input/output ids and exit",
    )
    parser.add_argument("--input", type=int, default=None, metavar="N", help="Input device id")
    parser.add_argument("--output", type=int, default=None, metavar="N", help="Output device id")
    parser.add_argument(
        "--vision",
        action="store_true",
        help="Opt in to one RAM still + local CLIP costume note on Talk (never blocks voice)",
    )
    parser.add_argument(
        "--snap",
        action="store_true",
        help="Kitchen still: grab one webcam frame, classify costume, print top-3, exit",
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=None,
        metavar="N",
        help="OpenCV camera index for --vision / --snap (default 0)",
    )
    args = parser.parse_args(argv)

    if args.list_devices:
        print(list_devices())
        return 0

    if args.input is not None or args.output is not None:
        try:
            set_devices(args.input, args.output)
        except OSError as exc:
            print(f"Could not set audio devices ({exc})")
            if not args.dry_run and not args.snap:
                return 1

    cfg = load_config(args.config)
    if args.continuous and args.dry_run:
        print("Refusing: --continuous cannot be used with --dry-run.")
        return 2
    if args.continuous:
        cfg["mode"] = "vad"
    elif args.mode:
        cfg["mode"] = args.mode
    if args.model:
        cfg["ollama"]["model"] = args.model
    if args.stt_model:
        cfg["stt"]["model"] = args.stt_model
    cfg["_input_device"] = args.input
    cfg["_output_device"] = args.output
    cfg.setdefault("vision", {})
    if args.vision or args.snap:
        cfg["vision"]["enabled"] = True
    if args.camera is not None:
        cfg["vision"]["camera_index"] = args.camera

    if cfg["privacy"].get("save_audio") or cfg["privacy"].get("save_transcripts"):
        print("Refusing to start: save_audio / save_transcripts must stay false.")
        return 2

    if args.snap:
        return _kitchen_still(cfg, dry_run=args.dry_run)

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

    sidecar = None
    if (cfg.get("vision") or {}).get("enabled"):
        print("  loading vision…")
        sidecar = VisionSidecar.from_config(cfg)
        ok, reason = sidecar.prepare()
        if not ok:
            print(f"  vision skipped: {reason}")
            sidecar.close()
            sidecar = None
        else:
            print(
                f"  vision load={sidecar.load_ms:.0f}ms  "
                f"CLIP={sidecar.model_name}  camera={sidecar.camera_index}"
            )
            print(f"  {PRIVACY_SIGN}")

    history: list[dict] = []
    print()
    print("Disembodied test loop. Kids hear a voice with no pumpkin yet — that is the point.")
    if args.continuous:
        pass  # continuous banner prints just before the loop
    else:
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

    if args.continuous:
        print("Continuous porch mode. Speak, pause, he answers — then listens again.")
        print("  Ctrl+C = quit")
        print()
        try:
            while True:
                metrics = TurnMetrics()
                vis_future = _kick_vision(sidecar)
                audio_in, metrics.record_ms = record_vad(
                    cfg["sample_rate"],
                    cfg["listen_limit_s"],
                    cfg["silence_s"],
                    cfg["energy_threshold"],
                    input_device=cfg.get("_input_device"),
                )
                user_text, metrics.stt_ms = stt.transcribe(audio_in, cfg["sample_rate"])
                del audio_in
                user_text = (user_text or "").strip()
                if not user_text:
                    # Don't stamp empty air all night
                    print("  (silence — still listening)")
                    time.sleep(float(cfg["cooldown_s"]))
                    continue
                _handle_turn(
                    user_text,
                    mayor,
                    speaker,
                    canned,
                    history,
                    cfg,
                    metrics,
                    typed=False,
                    vis_future=vis_future,
                )
                time.sleep(float(cfg["cooldown_s"]))
        except KeyboardInterrupt:
            print()
            line = random.choice(canned["goodnight"])
            print(f"Mayor: {line}")
            if speaker is not None:
                samples, rate, _, _ = speaker.synthesize(line)
                play(samples, rate, cfg.get("_output_device"))
                del samples
        return 0

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
            vis_future = _kick_vision(sidecar)
            _handle_turn(
                user_text,
                mayor,
                speaker,
                canned,
                history,
                cfg,
                metrics,
                typed=args.dry_run,
                vis_future=vis_future,
            )
            continue

        metrics = TurnMetrics()
        vis_future = _kick_vision(sidecar)
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

        _handle_turn(
            user_text,
            mayor,
            speaker,
            canned,
            history,
            cfg,
            metrics,
            typed=args.dry_run,
            vis_future=vis_future,
        )
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


def _handle_turn(
    user_text, mayor, speaker, canned, history, cfg, metrics, typed=False, vis_future=None
):
    user_text = (user_text or "").strip()
    metrics.words_in = len(user_text.split())
    print(f"Heard: {user_text!r}" if user_text else "Heard: (silence)")

    gesture = "stamp"
    line = ""
    t_post_stt = perf_counter()
    vis = take_ready(vis_future)
    visual_note = vis.note if vis is not None and vis.ok else None

    if looks_distress(user_text):
        line = canned["distress"][0]
        gesture = "listen"
        metrics.used_canned = True
    elif looks_tease(user_text) and canned.get("tease"):
        # Dry bureaucratic clapback — never roast the child (littles may be imitating).
        line = random.choice(canned["tease"])
        gesture = random.choice(["stamp", "bow", "think", "laugh"])
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
        for piece, piece_ttft, done in mayor.reply_stream(
            user_text, history, visual_note=visual_note
        ):
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
        metrics.vision_used = bool(visual_note)
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

    if vis is None:
        vis = take_ready(vis_future)
    if vis is not None:
        metrics.vision_ms = vis.vision_ms
        metrics.vision_label = vis.label if not vis.skip_reason else ""
        metrics.vision_score = vis.score
        if vis.skip_reason:
            print(f"  vision skipped: {vis.skip_reason}")
        elif vis.top3:
            print(f"  vision top: {format_top3(vis.top3)}")
            if metrics.vision_used and visual_note:
                print(f"  {visual_note}")
            elif vis.note and not metrics.used_canned:
                print("  vision ready too late; note not used this turn")

    print(metrics.render())
    print()



if __name__ == "__main__":
    sys.exit(main())
