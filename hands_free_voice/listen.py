"""`hands-free-voice listen`: the audio half of the hands-free-voice mod.

The mod starts this process and reads its stdout; this process owns the
mic, the speech link, the turn machine, the SpokenLog and the speaker.

  stdout   one JSON object per line, for the mod:
             hello    socket, contract, address, closer, commands   (first line)
             turn     text     a heard turn, for Claude Code
             compact           compact the session
             state    state    idle, hearing (words), speaking, paused or
                               trouble (detail), for the status line
             log      text     a line of the transcript
             error    text     why it could not start (then it exits)
  socket   HTTP on a Unix socket named in hello, from the mod:
             POST /event   one event for the ModSeat (bridge.py)
             POST /quit    stop listening
             GET  /status  the host's status snapshot

Nothing else is written to stdout: stray output would break the mod's
reading of it, so diagnostics go to stderr.
"""

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from hands_free_voice import __version__
from hands_free_voice.app import Host
from hands_free_voice.bridge import ModSeat
from hands_free_voice.config import load_config
from hands_free_voice.contract import contract_text
from hands_free_voice.providers.registry import (ConfigError, make_stt, make_tts, missing_keys,
                                                 required_key_envs)
from hands_free_voice.state import (DEFAULT_STATE_DIR, STATE_DIR_ENV, EventLog, LockFile,
                                    state_dir)
from hands_free_voice.turns import SHOWN_COMMANDS

SOCKET_NAME = "listen.sock"
MAX_BODY = 1 << 20
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 413: "Payload Too Large",
           500: "Internal Server Error"}


