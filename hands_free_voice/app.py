"""The host: what the text and voice front ends share.

One Seat, one SpokenLog, one terminal. Seat events are printed, logged
and, where they carry `say` lines, appended to the SpokenLog. Messages
go through a small FIFO worker so a /compact can hold behind a running
query while ordinary messages are written the moment they arrive.
"""

import asyncio
import sys
import time

from hands_free_voice.seat import Seat
from hands_free_voice.spoken_log import SpokenLog
from hands_free_voice.state import EventLog

RECEIVED = "Received."
COMPACTING = "Compacting."
COMPACT = object()  # the turn queue's /compact entry


def compose_status(snap: dict) -> str:
    """One short spoken-register status line from a snapshot dict. Keys
    are all optional: link_up (STT link), mic_age_s, session_id,
    queued_turns, query_open, unconsumed, last_pct, claude_alive."""
    parts = []
    if "link_up" in snap:
        parts.append("Link up." if snap["link_up"] else "Link down.")
    if "mic_age_s" in snap:
        age = snap["mic_age_s"]
        if age is None:
            parts.append("No mic frames yet.")
        elif age < 2.0:
            parts.append("Mic just heard.")
        else:
            parts.append(f"Mic quiet {int(age)} seconds.")
    sid = snap.get("session_id")
    parts.append(f"Session {sid[:8]}." if sid else "A fresh session.")
    if not snap.get("claude_alive", True):
        parts.append("Claude is closed until the next turn.")
    work = []
    if snap.get("query_open"):
        work.append("one query running")
    if snap.get("queued_turns"):
        work.append(f"{snap['queued_turns']} queued")
    if snap.get("unconsumed"):
        work.append(f"{snap['unconsumed']} written, not yet read")
    turns = ", ".join(work) if work else "no open turns"
    parts.append(turns[0].upper() + turns[1:] + ".")
    pct = snap.get("last_pct")
    parts.append(f"Context at {pct} percent." if pct is not None
                 else "Context not yet known.")
    return " ".join(parts)


def fmt_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


