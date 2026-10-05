"""The Seat: one persistent `claude -p` child driven over stream-json.

  claude -p [--resume <sid>] --input-format stream-json
            --output-format stream-json --verbose --replay-user-messages
            --append-system-prompt-file <contract>

The child is spawned on the first message and resumed from the session
id pinned on disk. Each message goes to its stdin the moment it is
heard; a message written while a query runs is injected at the query's
next tool boundary, like typing into the interactive CLI. The child
closes after IDLE_CLOSE_S with nothing to do and respawns on the next
message. Its stdout is read by one task per process run, each line
handed to that run's own Translator, and every event goes to `on_event`
in stream order.

Orphans (design §9.1): the child runs in its own process group. Every
exit path closes stdin, then sends SIGTERM and SIGKILL to the whole
group; kill_now() is the synchronous form for atexit and signal
handlers. The lock file records the child pid so a later start can find
a stray.
"""

import asyncio
import collections
import os
import signal
import sys
import time

from hands_free_voice.translator import Translator

SEAT_FLAGS = ["--input-format", "stream-json", "--output-format",
              "stream-json", "--verbose", "--replay-user-messages"]

# close(): stdin EOF lets the CLI finish its own background-task grace
# and exit; past this it gets SIGTERM, then SIGKILL, to its whole group.
CLOSE_GRACE_S = 10.0
TERM_GRACE_S = 5.0

IDLE_CLOSE_S = 30 * 60.0
SWEEP_INTERVAL_S = 30.0

STDERR_TAIL_LINES = 40


def build_seat_command(session_id: str | None, contract_path: str | os.PathLike | None,
                       extra_args=(), claude: str = "claude") -> list[str]:
    cmd = [claude, "-p"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += SEAT_FLAGS
    if contract_path:
        cmd += ["--append-system-prompt-file", str(contract_path)]
    cmd += list(extra_args)
    return cmd


def user_message_line(text: str) -> str:
    import json
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


async def _spawn_claude(cmd: list[str], cwd: str, env: dict | None):
    return await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=16 * 1024 * 1024)


