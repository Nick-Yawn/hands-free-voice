"""The gate: the speech-to-text socket is open only while someone talks.

Shared by every provider. A local VAD watches the mic. While the user is
silent no socket exists; the last `pre_roll_s` of audio waits in a ring.
At a speech onset the gate opens a session, flushes the ring and every
frame that arrived while connecting, and streams live from there, so no
word is lost to the connection. After the voice stops, a hangover
window runs; when it elapses (and the TurnMachine is not holding a turn
open: `hold()`), the session is closed gracefully, which flushes the
vendor's last finals, and the gate is closed again. Why: an idle socket
counts against a vendor's concurrency limit, vendors close idle sockets
on their own schedule, and streaming an open mic all day would burn a
plan's included hours.

Two hangovers: `hangover_s` once the session has produced words, so a
mid-thought pause does not cut a turn; `empty_hangover_s` when it has
produced none, so a door slam or a keyboard burst that opened the gate
costs two seconds, not ten.

Reconnect is automatic: a session the server ends (or errors) while the
gate still wants one is reopened with the ring flushed again. A session
that has heard `deaf_s` of VOICED audio (accumulated, not wall-clock:
a long silent pause is not deafness) without a word coming back is
closed and reopened; a second deaf session in a row asks the caller to
rebuild the input device (`on_deaf`), since audio that never
transcribes is more often a wrong device or rate than a broken link.

The gate emits its own SpeechStarted at every onset, whichever vendor
is listening, so the closer's settle window reacts within a frame.
"""

import asyncio
import collections
import contextlib
import time

from hands_free_voice.providers import Error, Final, Partial, SpeechStarted
from hands_free_voice.vad import Framer

DEFAULT_PRE_ROLL_S = 0.5
DEFAULT_HANGOVER_S = 10.0
DEFAULT_EMPTY_HANGOVER_S = 2.0
DEFAULT_DEAF_S = 8.0
ONSET_FRAMES = 3         # consecutive voiced VAD frames (60 ms at 20 ms) to open
RESUME_GAP_S = 0.3       # voice after this much quiet is a new onset
MAX_BUFFER_S = 30.0      # audio kept while a connect is failing
TICK_S = 0.25            # the pump's clock for hangover checks
OPEN_BACKOFF_MAX_S = 30.0