class JsonLines:
    """write(obj): one JSON line on `stream` (stdout), flushed at once.
    Once the mod stops reading (the pipe broke), `on_broken` fires once
    and later writes are dropped."""

    def __init__(self, stream=None):
        self.stream = stream
        self.on_broken = None
        self.broken = False

    def __call__(self, obj: dict) -> None:
        if self.broken:
            return
        out = self.stream or sys.stdout
        try:
            out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            out.flush()
        except (BrokenPipeError, ValueError):  # ValueError: the stream was closed
            self.broken = True
            if self.on_broken:
                self.on_broken()


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, bytes]:
    """(method, path, body) of one HTTP/1.1 request; chunked bodies too."""
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = b""
        while True:
            size = int((await reader.readuntil(b"\r\n")).split(b";")[0], 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                break
            body += await reader.readexactly(size)
            await reader.readexactly(2)
            if len(body) > MAX_BODY:
                raise ValueError("body too large")
    else:
        n = int(headers.get("content-length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        body = await reader.readexactly(n) if n else b""
    return method.upper(), path.split("?", 1)[0], body


async def serve(sock_path: str, route):
    """An HTTP server on a Unix socket; `route(method, path, body)` ->
    (status, JSON-able payload). One request per connection."""

    async def handle(reader, writer):
        try:
            method, path, body = await read_request(reader)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        except ValueError as exc:
            method = path = None
            status, payload = (413 if "too large" in str(exc) else 400), {"error": str(exc)}
        if method is not None:
            try:
                status, payload = route(method, path, body)
            except ValueError as exc:
                status, payload = 400, {"error": str(exc)}
            except Exception as exc:
                status, payload = 500, {"error": repr(exc)}
        data = json.dumps(payload).encode("utf-8")
        writer.write(f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\n"
                     f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode("latin-1") + data)
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=sock_path)


def make_route(host: Host, seat: ModSeat):
    def route(method: str, path: str, body: bytes):
        if method == "POST" and path == "/event":
            ev = json.loads(body or b"{}")
            if not isinstance(ev, dict):
                raise ValueError("an event is a JSON object")
            seat.event(ev)
            return 200, {"ok": True}
        if method == "POST" and path == "/quit":
            host.quitting.set()
            return 200, {"ok": True}
        if method == "GET" and path == "/status":
            return 200, host.status_snapshot()
        return 404, {"error": f"no route {method} {path}"}
    return route


async def run_listen(cfg: dict, project_dir: str, *, stt, tts, write, sock_dir: str,
                     log: EventLog | None = None, playback=None, mic=None,
                     vad=None) -> int | None:
    """The voice front over a ModSeat, with the socket the mod posts to.
    Returns the signal that ended it, or None for a quit (spoken, or the
    mod's POST /quit): the mod restarts a listener a signal ended, since
    a hot reload ends it that way."""
    from hands_free_voice.voice import run_voice

    def out(text: str) -> None:
        write({"type": "log", "text": text})

    host = Host(cfg, project_dir, log=log, out=out)
    if isinstance(write, JsonLines):
        write.on_broken = host.quitting.set  # the mod is gone: stop listening
    seat = ModSeat(write, host.on_seat_event,
                   closer_fallback=cfg["seat"]["closer_fallback"])
    sock_path = os.path.join(sock_dir, SOCKET_NAME)
    server = await serve(sock_path, make_route(host, seat))
    loop = asyncio.get_running_loop()
    ended_by: list[int] = []

    def on_signal(sig: int) -> None:
        ended_by.append(sig)
        host.quitting.set()

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, on_signal, sig)
    write({"type": "hello", "socket": sock_path, "contract": contract_text(),
           "address": cfg["words"]["address"], "closer": cfg["words"]["closer"],
           "commands": list(SHOWN_COMMANDS), "version": __version__})
    try:
        await run_voice(host, seat, cfg, stt=stt, tts=tts, playback=playback, mic=mic, vad=vad,
                        on_state=lambda state: write({"type": "state", **state}))
    finally:
        server.close()
        await server.wait_closed()
    return ended_by[0] if ended_by else None


KEYS_FILE = Path("~/.config/hands-free-voice/.env")


def read_env_file(path) -> dict:
    """NAME=value lines; `export `, quotes, blank lines and # comments allowed."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep or line.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[name.strip()] = value
    return out


def with_file_keys(names, environ=None, path=KEYS_FILE) -> dict:
    """The environment, with each named key it lacks taken from the keys
    file: the mod's options and the environment win."""
    env = dict(os.environ if environ is None else environ)
    found = read_env_file(path)
    for name in names:
        if not env.get(name) and found.get(name):
            env[name] = found[name]
    return env


LOCK_WAIT_S = 5.0  # a listener being replaced (the mod reloaded) is still closing


def wait_for_lock(lock: LockFile, wait_s: float = LOCK_WAIT_S, *,
                  sleep=time.sleep, clock=time.monotonic) -> int | None:
    """The pid still holding the lock after up to wait_s, or None once it is
    free: a listener the mod just stopped takes a moment to close."""
    deadline = clock() + wait_s
    while (holder := lock.holder()) and clock() < deadline:
        sleep(0.1)
    return holder


def listen_lock() -> LockFile:
    """One listener per machine: two open mics would both hear the
    address word and both send the turn."""
    root = Path(os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return LockFile(root / "listen.lock")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="hands-free-voice listen",
        description="The audio half of the hands-free-voice Claude Code mod; the mod"
                    " starts it. It writes JSON lines on stdout and serves the mod on"
                    " a Unix socket.")
    ap.add_argument("--project", default=None, metavar="DIR",
                    help="the session's folder, for its .hands-free-voice.toml"
                         " (default: the current directory)")
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="config file (default: ~/.config/hands-free-voice/config.toml)")
    ap.add_argument("--voice", default=None, metavar="VOICE_ID",
                    help="the TTS voice id (overrides [tts].voice in the config)")
    # the mod passes its options for the working cue (its /config rows) as these
    ap.add_argument("--working-cue", choices=("tone", "word", "off"), default=None,
                    help="what plays while Claude works without a word: a quiet tone, the"
                         " spinner's word spoken, or nothing (overrides [working].cue)")
    ap.add_argument("--working-interval", type=float, default=None, metavar="SECONDS",
                    help="quiet before the working cue, and between cues"
                         " (overrides [working].interval_s)")
    ap.add_argument("--working-tone-volume", type=float, default=None, metavar="SHARE",
                    help="the working tone's level as a share of the other tones', 0.1 for"
                         " 10 percent (overrides [working].tone_volume)")
    args = ap.parse_args(argv)
    write = JsonLines()

    def fail(text: str, code: int = 2) -> int:
        write({"type": "error", "text": text})
        return code

    project_dir = os.path.abspath(args.project or os.getcwd())
    cfg = load_config(project_dir if os.path.isdir(project_dir) else None,
                      user_path=args.config)
    if args.voice:
        cfg["tts"]["voice"] = args.voice
    for flag, key in ((args.working_cue, "cue"), (args.working_interval, "interval_s"),
                      (args.working_tone_volume, "tone_volume")):
        if flag is not None:
            cfg["working"][key] = flag
    if cfg["working"]["cue"] not in ("tone", "word", "off"):
        return fail(f"the working cue is tone, word or off, not {cfg['working']['cue']!r}")
    if not float(cfg["working"]["interval_s"]) > 0:
        return fail("the working cue's interval must be more than 0 seconds")
    if not float(cfg["working"]["tone_volume"]) >= 0:
        return fail("the working tone's volume can't be below 0")
    try:
        environ = with_file_keys(required_key_envs(cfg))
        missing = missing_keys(cfg, environ)
        if missing:
            them = "it" if len(missing) == 1 else "them"
            return fail(f"voice needs {' and '.join(missing)}: set {them} in the mod's"
                        f" options or {KEYS_FILE}")
        stt, tts = make_stt(cfg, environ), make_tts(cfg, environ)
    except ConfigError as exc:
        return fail(str(exc))

    lock = listen_lock()
    holder = wait_for_lock(lock)
    if holder:
        return fail(f"another session is already listening (pid {holder})", 3)
    lock.acquire(None)
    sock_dir = tempfile.mkdtemp(prefix="hfv-")
    log = EventLog(state_dir(project_dir) / "listen.jsonl")
    log.write("start", mode="listen", project=project_dir)
    try:
        ended_by = asyncio.run(run_listen(cfg, project_dir, stt=stt, tts=tts, write=write,
                                          sock_dir=sock_dir, log=log))
    finally:
        lock.release()
        shutil.rmtree(sock_dir, ignore_errors=True)
    return 128 + ended_by if ended_by else 0  # the shell's convention: 143 for SIGTERM
