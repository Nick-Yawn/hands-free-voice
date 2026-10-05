"""The seat for the mod: Claude Code runs the session, this process listens.

Under the hands-free-voice mod, Claude Code owns the conversation and the
mod relays it here as small events (a turn started, a tool was called, a
response's text, the turn ended). The ModTranslator turns those into the
same events the Translator makes from `claude -p` stream-json, so the
host, the SpokenLog and the voice front run unchanged. The ModSeat is the
Seat they drive: a heard turn or a compact goes out as one JSON line on
stdout, which the mod reads and hands to Claude Code.

Events from the mod, each a dict with a `kind`:

  start                       a main-loop turn began
  read                        a turn read a message delivered into it
  tool      name, input       a main-loop tool call
  text      text              one response's visible text
  complete  answer, reason,   the turn ended ("answer", "aborted",
            pct, elapsed_s    "refusal" or "error"); pct the context fill
  compacted                   a compact the mod ran has finished
"""

from hands_free_voice.narrate import narrate_tool
from hands_free_voice.translator import (
    ANSWER, CLOSER, COMPACTED, NARRATION, NARRATION_ROLE, NO_VOICE_BLOCK, SPEECH,
    STATUS, TURN_FAILED, VOICE_RE, extract_voice)

INTERRUPTED = "Interrupted."


def _say(text: str, register: str = SPEECH, role: str = ANSWER) -> dict:
    return {"text": text, "register": register, "role": role}


class ModTranslator:
    def __init__(self, closer_fallback: str = "Done."):
        self.closer_fallback = closer_fallback
        self.query_open = False
        self.unconsumed = 0
        self.compact_pending = False
        self.last_pct: int | None = None
        self._streamed: set[str] = set()

    @property
    def busy(self) -> bool:
        return self.query_open

    def submitted(self) -> None:
        self.unconsumed += 1

    def mark_compact_write(self) -> None:
        self.compact_pending = True

    def event(self, ev: dict) -> list[dict]:
        """One event from the mod -> zero or more seat events. Unknown or
        malformed events yield nothing."""
        if not isinstance(ev, dict):
            return []
        kind = ev.get("kind")
        if kind == "start":
            self.query_open = True
            self._streamed = set()
            self.unconsumed = max(0, self.unconsumed - 1)
            return [{"kind": "consumed", "text": ""}]
        if kind == "read":
            self.unconsumed = max(0, self.unconsumed - 1)
            return []
        if kind == "tool":
            name = str(ev.get("name") or "")
            inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
            event = {"kind": "tool", "name": name, "input": inp}
            line = narrate_tool(name, inp)
            if line:
                event["say"] = [_say(line, NARRATION, NARRATION_ROLE)]
            return [event]
        if kind == "text":
            out = []
            for block in VOICE_RE.findall(str(ev.get("text") or "")):
                block = block.strip()
                if block and block not in self._streamed:
                    self._streamed.add(block)
                    out.append({"kind": "progress", "text": block, "say": [_say(block)]})
            return out
        if kind == "complete":
            return self._complete(ev)
        if kind == "compacted":
            self.compact_pending = False
            return [{"kind": "say", "text": COMPACTED, "say": [_say(COMPACTED, role=STATUS)]}]
        return []

    def _complete(self, ev: dict) -> list[dict]:
        self.query_open = False
        out = []
        pct = ev.get("pct")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            self.last_pct = round(pct)
            out.append({"kind": "context", "pct": self.last_pct})
        answer = str(ev.get("answer") or "")
        reason = ev.get("reason") or "answer"
        result = {"kind": "result", "text": answer, "is_error": reason in ("error", "refusal")}
        elapsed = ev.get("elapsed_s")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            result["elapsed_s"] = elapsed
        block = extract_voice(answer)
        result["block"] = block
        closer = f"{self.last_pct} percent." if self.last_pct is not None else self.closer_fallback
        result["closer"] = closer
        if reason == "aborted":
            lines = [_say(INTERRUPTED, role=STATUS)]
        else:
            lines = []
            if result["is_error"]:
                lines.append(_say(TURN_FAILED, role=STATUS))
            elif block is None:
                lines.append(_say(NO_VOICE_BLOCK, role=STATUS))
            elif block not in self._streamed:
                lines.append(_say(block))
            lines.append(_say(closer, role=CLOSER))
        result["say"] = lines
        out.append(result)
        self._streamed = set()
        return out


class ModSeat:
    """What the host drives in place of a Seat. `write` puts one dict on
    stdout as a JSON line for the mod; `on_event` is the host's."""

    spawns = 1  # Claude Code is always up; the host never reads "closed"
    alive = True
    session_id = None

    def __init__(self, write, on_event, closer_fallback: str = "Done."):
        self._write = write
        self._on_event = on_event
        self.state = ModTranslator(closer_fallback)

    @property
    def busy(self) -> bool:
        return self.state.busy

    @property
    def unconsumed(self) -> int:
        return self.state.unconsumed

    @property
    def occupied(self) -> bool:
        return bool(self.state.busy or self.state.unconsumed)

    @property
    def compact_pending(self) -> bool:
        return self.state.compact_pending

    @property
    def last_pct(self) -> int | None:
        return self.state.last_pct

    async def submit(self, text: str) -> None:
        self._write({"type": "turn", "text": text})
        self.state.submitted()
        self._on_event({"kind": "accepted", "text": text})

    async def compact(self) -> None:
        self._write({"type": "compact"})
        self.state.mark_compact_write()

    def event(self, ev: dict) -> None:
        for out in self.state.event(ev):
            self._on_event(out)

    async def shutdown(self) -> None:
        pass