class Host:
    def __init__(self, cfg: dict, project_dir: str, *, seat: Seat | None = None,
                 spoken: SpokenLog | None = None, log: EventLog | None = None,
                 out=None, clock=time.monotonic):
        self.cfg = cfg
        self.project_dir = project_dir
        self.seat = seat
        self.spoken = spoken
        self.log = log or EventLog(None)
        self._out = out or (lambda s: print(s, flush=True))
        self._clock = clock
        self.turn_q: asyncio.Queue = asyncio.Queue()
        self.quitting = asyncio.Event()
        self.turns = 0
        self.query_since: float | None = None
        self.last_spoken_at = clock()
        self.last_pct: int | None = None
        self._compact_queued = False
        self._inflight = 0  # dequeued, not yet written (a spawn in progress)
        self._worker: asyncio.Task | None = None
        self.status_extra = None  # callable -> dict merged into the snapshot
        self.on_still_here = None  # callable, fired after a quiet stretch

    # -- output ------------------------------------------------------------

    def say_local(self, text: str) -> None:
        """A line hands-free-voice itself composes (status, acks)."""
        self._out(f"  · {text}")
        self.spoken.append(text, "speech", kind="local", role="status")

    def _spoken_started(self, entry) -> None:
        self.last_spoken_at = self._clock()
        self._out(f"  » {entry.text}")
        self.log.write("spoken", index=entry.index, text=entry.text,
                       register=entry.register)

    def _spoken_failed(self, entry, exc) -> None:
        self._out(f"  [speech failed on line {entry.index}: {exc!r}]")
        self.log.write("speech_error", index=entry.index, error=repr(exc))

    def bind(self, seat: Seat, spoken: SpokenLog) -> None:
        self.seat = seat
        self.spoken = spoken
        spoken.on_start = self._spoken_started
        spoken.on_error = self._spoken_failed

    # -- seat events ---------------------------------------------------------

    def on_seat_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        self.log.write("seat_event", **{k: v for k, v in ev.items() if k != "say"})
        # (an event's own "kind" rides as a field; the log's kind is "seat_event")
        if kind == "accepted":
            self._out(f"→ sent: {ev.get('text', '')}")
            self.turns += 1
            self.spoken.append(RECEIVED, "speech", kind="ack", role="ack")
        elif kind == "seat":
            state = ev.get("state")
            if state in ("started", "resumed"):
                sid = ev.get("session_id")
                self._out(f"[claude started in {self.project_dir}"
                          + (f", resuming session {sid[:8]}" if sid else "") + "]")
            elif state == "exited":
                self.query_since = None
                rc = ev.get("rc")
                self._out(f"[claude exited rc={rc}]")
                if rc not in (0, None):
                    for line in (ev.get("stderr") or [])[-10:]:
                        self._out(f"    {line}")
            elif state == "idle_closed":
                self.query_since = None
                self._out("[claude idle; closed until the next turn]")
        elif kind == "session":
            self._out(f"[session {ev.get('id')}]")
        elif kind == "consumed":
            if self.query_since is None:
                self.query_since = self._clock()
        elif kind == "tool":
            if ev.get("say"):
                self._out(f"  ⋯ {ev['say'][0]['text']}")
            if self.query_since is None:
                self.query_since = self._clock()
        elif kind == "progress":
            self._out(f"  ▷ {ev.get('text')}")
        elif kind == "context":
            self.last_pct = ev.get("pct")
        elif kind == "result":
            self.query_since = None
            text = ev.get("text") or ""
            tail = fmt_elapsed(ev.get("elapsed_s"))
            if ev.get("suppressed"):
                self._out(f"  (empty {ev.get('origin')} result, not spoken)")
            else:
                self._out(f"\n{text}\n" + (f"  ({tail})" if tail else ""))
        elif kind == "say":
            self._out(f"  · {ev.get('text')}")
        elif kind == "error":
            self._out(f"[error: {ev.get('message')}]")
        elif kind == "injected":
            self._out("  (a background task's completion joined the running query)")
        for line in ev.get("say") or []:
            self.spoken.append(line["text"], line["register"], kind=kind,
                               role=line.get("role", "answer"))

    # -- messages --------------------------------------------------------------

    def enqueue_turn(self, text: str) -> None:
        self.log.write("dispatch", text=text)
        self.spoken.user_input()
        self.turn_q.put_nowait(text)

    def command_compact(self) -> None:
        if self._compact_queued or self.seat.compact_pending:
            self.say_local(RECEIVED)
            return
        self._compact_queued = True
        busy = self.seat.occupied
        self.say_local(RECEIVED + (" Compacting after this turn lands." if busy else ""))
        self.turn_q.put_nowait(COMPACT)

    async def turn_worker(self) -> None:
        while True:
            item = await self.turn_q.get()
            self._inflight += 1
            try:
                if item is COMPACT:
                    while self.seat.occupied:
                        await asyncio.sleep(0.05)
                    self.say_local(COMPACTING)
                    await self.seat.compact()
                else:
                    await self.seat.submit(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._out(f"[turn error: {exc!r}]")
                self.log.write("turn_error", error=repr(exc))
                self.spoken.append("That message did not reach Claude.", "speech",
                                   kind="error", role="status")
            finally:
                self._inflight -= 1
                if item is COMPACT:
                    self._compact_queued = False

    def start(self) -> None:
        self.spoken.start()
        if self._worker is None:
            self._worker = asyncio.ensure_future(self.turn_worker())

    # -- status and quiet ------------------------------------------------------

    def status_snapshot(self) -> dict:
        snap = {
            "session_id": self.seat.session_id,
            "claude_alive": self.seat.alive or self.seat.spawns == 0,
            "queued_turns": self.turn_q.qsize(),
            "query_open": self.seat.busy,
            "unconsumed": self.seat.unconsumed,
            "last_pct": self.last_pct,
        }
        if self.status_extra:
            snap.update(self.status_extra())
        return snap

    def command_status(self) -> str:
        line = compose_status(self.status_snapshot())
        self.say_local(line)
        return line

    async def still_here_ticker(self, quiet_s: float, interval_s: float = 2.0) -> None:
        """A soft still-here cue after `quiet_s` of silence while Claude
        works; anything spoken resets the clock."""
        while True:
            await asyncio.sleep(interval_s)
            if not self.seat.busy or self.spoken.busy:
                continue
            if self._clock() - self.last_spoken_at < quiet_s:
                continue
            self.last_spoken_at = self._clock()
            self.log.write("still_here")
            if self.on_still_here:
                self.on_still_here()

    @property
    def occupied(self) -> bool:
        return self.seat.occupied or not self.turn_q.empty() or self._inflight > 0

    async def quit(self) -> None:
        if self.occupied:
            self.say_local("A turn is still in flight. Closing after it lands.")
            self._out("[Ctrl-C to force]")
            while self.occupied:
                await asyncio.sleep(0.2)
        self.quitting.set()

    async def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None
        try:
            await self.seat.shutdown()
        finally:
            await self.spoken.stop()


# -- the text front end ------------------------------------------------------

TEXT_HELP = """[hands-free-voice --text: type a message and press Enter to send it.
 Local commands: :status  :again  :back N  :stop  :resume  :compact  :quit]"""


def parse_text_command(line: str) -> tuple[str, int | None] | None:
    """A ':command' line -> (name, arg), or None for an ordinary message."""
    s = line.strip()
    if s.lower() in ("quit", "exit"):
        return ("quit", None)
    if not s.startswith(":"):
        return None
    words = s[1:].split()
    if not words:
        return None
    name = words[0].lower()
    if name in ("again", "replay", "repeat"):
        return ("again", None)  # the last answer
    if name == "back":
        try:
            return ("again", int(words[1]) if len(words) > 1 else 1)  # n raw lines
        except ValueError:
            return ("again", 1)
    if name in ("stop", "pause"):
        return ("stop", None)
    if name in ("resume", "continue"):
        return ("resume", None)
    if name in ("status", "compact", "quit", "exit", "cancel"):
        return ("quit" if name == "exit" else name, None)
    return (name, None)


async def handle_command(host: Host, name: str, arg) -> None:
    host.log.write("command", name=name, arg=arg)
    if name not in ("stop", "resume"):
        host.spoken.user_input()  # any other command is new input: a stop ends
    if name == "stop":
        host.spoken.pause("user")
        host._out("  · paused")
    elif name == "resume":
        host.spoken.resume("user")
        host._out("  · resumed")
    elif name == "again":
        played = host.spoken.replay(arg) if arg else host.spoken.replay_answer()
        if played is None:
            host._out("  · nothing to replay yet")
    elif name == "status":
        host.command_status()
    elif name == "compact":
        host.command_compact()
    elif name == "cancel":
        host._out("  · nothing to cancel in text mode")
    elif name == "quit":
        await host.quit()
    else:
        host._out(f"  · unknown command: {name}")


def stdin_lines() -> asyncio.Queue:
    """Lines from stdin via a daemon thread, so a pending read never
    blocks the loop or the exit."""
    import threading

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def pump():
        for line in sys.stdin:
            loop.call_soon_threadsafe(q.put_nowait, line)
        loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=pump, daemon=True).start()
    return q


async def run_text(host: Host, lines: asyncio.Queue | None = None,
                   prompt: bool = True) -> None:
    lines = lines if lines is not None else stdin_lines()
    host._out(f"[hands-free-voice in {host.project_dir}]")
    host._out(TEXT_HELP)
    host.start()
    ticker = asyncio.ensure_future(host.still_here_ticker(
        float(host.cfg["seat"]["still_here_s"])))
    host.on_still_here = host.on_still_here or (lambda: host._out("  ⋯ (still working)"))
    quit_wait = asyncio.ensure_future(host.quitting.wait())
    try:
        while not host.quitting.is_set():
            if prompt:
                print("you ▸ ", end="", flush=True)
            getter = asyncio.ensure_future(lines.get())
            done, _ = await asyncio.wait({getter, quit_wait},
                                         return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                break
            line = getter.result()
            if line is None:  # EOF: like :quit, let the turn in flight land
                await host.quit()
                break
            line = line.strip()
            if not line:
                continue
            cmd = parse_text_command(line)
            if cmd:
                await handle_command(host, *cmd)
            else:
                host.enqueue_turn(line)
    finally:
        quit_wait.cancel()
        ticker.cancel()
        await host.shutdown()
        host._out("[session closed]")