def _signal_group(proc, sig) -> None:
    """Signal the child's whole process group; a fake without a pid gets
    its own kill(). Never raises."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        try:
            proc.kill()
        except Exception:
            pass
        return
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


class Seat:
    def __init__(self, project_dir: str, *, contract_path=None, session_pin=None,
                 on_event, spawn=None, clock=time.monotonic, env=None,
                 claude: str = "claude", claude_args=(), lock=None, log=None,
                 idle_close_s: float = IDLE_CLOSE_S,
                 sweep_interval_s: float = SWEEP_INTERVAL_S,
                 closer_fallback: str = "Done."):
        self.project_dir = project_dir
        self.contract_path = contract_path
        self.pin = session_pin
        self.session_id: str | None = session_pin.load() if session_pin else None
        self._on_event = on_event
        self._spawn = spawn or _spawn_claude
        self._clock = clock
        self._env = env
        self._claude = claude
        self._claude_args = list(claude_args)
        self._lock = lock
        self._log = log
        self.idle_close_s = idle_close_s
        self.sweep_interval_s = sweep_interval_s
        self._closer_fallback = closer_fallback
        self.state = Translator(clock, closer_fallback)
        self._proc = None
        self._reader = None
        self._stderr_reader = None
        self._sweeper = None
        self._closings: set = set()
        self._stderr_tail: collections.deque = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._write_lock = asyncio.Lock()
        self.spawns = 0

    # -- state -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def busy(self) -> bool:
        return self.state.busy

    @property
    def unconsumed(self) -> int:
        return self.state.unconsumed

    @property
    def occupied(self) -> bool:
        """A query running, or a message written and not yet read."""
        return bool(self.state.busy or self.state.unconsumed)

    @property
    def compact_pending(self) -> bool:
        return self.state.compact_pending

    @property
    def last_pct(self) -> int | None:
        return self.state.last_pct

    @property
    def child_pid(self) -> int | None:
        return getattr(self._proc, "pid", None) if self.alive else None

    # -- writing -------------------------------------------------------------

    async def submit(self, text: str) -> None:
        """Write one message, spawning first if no live child. Returns
        once the words are in; the answer streams through the reader. A
        write failure raises before anything is counted."""
        async with self._write_lock:
            if not self.alive:
                await self._start()
            self._proc.stdin.write(user_message_line(text).encode("utf-8"))
            await self._proc.stdin.drain()
            self.state.submitted()
            self._emit({"kind": "accepted", "text": text})

    async def compact(self) -> None:
        """Write /compact: no accepted event, no consumed count (the CLI
        never echoes a slash command back), the translator's compact
        flags armed."""
        async with self._write_lock:
            if not self.alive:
                await self._start()
            self._proc.stdin.write(user_message_line("/compact").encode("utf-8"))
            await self._proc.stdin.drain()
            self.state.touch()
            self.state.mark_compact_write()

    # -- lifecycle -----------------------------------------------------------

    async def _start(self) -> None:
        resumed = bool(self.session_id)
        cmd = build_seat_command(self.session_id, self.contract_path,
                                 self._claude_args, self._claude)
        self._proc = await self._spawn(cmd, self.project_dir, self._env)
        self.spawns += 1
        self.state = Translator(self._clock, self._closer_fallback)
        self._stderr_tail = collections.deque(maxlen=STDERR_TAIL_LINES)
        if self._lock is not None:
            try:
                self._lock.set_child(getattr(self._proc, "pid", None))
            except OSError:
                pass
        self._reader = asyncio.ensure_future(self._read(self._proc, self.state))
        stderr = getattr(self._proc, "stderr", None)
        if stderr is not None:
            self._stderr_reader = asyncio.ensure_future(
                self._drain_stderr(stderr, self._stderr_tail))
        if self._sweeper is None:
            self._sweeper = asyncio.ensure_future(self._sweep())
        self._logw("seat_start", cmd=cmd, cwd=self.project_dir, resume=resumed,
                   pid=getattr(self._proc, "pid", None))
        self._emit({"kind": "seat", "state": "resumed" if resumed else "started",
                    "session_id": self.session_id, "pid": getattr(self._proc, "pid", None)})

    async def _read(self, proc, state: Translator) -> None:
        tail = self._stderr_tail
        while True:
            try:
                line = await proc.stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logw("seat_read_failed", error=repr(exc)[:300])
                self._emit({"kind": "error",
                            "message": f"claude's output became unreadable ({exc!r});"
                                       " ending the process"})
                _signal_group(proc, signal.SIGKILL)
                break
            if not line:
                break
            text = line.decode("utf-8", "replace")
            self._logw("stream", line=text.rstrip("\n"))
            try:
                events = state.record(text)
            except Exception as exc:
                self._logw("seat_record_failed", error=repr(exc)[:300])
                continue
            for ev in events:
                self._emit(ev)
        rc = await proc.wait()
        if self._proc is proc:
            self._proc = None
            if self._lock is not None:
                try:
                    self._lock.set_child(None)
                except OSError:
                    pass
        self._logw("seat_exit", rc=rc, stderr=list(tail))
        events = state.process_exited(rc)
        for ev in events:
            if ev.get("kind") == "seat":
                ev["stderr"] = list(tail)
            self._emit(ev)

    @staticmethod
    async def _drain_stderr(stream, tail: collections.deque) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                tail.append(line.decode("utf-8", "replace").rstrip("\n"))
        except (asyncio.CancelledError, Exception):
            return

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval_s)
            try:
                state = self.state
                now = self._clock()
                if self.alive and state.idle \
                        and now - state.last_activity > self.idle_close_s:
                    self._logw("seat_idle_close")
                    for ev in state.idle_closed():
                        self._emit(ev)
                    await self.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logw("seat_sweep_failed", error=repr(exc)[:300])

    async def close(self) -> None:
        """Close the CURRENT child: stdin EOF, a bounded wait, then
        SIGTERM and SIGKILL to the group. Shielded, so a cancelled caller
        cannot strand a half-signalled child."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        task = asyncio.ensure_future(self._close_proc(proc))
        self._closings.add(task)
        task.add_done_callback(self._closings.discard)
        await asyncio.shield(task)

    @staticmethod
    async def _close_proc(proc) -> None:
        try:
            proc.stdin.close()
        except Exception:
            pass
        for grace, sig in ((CLOSE_GRACE_S, signal.SIGTERM),
                           (TERM_GRACE_S, signal.SIGKILL)):
            try:
                await asyncio.wait_for(proc.wait(), grace)
                return
            except asyncio.TimeoutError:
                _signal_group(proc, sig)
        await proc.wait()

    async def shutdown(self) -> None:
        """Host exit: the sweep ends, the child closes, and every close
        still in flight is awaited. No child outlives the host."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):
                pass
            self._sweeper = None
        await self.close()
        while self._closings:
            await asyncio.gather(*list(self._closings), return_exceptions=True)

    def kill_now(self) -> None:
        """Synchronous last resort (atexit, a signal): the whole group,
        TERM then KILL."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        pid = getattr(proc, "pid", None)
        if pid is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except OSError:
                return
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
                time.sleep(0.05)

    # -- plumbing ------------------------------------------------------------

    def _emit(self, ev: dict) -> None:
        if ev.get("kind") == "session":
            sid = ev.get("id")
            if sid and sid != self.session_id:
                self.session_id = sid
                if self.pin is not None:
                    try:
                        self.pin.save(sid)
                    except OSError as exc:
                        self._logw("pin_failed", error=repr(exc))
                if self._lock is not None:
                    try:
                        self._lock.set_session(sid)
                    except OSError:
                        pass
        try:
            self._on_event(ev)
        except Exception as exc:
            print(f"[hands-free-voice: event handler failed ({exc!r}); {ev.get('kind')} dropped]",
                  file=sys.stderr)

    def _logw(self, kind: str, **fields) -> None:
        if self._log is not None:
            self._log.write(kind, **fields)
