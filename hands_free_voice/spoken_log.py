"""The SpokenLog: every line hands-free-voice says, and a cursor playing through it.

Nothing is dropped; the cursor just moves. Pause, resume, "again", "back
N" and pause-while-the-user-talks all fall out of the cursor:

  * append() adds a line and wakes the player. Every line has a role:
    "answer" (content said to the user), "closer", "ack", "narration"
    or "status".
  * pause(who) / resume(who): two independent holds, "user" (the stop
    command) and "talk" (the address word opened a turn). Playback runs
    only while neither holds. Pausing stops the line in flight; it
    replays from its start on resume.
  * A user stop holds only what was playing. Output arriving while
    stopped queues silently. user_input() (a turn opened or sent, a
    command other than stop/resume) abandons the stopped line and
    everything queued behind it and clears the hold, so what comes next
    plays; "resume" alone plays the stopped line and its backlog.
  * replay_answer() ("again"): the last "answer" line at or before the
    cursor plays again, through its closer, and playback then resumes
    where it was. Acks, closers, narration and status lines are never
    what "again" means. replay(n) moves the cursor n raw lines back.

The player calls `speak(text, register)` for each entry; the callable
owns synthesis and playback (or printing, in text mode) and must honor
cancellation promptly. on_change() fires as a line starts or stops
playing, so a watcher can tell speaking from silent.
"""

import asyncio
import time
from dataclasses import dataclass, field


ANSWER, CLOSER = "answer", "closer"


@dataclass
class Entry:
    index: int
    text: str
    register: str
    kind: str
    role: str = ANSWER
    ts: float = field(default_factory=time.time)


class SpokenLog:
    def __init__(self, speak, *, on_start=None, on_error=None, on_change=None):
        self._speak = speak
        self.on_start = on_start      # (entry) as a line starts playing
        self.on_error = on_error      # (entry, exc) when a line fails
        self.on_change = on_change    # () as a line starts or stops playing
        self.entries: list[Entry] = []
        self.cursor = 0
        self.playing: int | None = None
        self._holds: set[str] = set()
        self._wake = asyncio.Event()
        self._current: asyncio.Task | None = None
        self._interrupted = False
        self._task: asyncio.Task | None = None
        self._window: tuple[int, int] | None = None  # a replay: (last index, resume at)

    # -- writing ---------------------------------------------------------

    def append(self, text: str, register: str = "speech", kind: str = "say",
               role: str = ANSWER) -> Entry:
        entry = Entry(len(self.entries), text, register, kind, role)
        self.entries.append(entry)
        self._wake.set()
        return entry

    # -- the cursor ------------------------------------------------------

    @property
    def paused(self) -> bool:
        return bool(self._holds)

    @property
    def holds(self) -> frozenset:
        return frozenset(self._holds)

    @property
    def busy(self) -> bool:
        return self.playing is not None

    @property
    def backlog(self) -> int:
        return len(self.entries) - self.cursor

    def pause(self, who: str = "user") -> None:
        self._holds.add(who)
        self._interrupt_current()

    def resume(self, who: str = "user") -> None:
        if who in self._holds:
            self._holds.discard(who)
            self._wake.set()

    def user_input(self) -> None:
        """New input from the user while stopped: the stopped line and
        everything queued behind it are abandoned, and the hold clears."""
        if "user" not in self._holds:
            return
        self._holds.discard("user")
        self.cursor = len(self.entries)
        self._window = None
        self._wake.set()

    def _reference(self) -> int | None:
        if self.playing is not None:
            return self.playing
        return self.cursor - 1 if self.cursor > 0 else None

    def replay(self, n: int = 1) -> int | None:
        """Cursor to the n-th line back; None when nothing has played."""
        ref = self._reference()
        if ref is None:
            return None
        target = max(0, ref - (max(1, n) - 1))
        self._window = None
        self._jump(target)
        return target

    def replay_answer(self) -> int | None:
        """"Again": the last answer at or before the cursor plays again,
        through its closer if one follows, then playback resumes where it
        was. None when no answer has played yet."""
        ref = self._reference()
        if ref is None:
            return None
        target = next((i for i in range(ref, -1, -1) if self.entries[i].role == ANSWER), None)
        if target is None:
            return None
        resume_at = self.playing + 1 if self.playing is not None else self.cursor
        end = next((i for i in range(target, len(self.entries))
                    if self.entries[i].role == CLOSER), target)
        self._window = (end, resume_at)
        self._jump(target)
        return target

    def _jump(self, target: int) -> None:
        self._interrupt_current()
        self.cursor = target
        self._holds.discard("user")
        self._wake.set()

    def _advance(self, idx: int) -> None:
        """The line at idx is done: the cursor moves on, unless something
        moved it meanwhile; a replay window ending here resumes."""
        if self.cursor != idx:
            return
        if self._window is not None and idx >= self._window[0]:
            self.cursor = max(self._window[1], idx + 1)
            self._window = None
        else:
            self.cursor = idx + 1

    def _interrupt_current(self) -> None:
        cur = self._current
        if cur is not None and not cur.done():
            self._interrupted = True
            cur.cancel()

    # -- the player ------------------------------------------------------

    def start(self) -> asyncio.Task:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())
        return self._task

    async def stop(self) -> None:
        self._interrupt_current()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        while True:
            if self.paused or self.cursor >= len(self.entries):
                self._wake.clear()
                await self._wake.wait()
                continue
            idx = self.cursor
            entry = self.entries[idx]
            self.playing = idx
            if self.on_start:
                self.on_start(entry)
            if self.on_change:
                self.on_change()
            self._interrupted = False
            self._current = asyncio.ensure_future(self._speak(entry.text, entry.register))
            try:
                await self._current
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    self._current.cancel()  # the player itself is stopping
                    raise
                self._interrupted = False
                continue  # the line was paused or replayed: the cursor decides
            except Exception as exc:
                if self.on_error:
                    self.on_error(entry, exc)
                self._advance(idx)  # a bad line is skipped, not looped
            else:
                self._advance(idx)
            finally:
                self.playing = None
                self._current = None
                if self.on_change:
                    self.on_change()
