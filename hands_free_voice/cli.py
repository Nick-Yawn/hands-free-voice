"""The `hands-free-voice` command."""

import argparse
import asyncio
import atexit
import os
import signal
import sys

from hands_free_voice import __version__
from hands_free_voice.app import Host, run_text
from hands_free_voice.config import child_env, load_config
from hands_free_voice.contract import write_contract
from hands_free_voice.providers.registry import ConfigError, make_stt, make_tts, missing_keys
from hands_free_voice.seat import Seat
from hands_free_voice.spoken_log import SpokenLog
from hands_free_voice.state import EventLog, LockFile, SessionPin, kill_group, state_dir


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hands-free-voice",
        description="A voice interface for Claude Code. Run it in a project"
                    " folder; say the address word, talk, say the closer.",
        epilog="Anything after '--' is passed to claude, e.g."
               " hands-free-voice -- --permission-mode acceptEdits. 'hands-free-voice"
               " listen' is the audio half of the Claude Code mod, which starts it.")
    ap.add_argument("--text", action="store_true",
                    help="type instead of talk: no audio, no speech keys needed")
    ap.add_argument("--project", default=None, metavar="DIR",
                    help="the project folder (default: the current directory)")
    ap.add_argument("--new", action="store_true",
                    help="start a fresh claude session instead of resuming the pinned one")
    ap.add_argument("--resume", default=None, metavar="SESSION_ID",
                    help="pin and resume this claude session id")
    ap.add_argument("--voice", default=None, metavar="VOICE_ID",
                    help="the TTS voice id (overrides [tts].voice in the config)")
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="config file (default: ~/.config/hands-free-voice/config.toml)")
    ap.add_argument("--claude", default=None, metavar="BIN",
                    help="the claude binary to drive (default: claude on PATH)")
    ap.add_argument("--version", action="version", version=f"hands-free-voice {__version__}")
    return ap


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


NO_PERMISSION_MODE_WARNING = (
    "hands-free-voice: no permission mode set: Claude can't run tools that need"
    " approval; set [seat] claude_args (see README)")


def missing_permission_mode(claude_args: list[str]) -> bool:
    """True unless --permission-mode (space- or '='-joined) is among the
    args claude will be spawned with. Left unset, -p mode denies every
    tool call that needs approval, so a first voice request silently
    fails."""
    return not any(a == "--permission-mode" or a.startswith("--permission-mode=")
                  for a in claude_args)


def handle_stray(lock: LockFile, ask=None) -> None:
    """A child claude left by a host that is gone: offer to kill it."""
    stray = lock.stray_child()
    if not stray:
        return
    prompt = ask or (lambda q: sys.stdin.isatty() and input(q).strip().lower() in ("y", "yes"))
    print(f"[a claude child from a previous hands-free-voice (pid {stray}) is still running]")
    if prompt(f"Kill it? [y/N] "):
        kill_group(stray)
        print(f"[killed pid {stray}]")
    else:
        print("[left running; it will keep the old session busy]")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["listen"]:
        from hands_free_voice.listen import main as listen_main
        return listen_main(argv[1:])
    own, passthrough = split_passthrough(argv)
    args = build_parser().parse_args(own)

    project_dir = os.path.abspath(args.project or os.getcwd())
    if not os.path.isdir(project_dir):
        print(f"hands-free-voice: not a directory: {project_dir}", file=sys.stderr)
        return 2
    cfg = load_config(project_dir, user_path=args.config)
    if args.voice:
        cfg["tts"]["voice"] = args.voice
    if args.claude:
        cfg["seat"]["claude"] = args.claude
    claude_args = list(cfg["seat"].get("claude_args") or []) + passthrough
    if missing_permission_mode(claude_args):
        print(NO_PERMISSION_MODE_WARNING, file=sys.stderr)

    sdir = state_dir(project_dir)
    log = EventLog(sdir / "log.jsonl")
    pin = SessionPin(sdir / "session_id")
    if args.new:
        pin.clear()
    if args.resume:
        pin.save(args.resume)
    lock = LockFile(sdir / "lock")
    holder = lock.holder()
    if holder:
        print(f"hands-free-voice: another hands-free-voice (pid {holder}) holds this project's"
              f" session; quit it first", file=sys.stderr)
        return 3
    handle_stray(lock)
    lock.acquire(pin.load())
    atexit.register(lock.release)
    contract = write_contract(sdir)

    host = Host(cfg, project_dir, log=log)
    seat = Seat(project_dir, contract_path=contract, session_pin=pin,
                on_event=host.on_seat_event, env=child_env(),
                claude=cfg["seat"]["claude"], claude_args=claude_args,
                lock=lock, log=log,
                idle_close_s=float(cfg["seat"]["idle_close_min"]) * 60.0,
                closer_fallback=cfg["seat"]["closer_fallback"])
    atexit.register(seat.kill_now)
    log.write("start", mode="text" if args.text else "voice", project=project_dir,
              session=pin.load(), argv=argv)

    if args.text:
        async def speak_nothing(text, register):
            await asyncio.sleep(0)
        host.bind(seat, SpokenLog(speak_nothing))
        runner = run_text(host)
    else:
        from hands_free_voice.voice import run_voice
        try:
            missing = missing_keys(cfg)
            if missing:
                print(f"hands-free-voice: voice mode needs {' and '.join(missing)} in the"
                      f" environment (or run with --text)", file=sys.stderr)
                return 2
            stt, tts = make_stt(cfg), make_tts(cfg)
        except ConfigError as exc:
            print(f"hands-free-voice: {exc}", file=sys.stderr)
            return 2
        runner = run_voice(host, seat, cfg, stt=stt, tts=tts)

    async def run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, host.quitting.set)
            except (NotImplementedError, RuntimeError):
                pass
        await runner

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()
        seat.kill_now()
    finally:
        lock.release()
    return 0
