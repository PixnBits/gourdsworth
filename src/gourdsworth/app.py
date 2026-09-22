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
from gourdsworth.net import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    CrateListener,
    is_loopback,
    resolve_bind,
    serve_echo,
)
from gourdsworth.guardrails import (
    looks_debug_pass,
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
    VISION_BLIND_NOTE,
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
        f"vision_used=0" + (f"  persons={result.person_count}" if result.person_count is not None else "")
    )
    print(result.note)
    if dry_run:
        print("  (dry-run: note not sent to the mayor)")
    return 0


def _setup_run_log(path: str | None) -> None:
    """Tee stdout/stderr so porch runs can be pulled without copy-paste."""
    if not path:
        return
    from pathlib import Path as _P
    import sys as _sys
    import time as _time

    log_path = _P(path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "a", buffering=1, encoding="utf-8")
    fh.write(f"\n==== crate-desktop start {_time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    fh.flush()

    class _Tee:
        def __init__(self, stream, fileh):
            self._stream = stream
            self._fh = fileh

        def write(self, data):
            self._stream.write(data)
            self._fh.write(data)
            self._fh.flush()
            return len(data)

        def flush(self):
            self._stream.flush()
            self._fh.flush()

        def fileno(self):
            return self._stream.fileno()

        def isatty(self):
            return False

    _sys.stdout = _Tee(_sys.stdout, fh)  # type: ignore[assignment]
    _sys.stderr = _Tee(_sys.stderr, fh)  # type: ignore[assignment]
    print(f"  logging to {log_path}")



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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Tee stdout/stderr to a file (default logs/crate-desktop.log with --serve-crate)",
    )
    parser.add_argument(
        "--serve-crate",
        action="store_true",
        help="Accept one Pi crate client; run the turn loop on remote PCM (default bind 127.0.0.1)",
    )
    parser.add_argument(
        "--crate-echo",
        action="store_true",
        help="Protocol smoke: echo crate PCM as play + send a gesture; no models",
    )
    parser.add_argument(
        "--crate-host",
        default=None,
        help="Override crate.host bind address",
    )
    parser.add_argument(
        "--crate-port",
        type=int,
        default=None,
        metavar="N",
        help="Override crate.port",
    )
    args = parser.parse_args(argv)
    if args.log_file is None and getattr(args, "serve_crate", False):
        from gourdsworth.config import ROOT
        args.log_file = str(ROOT / "logs" / "crate-desktop.log")
    if getattr(args, "log_file", None):
        _setup_run_log(args.log_file)


    if args.list_devices:
        print(list_devices())
        return 0

    if args.crate_echo:
        return _run_crate_echo(args)

    if args.input is not None or args.output is not None:
        if args.serve_crate:
            print("  (crate mode: desktop --input/--output unused; I/O is on the Pi)")
        else:
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
    if args.serve_crate and args.dry_run:
        print(
            "Refusing: --serve-crate cannot be used with --dry-run. "
            "Use --crate-echo for protocol smoke without models."
        )
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
    cfg.setdefault("crate", {})
    if args.serve_crate:
        cfg["crate"]["enabled"] = True
        # Crate continuous clients stream until silence; PTT waits the full listen_limit.
        if args.mode is None:
            cfg["mode"] = "vad"
    if args.crate_host:
        cfg["crate"]["host"] = args.crate_host
    if args.crate_port is not None:
        cfg["crate"]["port"] = args.crate_port

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

    if (cfg.get("crate") or {}).get("enabled"):
        return _serve_crate(cfg, mayor, stt, speaker, sidecar, canned)

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
                # VAD always closes on silence after speech (or times out empty).
                metrics.post_speech_silence_ms = float(cfg["silence_s"]) * 1000.0
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
    user_text,
    mayor,
    speaker,
    canned,
    history,
    cfg,
    metrics,
    typed=False,
    vis_future=None,
    play_fn=None,
    gesture_fn=None,
):
    user_text = (user_text or "").strip()
    metrics.words_in = len(user_text.split())
    print(f"Heard: {user_text!r}" if user_text else "Heard: (silence)")

    def _play(samples, rate) -> float:
        if play_fn is not None:
            return float(play_fn(samples, rate) or 0.0)
        return play(samples, rate, cfg.get("_output_device"))

    gesture = "stamp"
    line = ""
    t_post_stt = perf_counter()
    vis = take_ready(vis_future)
    if vis is not None and vis.ok and vis.note:
        visual_note = vis.note
    else:
        # No still / soft CLIP — ask, don't invent a costume.
        visual_note = VISION_BLIND_NOTE

    if looks_debug_pass(user_text):
        # Secret porch diagnostics (Phineas and Ferb passcodes).
        bits = ["Agent P debug channel open."]
        if vis is not None and vis.ok and vis.note:
            bits.append(f"Vision says {vis.label or 'unknown'} at {vis.score:.2f}.")
            if getattr(vis, "top3", None):
                tops = ", ".join(f"{n} {s:.2f}" for n, s in vis.top3[:3])
                bits.append(f"Top guesses: {tops}.")
        elif vis is not None and getattr(vis, "skip_reason", None):
            bits.append(f"Vision skipped: {vis.skip_reason}.")
        else:
            bits.append("No still ready yet. Camera may still be grabbing.")
        if metrics.uplink_first_ms:
            bits.append(f"Uplink first {metrics.uplink_first_ms:.0f} milliseconds.")
        line = " ".join(bits)
        gesture = "think"
        metrics.used_canned = True
        metrics.vision_used = bool(vis is not None and vis.ok and vis.note)
    elif looks_distress(user_text):
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
                        metrics.play_ms += _play(samples, rate)
                    del samples
            if done:
                break
        metrics.llm_total_ms = (perf_counter() - t_llm0) * 1000
        metrics.vision_used = bool(vis is not None and vis.ok and vis.note)
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
                metrics.play_ms += _play(samples, rate)
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
    if gesture_fn is not None:
        try:
            gesture_fn(gesture)
        except (ConnectionError, OSError) as exc:
            print(f"  crate gesture send failed: {exc}")

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
            metrics.play_ms = _play(samples, rate)
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
        metrics.person_count = vis.person_count
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