class Gate:
    CLOSED, OPENING, OPEN, CLOSING = "closed", "opening", "open", "closing"

    def __init__(self, open_session, on_event, *, vad, rate: int = 16000,
                 pre_roll_s: float = DEFAULT_PRE_ROLL_S,
                 hangover_s: float = DEFAULT_HANGOVER_S,
                 empty_hangover_s: float = DEFAULT_EMPTY_HANGOVER_S,
                 deaf_s: float = DEFAULT_DEAF_S,
                 hold=None, on_link=None, on_deaf=None, out=None, log=None,
                 clock=time.monotonic, onset_frames: int = ONSET_FRAMES,
                 resume_gap_s: float = RESUME_GAP_S, tick_s: float = TICK_S,
                 max_buffer_s: float = MAX_BUFFER_S, backoff_s: float = 1.0):
        self._open_session = open_session
        self._on_event = on_event
        self._vad = vad
        self._framer = Framer(vad.frame_bytes)
        self.rate = rate
        self.pre_roll_bytes = int(rate * 2 * pre_roll_s)
        self.hangover_s = hangover_s
        self.empty_hangover_s = empty_hangover_s
        self.deaf_s = deaf_s
        self._hold = hold or (lambda: False)
        self._on_link = on_link or (lambda up, detail=None: None)
        self._on_deaf = on_deaf or (lambda: None)
        self._out = out or (lambda s: None)
        self._log = log or (lambda kind, **f: None)
        self._clock = clock
        self.onset_frames = onset_frames
        self.resume_gap_s = resume_gap_s
        self.tick_s = tick_s
        self.max_buffer_bytes = int(rate * 2 * max_buffer_s)
        self.backoff_s = backoff_s

        self.state = self.CLOSED
        self.sessions_opened = 0
        self.last_voice_t: float | None = None
        self._ring: collections.deque = collections.deque()
        self._ring_bytes = 0
        self._pending: list[bytes] = []   # the ring, snapshotted at the open
        self._buffer: list[bytes] = []    # frames that arrived while connecting
        self._buffer_bytes = 0
        self._send_q: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._session = None
        self._voiced_run = 0
        self._in_speech = False
        self._text_seen = False
        self._opened_t = 0.0
        self._voiced_no_text_s = 0.0   # voiced audio since the last words (or the open)
        self._frame_s = vad.frame_bytes / (rate * 2)
        self._deaf_strikes = 0
        self._reopen = False
        self._close_reason: str | None = None
        self._quit = False

    # -- the mic side -----------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        """A chunk of PCM16 mono at `rate`, from the mic, on the loop thread."""
        if self._quit or not chunk:
            return
        now = self._clock()
        onset = False
        for frame in self._framer.frames(chunk):
            if self._vad.is_speech(frame):
                self._voiced_run += 1
                self.last_voice_t = now
                if self.state != self.CLOSED:
                    self._voiced_no_text_s += self._frame_s
                if not self._in_speech and self._voiced_run >= self.onset_frames:
                    self._in_speech = True
                    onset = True
            else:
                self._voiced_run = 0
                if self._in_speech and self.last_voice_t is not None \
                        and now - self.last_voice_t > self.resume_gap_s:
                    self._in_speech = False
        self._ring.append(chunk)
        self._ring_bytes += len(chunk)
        while self._ring_bytes > self.pre_roll_bytes and len(self._ring) > 1:
            self._ring_bytes -= len(self._ring.popleft())
        if onset:
            self._on_event(SpeechStarted())
        if self.state == self.CLOSED:
            if onset:
                self._start()
        elif self.state == self.OPENING:
            self._buffer.append(chunk)
            self._buffer_bytes += len(chunk)
            while self._buffer_bytes > self.max_buffer_bytes and self._buffer:
                self._buffer_bytes -= len(self._buffer.pop(0))
        elif self.state == self.OPEN:
            self._send_q.put_nowait(chunk)
        elif self.state == self.CLOSING and onset:
            self._reopen = True  # voice came back mid-close: open again after it

    def _start(self) -> None:
        self.state = self.OPENING
        self._pending = list(self._ring)
        self._buffer, self._buffer_bytes = [], 0
        self._send_q = asyncio.Queue()
        self._task = asyncio.ensure_future(self._session_loop())

    # -- the session side ---------------------------------------------------------

    @property
    def listening(self) -> bool:
        return self.state in (self.OPENING, self.OPEN)

    def _wanted(self) -> bool:
        """Should a socket exist right now? While a turn is held open, or
        while the voice is within its hangover."""
        if self._quit:
            return False
        if self._hold():
            return True
        if self.last_voice_t is None:
            return False
        limit = self.hangover_s if self._text_seen else self.empty_hangover_s
        return self._clock() - self.last_voice_t <= limit

    def _close_reason_now(self) -> str | None:
        if self._quit:
            return "quit"
        if self.deaf_s and self._voiced_no_text_s > self.deaf_s:
            return "deaf"
        if self._hold():
            return None
        if not self._wanted():
            return "hangover" if self._text_seen else "no words"
        return None

    def _note(self, ev) -> None:
        if isinstance(ev, (Partial, Final)) and (ev.text or "").strip():
            self._text_seen = True
            self._voiced_no_text_s = 0.0
            self._deaf_strikes = 0

    async def _pump(self, session) -> None:
        q = self._send_q
        while True:
            chunk = None
            try:
                chunk = await asyncio.wait_for(q.get(), self.tick_s)
            except asyncio.TimeoutError:
                pass
            if chunk is not None:
                try:
                    await session.send(chunk)
                except Exception as exc:
                    self._log("stt", state="send_failed", error=repr(exc)[:200])
                    return  # the reader sees the drop and decides
            reason = self._close_reason_now()
            if reason is None:
                continue
            self.state = self.CLOSING
            self._close_reason = reason
            if reason == "deaf":
                self._deaf_strikes += 1
                self._reopen = True
                self._out("[ears: voice but no words for"
                          f" {self.deaf_s:.0f}s; reconnecting the speech-to-text link]")
                if self._deaf_strikes >= 2:
                    self._deaf_strikes = 0
                    self._on_deaf()
            while not q.empty():
                with contextlib.suppress(Exception):
                    await session.send(q.get_nowait())
            with contextlib.suppress(Exception):
                await session.close()  # graceful: the last finals reach the reader
            return

    async def _session_loop(self) -> None:
        backoff = self.backoff_s
        failed = False
        while True:
            t0 = self._clock()
            try:
                session = await self._open_session()
            except Exception as exc:
                failed = True
                self._log("stt", state="open_failed", error=repr(exc)[:200])
                self._on_link(False, f"speech-to-text link failed ({exc!r});"
                                     f" retrying in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(max(backoff, 0.05) * 2, OPEN_BACKOFF_MAX_S)
                if not self._wanted():
                    self._give_up()
                    return
                continue
            self.sessions_opened += 1
            self._session = session
            if failed:
                failed = False
                self._on_link(True, None)
            backoff = self.backoff_s
            flushed = sum(len(c) for c in self._pending) + self._buffer_bytes
            self._log("stt", state="open", connect_ms=int((self._clock() - t0) * 1000),
                      flushed_ms=int(flushed / (self.rate * 2) * 1000))
            self.state = self.OPEN
            self._close_reason = None
            self._reopen = False
            for chunk in self._pending + self._buffer:
                self._send_q.put_nowait(chunk)
            self._pending, self._buffer, self._buffer_bytes = [], [], 0
            self._opened_t = self._clock()
            self._text_seen = False
            self._voiced_no_text_s = 0.0
            pump = asyncio.ensure_future(self._pump(session))
            reason = None
            try:
                async for ev in session.events():
                    if isinstance(ev, Error):
                        reason = ev.message
                        break
                    self._note(ev)
                    self._on_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = repr(exc)
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                self._session = None
            if self.state == self.CLOSING:
                self._log("stt", state="closed", reason=self._close_reason,
                          words=self._text_seen)
                self.state = self.CLOSED
                if self._reopen and not self._quit:
                    self._start_again()
                    continue
                return
            # the server ended it
            with contextlib.suppress(Exception):
                await session.close()
            self._log("stt", state="dropped", reason=reason, words=self._text_seen)
            self._on_link(False, f"speech-to-text link dropped ({reason})")
            if self._wanted():
                failed = True
                self._start_again()
                continue
            self.state = self.CLOSED
            return

    def _start_again(self) -> None:
        self.state = self.OPENING
        self._pending = list(self._ring)
        self._buffer, self._buffer_bytes = [], 0
        self._send_q = asyncio.Queue()

    def _give_up(self) -> None:
        lost = sum(len(c) for c in self._pending) + self._buffer_bytes
        self.state = self.CLOSED
        self._pending, self._buffer, self._buffer_bytes = [], [], 0
        self._out(f"[ears: speech-to-text unreachable; {lost / (self.rate * 2):.1f}s"
                  " of speech was lost]")

    # -- lifecycle ----------------------------------------------------------------

    async def probe(self) -> None:
        """Open and close one session: proves the key and the network
        before anyone speaks. Raises what the open raises."""
        t0 = self._clock()
        session = await self._open_session()
        self._log("stt", state="probe_ok", connect_ms=int((self._clock() - t0) * 1000))
        with contextlib.suppress(Exception):
            await session.close()

    async def stop(self) -> None:
        """Quit: no flush is owed; whatever is open closes. The session
        is taken before the loop is cancelled, since the loop's own
        cleanup forgets it; adapters make a second close harmless."""
        self._quit = True
        session, self._session = self._session, None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.close(), 1.0)
        self.state = self.CLOSED
