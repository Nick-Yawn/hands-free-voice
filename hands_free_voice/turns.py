"""The TurnMachine: address-framed turn assembly over a transcript stream.

The mic hears everything; only speech framed by "<address> ... <closer>"
becomes a turn. Everything outside the frame is dropped on the floor.
Framing is also the vocal control surface: "<address> <command>" runs a
local command without costing a Claude turn.

Position rules (design §9.7):

  * The address word counts only as an utterance's first word. A
    mention later in a sentence is content. Tolerating filler before it
    ("um, operator") is a knob, off by default.
  * The closer counts only as the final word followed by silence: a
    final ending in the closer puts the machine in CLOSING and the host
    waits a short settle window; more speech in that window makes the
    closer content, silence makes it a dispatch (settle()).
  * A command is the address word plus the command phrase with nothing
    after it, except "stop", which fires anywhere in an utterance
    because a false stop only pauses speech.
  * The address word opening a new utterance after more than
    `readdress_gap_s` of quiet in an open turn discards that turn and
    starts fresh (an "abandoned" action marks it).

Actions returned by feed() / settle() / abandon_stale(), in order:
  ("drop", text)              unaddressed speech, ignored
  ("open", text|None)         a turn opened; text is what came with it
  ("append", text)            content added to the open turn
  ("closing", turn)           the closer ended this final; settle() next
  ("dispatch", turn)          the closer held through the silence: send
  ("command", name, arg)      a local command (arg is always None; no
                               phrase names a replay count, but the
                               SpokenLog cursor keeps the n-back ability
                               for other code to call)
  ("abandoned", text)         an open turn was discarded un-dispatched
"""

import re
import time

ADDRESS = "operator"
CLOSER = "over"

COMMANDS = {
    "stop": "stop",
    "pause": "stop",
    "resume": "resume",
    "continue": "resume",
    "again": "again",
    "repeat": "again",
    "replay": "again",
    "say that again": "again",
    "status": "status",
    "where are we": "status",
    "cancel": "cancel",
    "never mind": "cancel",
    "nevermind": "cancel",  # dictation often joins the two words
    "compact": "compact",
    "compact yourself": "compact",
    "quit": "quit",
    "exit": "quit",
    "end session": "quit",
    "confirm": "confirm",  # files the feedback just read back (the mod's)
    "confirmed": "confirm",
}

# One phrase per command, in the order the mod's status line lists them.
# Confirm is left off: it matters only once a feedback draft is read back,
# and the read-back says when to say it.
SHOWN_COMMANDS = ("stop", "resume", "again", "never mind", "status", "compact", "quit")


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def command_for(tail: str) -> tuple[str, int | None] | None:
    """The command a normalized phrase names, with its argument, or None."""
    cmd = COMMANDS.get(tail)
    if cmd:
        return (cmd, None)
    return None