def _crate_settings(cfg: dict) -> tuple[str, int]:
    crate = cfg.get("crate") or {}
    host = resolve_bind(crate.get("host") or DEFAULT_HOST, bool(crate.get("allow_lan")))
    port = int(crate.get("port") or DEFAULT_PORT)
    return host, port


def _run_crate_echo(args) -> int:
    cfg = load_config(args.config)
    if cfg["privacy"].get("save_audio") or cfg["privacy"].get("save_transcripts"):
        print("Refusing to start: save_audio / save_transcripts must stay false.")
        return 2
    crate = cfg.setdefault("crate", {})
    if args.crate_host:
        crate["host"] = args.crate_host
    if args.crate_port is not None:
        crate["port"] = args.crate_port
    try:
        host, port = _crate_settings(cfg)
    except ValueError as exc:
        print(exc)
        return 2
    print("Gourdsworth crate echo — no models, RAM only, nothing written")
    if is_loopback(host):
        print(f"  bind {host}:{port}  (localhost; children's PCM never leaves this machine)")
    else:
        print(f"  bind {host}:{port}  WARNING: LAN bind. PCM/JPEG reachable on this network. No TLS.")
    print("  Ctrl+C to quit")
    try:
        serve_echo(host, port)
    except KeyboardInterrupt:
        print()
    return 0


def _serve_crate(cfg, mayor, stt, speaker, sidecar, canned) -> int:
    if stt is None:
        print(
            "STT is required for --serve-crate "
            "(use --crate-echo to test the protocol without models)."
        )
        return 1
    try:
        host, port = _crate_settings(cfg)
    except ValueError as exc:
        print(exc)
        return 2
    print()
    print("Crate server. Inference stays here. The Pi is mic / speaker / camera / button only.")
    print(
        f"  listen mode={cfg.get('mode')}  "
        f"(vad ends on pause; ptt waits for button-up / full listen_limit)"
    )
    if is_loopback(host):
        print(f"  crate listening on {host}:{port}  (localhost only)")
    else:
        print(f"  crate listening on {host}:{port}  WARNING: LAN bind, no TLS, no auth")
    if sidecar is not None:
        print("  vision: JPEG stills from the crate (desktop camera unused)")
    print("  Talk is the Pi button / Enter. Ctrl+C to quit.")
    print()
    listener = CrateListener(host, port)
    history: list[dict] = []
    try:
        while True:
            print("waiting for crate client…")
            conn = listener.accept()
            peer = conn.peer or ("?", 0)
            print(f"crate connected {peer[0]}:{peer[1]}")
            try:
                _run_crate_session(
                    conn, cfg, mayor, stt, speaker, sidecar, canned, history
                )
            except (ConnectionError, OSError) as exc:
                print(f"crate session ended: {exc}")
            finally:
                conn.close()
                print("crate disconnected")
    except KeyboardInterrupt:
        print()
        line = random.choice(canned["goodnight"])
        print(f"Mayor: {line}")
    finally:
        listener.close()
    return 0