class TurnMachine:
    IDLE = "idle"
    OPEN = "open"
    CLOSING = "closing"

    def __init__(self, address: str = ADDRESS, closer: str = CLOSER, *,
                 filler_before_address: bool = False,
                 readdress_gap_s: float = 2.0,
                 idle_turn_s: float = 60.0,
                 clock=time.monotonic):
        self.address = _norm(address)
        self.closer = tuple(_norm(w) for w in closer.split() if _norm(w))
        self.filler_before_address = filler_before_address
        self.readdress_gap_s = readdress_gap_s
        self.idle_turn_s = idle_turn_s
        self._clock = clock
        self.state = self.IDLE
        self._words: list[str] = []
        self._pending = False  # the last final ended on a bare address word
        self._last_active: float | None = None

    # -- reading ---------------------------------------------------------

    def text(self) -> str:
        return " ".join(self._words)

    def _turn(self) -> str:
        """The buffer minus a trailing closer."""
        n = len(self.closer)
        if self._ends_with_closer():
            return " ".join(self._words[:-n]).strip()
        return " ".join(self._words).strip()

    def preview(self, partial: str) -> str | None:
        """The turn's words as they would read with an in-progress partial
        added, for showing words as they are said; None when the partial
        neither continues a turn nor opens one (unaddressed speech, a
        command). Changes nothing."""
        if self.state != self.IDLE:
            return " ".join([self.text(), partial.strip()]).strip()
        pairs = [(w, n) for w in partial.split() if (n := _norm(w))]
        frame = self._frame_index([n for _, n in pairs])
        if frame is None:
            return None
        if command_for(" ".join(n for _, n in pairs[frame + 1:])):
            return None
        return " ".join(w for w, _ in pairs[frame + 1:])

    def _ends_with_closer(self) -> bool:
        n = len(self.closer)
        return len(self._words) >= n and \
            tuple(_norm(w) for w in self._words[-n:]) == self.closer

    def reset(self) -> None:
        self.state = self.IDLE
        self._words = []
        self._pending = False
        self._last_active = None

    def abandon(self) -> list[tuple]:
        """Discard an open turn (a cancel, a re-address, idle expiry):
        ("abandoned", text) when real content was lost, nothing for a
        bare open."""
        text = self._turn()
        self.reset()
        return [("abandoned", text)] if text else []

    # -- the stream ------------------------------------------------------

    def _frame_index(self, norm: list[str]) -> int | None:
        """Where the address word sits as a frame opener: index 0, or
        (with filler tolerance) its first occurrence anywhere."""
        if not norm:
            return None
        if norm[0] == self.address:
            return 0
        if self.filler_before_address and self.address in norm:
            return norm.index(self.address)
        return None

    def feed(self, final: str, now: float | None = None) -> list[tuple]:
        now = self._clock() if now is None else now
        pairs = [(w, n) for w in final.split() if (n := _norm(w))]
        norm = [n for _, n in pairs]
        if not norm:
            return []

        # A bare address word ending the previous final arms one final's
        # worth of grace: a next final that is exactly a command phrase
        # fires it ("Operator." ... "stop" split across finals).
        pending, self._pending = self._pending, norm[-1] == self.address
        if pending:
            cmd = command_for(" ".join(norm))
            if cmd:
                if self._words and _norm(self._words[-1]) == self.address:
                    self._words.pop()  # the address was the command's
                return self._fire(cmd)

        # "stop" fires anywhere: the address followed by exactly "stop"
        # to the end of the final.
        if self.address in norm:
            i = norm.index(self.address)
            tail = " ".join(norm[i + 1:])
            if COMMANDS.get(tail) == "stop":
                return self._fire(("stop", None))

        frame = self._frame_index(norm)
        if frame is not None:
            tail = " ".join(norm[frame + 1:])
            cmd = command_for(tail)
            if cmd:
                return self._fire(cmd)

        if self.state == self.IDLE:
            if frame is None:
                return [("drop", final)]
            content = norm[frame + 1:]
            if content == list(self.closer):
                return [("drop", final)]  # bare "operator over": no turn
            self.state = self.OPEN
            self._words = [w for w, _ in pairs[frame + 1:]]
            self._last_active = now
            return [("open", self.text() or None)] + self._maybe_close()

        # OPEN or CLOSING: a closer that was pending is now content unless
        # this final re-addresses after a gap.
        out: list[tuple] = []
        if frame is not None and self._last_active is not None \
                and now - self._last_active > self.readdress_gap_s:
            out += self.abandon()
            self.state = self.OPEN
            self._words = [w for w, _ in pairs[frame + 1:]]
            self._last_active = now
            self._pending = norm[-1] == self.address
            return out + [("open", self.text() or None)] + self._maybe_close()
        self.state = self.OPEN
        self._words += [w for w, _ in pairs]
        self._last_active = now
        return [("append", final)] + self._maybe_close()

    def _fire(self, cmd: tuple[str, int | None]) -> list[tuple]:
        name, arg = cmd
        out: list[tuple] = []
        if name == "cancel":
            out += self.abandon()
        elif self.state != self.IDLE and not self._turn():
            self.reset()  # a bare open whose next words were a command
        return out + [("command", name, arg)]

    def _maybe_close(self) -> list[tuple]:
        if self._ends_with_closer():
            self.state = self.CLOSING
            return [("closing", self._turn())]
        return []

    def settle(self) -> list[tuple]:
        """The closer's silence window elapsed with nothing more said:
        the turn is sent. A no-op unless CLOSING."""
        if self.state != self.CLOSING:
            return []
        turn = self._turn()
        self.reset()
        return [("dispatch", turn)]

    def abandon_stale(self, now: float | None = None) -> list[tuple]:
        """Idle expiry: an open turn that heard nothing for idle_turn_s
        is discarded, so it can't sit open forever swallowing room
        speech. A CLOSING turn is not stale; settle() owns it."""
        now = self._clock() if now is None else now
        if self.state == self.OPEN and self._last_active is not None \
                and now - self._last_active > self.idle_turn_s:
            return self.abandon()
        return []