def _run_crate_session(conn, cfg, mayor, stt, speaker, sidecar, canned, history) -> None:
    hello = conn.handshake("desktop", timeout=10.0)
    if hello is None:
        raise ConnectionError("crate hello timeout")
    continuous = bool(hello.get("continuous"))
    opener = random.choice(canned["opener"])
    print(f"Mayor: {opener}")
    if continuous:
        print("  continuous crate: mic stays open except while SPEAKING")
    if speaker is not None:
        samples, rate, _, _ = speaker.synthesize(opener)
        print("SPEAKING")
        conn.send({"event": "speaking"})
        conn.play_float(samples, rate)
        del samples
    conn.send({"event": "ready"})
    print("ready.")

    armed = False
    vis_future = None  # in-flight or unused CLIP future (carried across turns)
    pending_jpeg: bytes | None = None  # newer still waiting for a free classify slot
    while not conn.closed:
        if continuous:
            if not armed:
                # Wait for Pi to open the always-on uplink once
                if not conn.wait_button("down", timeout=0.5):
                    continue
                armed = True
            else:
                conn.arm_listen()
        else:
            if not conn.wait_button("down", timeout=0.5):
                continue

        metrics = TurnMetrics()
        print("LISTENING")
        conn.send({"event": "listen"})
        (
            audio_in,
            metrics.record_ms,
            metrics.uplink_first_ms,
            metrics.uplink_jitter_ms,
            metrics.had_voice,
        ) = conn.listen_pcm(
            sample_rate=int(cfg["sample_rate"]),
            limit_s=float(cfg["listen_limit_s"]),
            mode=str(cfg.get("mode") or "vad"),
            silence_s=float(cfg["silence_s"]),
            energy_threshold=float(cfg["energy_threshold"]),
            min_voiced_s=0.35,
            keep_listening=continuous,
        )
        if str(cfg.get("mode") or "vad") == "vad" and metrics.had_voice:
            metrics.post_speech_silence_ms = float(cfg["silence_s"]) * 1000.0

        # Vision never blocks first audio: grab whatever JPEG is already here,
        # kick CLIP in parallel with STT, carry unused/late results to the next turn.
        def _ingest_jpeg(tag: str) -> None:
            nonlocal vis_future, pending_jpeg
            if sidecar is None:
                return
            jpeg = conn.take_jpeg()
            if jpeg is None:
                return
            print(f"  jpeg {tag} {len(jpeg) // 1024}KiB")
            if vis_future is None or vis_future.done():
                kicked = sidecar.submit_jpeg(jpeg)
                if kicked is not None:
                    vis_future = kicked
                    pending_jpeg = None
                    print("  vision classify started")
                else:
                    pending_jpeg = jpeg
            else:
                # Newer frame supersedes; apply when in-flight CLIP finishes
                pending_jpeg = jpeg
                print("  vision busy — still queued for next slot")
            del jpeg

        def _promote_pending() -> None:
            nonlocal vis_future, pending_jpeg
            if pending_jpeg is None or sidecar is None:
                return
            if vis_future is not None and not vis_future.done():
                return
            kicked = sidecar.submit_jpeg(pending_jpeg)
            if kicked is not None:
                vis_future = kicked
                print(f"  vision classify started (carried still {len(pending_jpeg) // 1024}KiB)")
                pending_jpeg = None

        _promote_pending()
        if metrics.had_voice:
            _ingest_jpeg("at-speech-end")

        def _rearm_quiet() -> None:
            """Stay listening — do not send ready (that ducks the Pi mic)."""
            _ingest_jpeg("idle")
            _promote_pending()
            if continuous:
                pass
            else:
                conn.reset_talk_latch()
                conn.send({"event": "ready"})
                print("ready.")
                time.sleep(float(cfg["cooldown_s"]))

        if not metrics.had_voice:
            print("  (still listening)")
            del audio_in
            _rearm_quiet()
            continue

        # STT starts immediately — CLIP may still be running
        user_text, metrics.stt_ms = stt.transcribe(audio_in, cfg["sample_rate"])
        del audio_in
        user_text = (user_text or "").strip()
        _ingest_jpeg("during-stt")
        _promote_pending()
        if not user_text:
            print("Heard: (silence)")
            print("  (still listening)")
            _rearm_quiet()
            continue

        print("THINKING")
        conn.send({"event": "thinking"})
        spoke = {"n": 0}

        def play_fn(samples, rate, _spoke=spoke, _conn=conn):
            if _spoke["n"] == 0:
                print("SPEAKING")
                _conn.send({"event": "speaking"})
            _spoke["n"] += 1
            return _conn.play_float(samples, rate)

        def gesture_fn(name, _conn=conn):
            _conn.send({"event": "gesture", "name": name})

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
            play_fn=play_fn,
            gesture_fn=gesture_fn,
        )
        # If this turn used the note, clear the future so a carried still can run next.
        # If unused (CLIP late or skipped), keep vis_future for the next utterance.
        if metrics.vision_used:
            print("  vision consumed this turn")
            vis_future = None
        elif vis_future is not None and vis_future.done():
            print("  vision ready but unused — carrying to next speech")
        elif vis_future is not None:
            print("  vision still running — carrying to next speech")
        _ingest_jpeg("after-turn")
        _promote_pending()
        # After Mayor speaks, ready unmutes the Pi mic.
        if continuous:
            conn.arm_listen()
        else:
            conn.reset_talk_latch()
        conn.send({"event": "ready"})
        print("ready.")
        time.sleep(float(cfg["cooldown_s"]))


if __name__ == "__main__":
    sys.exit(main())
