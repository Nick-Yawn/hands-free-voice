"""The Translator: a pure function over `claude -p` stream-json records.

Stream lines go in; events come out, in stream order, each an ordinary
dict with a `kind` and, when something should be heard, a `say` list of
`{"text", "register"}` lines. There is no turn tracking, no attribution
and no dedupe: the seat is an ordered channel (design §3). The only state
is a few whole counters the idle rule needs, plus two per-query trackers
narration needs (which ⟦voice⟧ blocks already streamed this query, and
the query's last context figure), both reset at every query boundary.

Record shapes, all measured on the CLI (2.1.258 and later):

  * `system/init` opens a query and names the session.
  * `assistant` records carry tool calls and text; text may contain
    ⟦voice⟧ blocks, which are spoken as they stream. Records with a
    truthy `parent_tool_use_id` belong to a subagent and are skipped
    entirely (about 50 "Running a command." lines once spoke in a row
    without this).
  * `user` records are either the CLI's echo of a message we wrote
    (`--replay-user-messages`) or a tool_result batch.
  * `result` closes a query. Its top-level `usage` is cumulative across
    tool rounds (it once read "108 percent"); the LAST entry of
    `usage.iterations` is the true window fill. A query the CLI started
    on its own (a background task waking the session) carries an
    `origin` field; a message written on stdin yields a result with no
    `origin` at all. An empty result from a machine-started query is
    logged, never spoken.
  * `system/status {status: compacting}` and `system/compact_boundary`
    bracket a compaction; the compaction's own hollow result is
    swallowed.
"""

import json
import re
import time

from hands_free_voice.narrate import narrate_tool

VOICE_RE = re.compile(r"⟦voice⟧\s*(.*?)\s*⟦/voice⟧", re.DOTALL)

NO_VOICE_BLOCK = "No voice summary in this response."
TURN_FAILED = "The Claude turn failed."
COMPACTING = "Compacting."
COMPACTED = "Compacted."

# Spoken registers: what is said TO the user plays at full volume; what
# hands-free-voice says while working (tool narration) plays quieter, so the two
# are told apart by ear.
SPEECH = "speech"
NARRATION = "narration"

# A backgrounded task the seat never sees finish stops counting as live
# after this long, so a lost completion can never pin the process open.
TASK_MAX_S = 3 * 3600.0
TASK_DONE_STATUSES = frozenset({"completed", "stopped", "killed", "failed"})

# A completion injected into the running query arrives as a `user`
# record whose text opens with this block: the running query narrates
# its own continuation, so it is logged, never forwarded.
INJECTED_PREFIX = "<task-notification>"


def extract_voice(text: str) -> str | None:
    """The LAST well-formed ⟦voice⟧ block, or None (absent, empty or
    unterminated blocks are never read aloud as markup)."""
    blocks = [b.strip() for b in VOICE_RE.findall(text or "") if b.strip()]
    return blocks[-1] if blocks else None


def parse_stream_obj(obj) -> list[tuple]:
    """Low-level events from one decoded record, in content order:
      ("session", id)            the init record names the conversation
      ("tool", name, input)      one per main-chain tool call
      ("speak", text)            one per complete ⟦voice⟧ block
      ("context", used, window)  result usage -> window fill
      ("result", text, is_error) the closing record
    Anything else yields nothing."""
    if not isinstance(obj, dict):
        return []
    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "init":
        sid = obj.get("session_id")
        return [("session", sid)] if sid else []
    if kind == "assistant":
        if obj.get("parent_tool_use_id"):
            return []
        events: list[tuple] = []
        for b in (obj.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                events.append(("tool", b.get("name", ""), b.get("input") or {}))
            elif b.get("type") == "text":
                events += [("speak", m.strip())
                           for m in VOICE_RE.findall(b.get("text") or "")
                           if m.strip()]
        return events
    if kind == "result":
        events = []
        usage = obj.get("usage") or {}
        iters = usage.get("iterations")
        if isinstance(iters, list) and iters and isinstance(iters[-1], dict):
            usage = iters[-1]
        used = sum(usage.get(k) or 0 for k in
                   ("input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens"))
        windows = [m.get("contextWindow") or 0
                   for m in (obj.get("modelUsage") or {}).values()
                   if isinstance(m, dict)]
        if used and windows and max(windows):
            events.append(("context", used, max(windows)))
        result = obj.get("result")
        events.append(("result", result if isinstance(result, str) else "",
                       bool(obj.get("is_error"))))
        return events
    return []


def parse_stream_line(line: str) -> list[tuple]:
    try:
        obj = json.loads(line)
    except ValueError:
        return []
    return parse_stream_obj(obj)


def echo_text(obj: dict) -> str | None:
    """The text of a replayed user message, or None when the `user`
    record is a tool_result batch. Both content shapes occur on the wire:
    the block list we write comes back as blocks; the CLI's own synthetic
    messages (and a slash command's expansion) come back as a string."""
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    texts = []
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "text":
            return None
        texts.append(b.get("text") or "")
    return "".join(texts)


def is_plain_string_record(obj: dict) -> bool:
    return isinstance((obj.get("message") or {}).get("content"), str)


def is_empty_machine_result(origin: str | None, text: str) -> bool:
    """A result whose origin is present and not human AND whose text is
    empty: a query the CLI itself started that produced nothing. Logged,
    never spoken. A machine-started query WITH text is an answer like any
    other (a background agent's report)."""
    if not origin or origin == "human":
        return False
    return not text.strip()


# Roles: what a spoken line IS, for the replay cursor. "answer" is the
# ⟦voice⟧ content said to the user (a result's block, a mid-turn block);
# "closer" the percent or its fallback; "narration" a tool line;
# "status" hands-free-voice's own remarks (compaction marks, errors, acks).
ANSWER, CLOSER, NARRATION_ROLE, STATUS = "answer", "closer", "narration", "status"


def _say(text: str, register: str = SPEECH, role: str = ANSWER) -> dict:
    return {"text": text, "register": register, "role": role}


class Translator:
    """One instance per PROCESS run: a respawn starts a fresh one, so an
    old reader's tail never touches the new run's counters."""

    def __init__(self, clock=time.monotonic, closer_fallback: str = "Done."):
        self._clock = clock
        self.closer_fallback = closer_fallback
        self.query_open = False
        self.unconsumed = 0
        self.tasks: dict[str, float] = {}
        self.session_id: str | None = None
        self.last_activity = clock()
        self._compact_write_pending = False
        self._in_compaction = False
        # Per-query narration state.
        self._streamed: set[str] = set()
        self._last_pct: int | None = None

    # -- state the host reads ------------------------------------------

    @property
    def busy(self) -> bool:
        return self.query_open

    @property
    def compact_pending(self) -> bool:
        return self._compact_write_pending

    @property
    def last_pct(self) -> int | None:
        return self._last_pct

    def live_tasks(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        return sum(1 for t0 in self.tasks.values() if now - t0 <= TASK_MAX_S)

    @property
    def idle(self) -> bool:
        return not self.query_open and self.live_tasks() == 0

    # -- inputs from the host --------------------------------------------

    def submitted(self) -> None:
        self.unconsumed += 1
        self.last_activity = self._clock()

    def touch(self) -> None:
        self.last_activity = self._clock()

    def mark_compact_write(self) -> None:
        """A /compact of ours is on the wire: the CLI never echoes a slash
        command back as a consumed user record, so it joins no counter,
        and its own "Compacting." was said at write time."""
        self._compact_write_pending = True

    # -- the stream ------------------------------------------------------

    def record(self, line: str) -> list[dict]:
        """One stream-json line -> zero or more events, in order. Never
        raises on wire noise: a bad line is dropped."""
        try:
            obj = json.loads(line)
        except ValueError:
            return []
        if not isinstance(obj, dict):
            return []
        self.last_activity = self._clock()
        if obj.get("parent_tool_use_id"):
            return []
        kind = obj.get("type")
        if kind == "system":
            return self._system(obj)
        if kind == "user":
            return self._user(obj)
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "result":
            return self._result(obj)
        return []

    def process_exited(self, rc) -> list[dict]:
        """The process is gone: an honest error when it died with a query
        running or with messages written and never read, then the
        liveness event; every counter resets."""
        out = []
        if self.query_open or self.unconsumed or self._compact_write_pending:
            detail = f"claude exited (rc={rc})"
            if self.query_open:
                detail += " mid-query"
            owed = self._owed()
            if owed:
                detail += f" with {owed}"
            out.append({"kind": "error", "message": detail,
                        "say": [_say("Claude's process exited unexpectedly.", role=STATUS)]})
        out.append({"kind": "seat", "state": "exited", "rc": rc})
        self.query_open = False
        self.unconsumed = 0
        self.tasks.clear()
        self._clear_compact()
        self._reset_query()
        return out

    def idle_closed(self) -> list[dict]:
        out = []
        owed = self._owed()
        if owed:
            out.append({"kind": "error",
                        "message": f"claude idle-closed with {owed}",
                        "say": [_say("A message was never read before the"
                                     " idle close.", role=STATUS)]})
        out.append({"kind": "seat", "state": "idle_closed"})
        self.unconsumed = 0
        self._clear_compact()
        self._reset_query()
        return out

    # -- internals -------------------------------------------------------

    def _owed(self) -> str:
        parts = []
        if self.unconsumed:
            parts.append(f"{self.unconsumed} message(s) written, never read")
        if self._compact_write_pending:
            parts.append("a compact write unanswered")
        return " and ".join(parts)

    def _clear_compact(self) -> None:
        self._compact_write_pending = False
        self._in_compaction = False

    def _reset_query(self) -> None:
        self._streamed = set()
        self._last_pct = None

    def _system(self, obj: dict) -> list[dict]:
        sub = obj.get("subtype")
        if sub == "init":
            out = []
            sid = obj.get("session_id")
            if sid and sid != self.session_id:
                self.session_id = sid
                out.append({"kind": "session", "id": sid})
            self.query_open = True
            return out
        if sub == "task_started":
            if obj.get("owned_by_subagent") or not obj.get("is_backgrounded"):
                return []
            tid = obj.get("task_id")
            if tid:
                self.tasks[tid] = self._clock()
            return []
        if sub == "background_tasks_changed":
            tasks = obj.get("tasks")
            if isinstance(tasks, list):
                live = {t.get("task_id") for t in tasks if isinstance(t, dict)}
                for tid in [t for t in self.tasks if t not in live]:
                    del self.tasks[tid]
            return []
        if sub == "task_notification":
            tid = obj.get("task_id")
            status = obj.get("status")
            if tid and (status is None or status in TASK_DONE_STATUSES):
                self.tasks.pop(tid, None)
            return []
        if sub == "status" and obj.get("status") == "compacting":
            if self._compact_write_pending:
                return []  # ours: "Compacting." was said at write time
            return [{"kind": "say", "text": COMPACTING,
                     "say": [_say(COMPACTING, role=STATUS)]}]
        if sub == "compact_boundary":
            self._in_compaction = True
            return [{"kind": "say", "text": COMPACTED,
                     "say": [_say(COMPACTED, role=STATUS)]}]
        return []

    def _user(self, obj: dict) -> list[dict]:
        text = echo_text(obj)
        if text is None:
            return []
        if text.startswith(INJECTED_PREFIX):
            return [{"kind": "injected", "text": text}]
        if self._in_compaction and is_plain_string_record(obj):
            return []  # the compaction's own synthetic records
        self.query_open = True
        self.unconsumed = max(0, self.unconsumed - 1)
        return [{"kind": "consumed", "text": text}]

    def _assistant(self, obj: dict) -> list[dict]:
        out = []
        for ev in parse_stream_obj(obj):
            if ev[0] == "tool":
                _, name, inp = ev
                line = narrate_tool(name, inp)
                event = {"kind": "tool", "name": name, "input": inp}
                if line:
                    event["say"] = [_say(line, NARRATION, NARRATION_ROLE)]
                out.append(event)
            elif ev[0] == "speak":
                text = ev[1]
                self._streamed.add(text)
                out.append({"kind": "progress", "text": text,
                            "say": [_say(text)]})
        return out

    def _result(self, obj: dict) -> list[dict]:
        self.query_open = False
        self._compact_write_pending = False
        if self._in_compaction:
            self._in_compaction = False
            text = obj.get("result")
            textless = not isinstance(text, str) or not text.strip()
            if textless and not obj.get("origin") \
                    and obj.get("num_turns") == 0 \
                    and not obj.get("is_error"):
                self._reset_query()
                return []  # the compaction's own hollow result
        out = []
        for ev in parse_stream_obj(obj):
            if ev[0] == "context":
                _, used, window = ev
                self._last_pct = round(used * 100 / window)
                out.append({"kind": "context", "used": used,
                            "window": window, "pct": self._last_pct})
            elif ev[0] == "result":
                out.append(self._result_event(obj, ev[1], ev[2]))
        self._reset_query()
        return out

    def _result_event(self, obj: dict, text: str, is_error: bool) -> dict:
        event = {"kind": "result", "text": text, "is_error": is_error}
        duration_ms = obj.get("duration_ms")
        if isinstance(duration_ms, (int, float)) \
                and not isinstance(duration_ms, bool):
            event["elapsed_s"] = round(duration_ms / 1000.0, 3)
        num_turns = obj.get("num_turns")
        if num_turns is not None:
            event["num_turns"] = num_turns
        origin = obj.get("origin")
        origin_kind = origin.get("kind") if isinstance(origin, dict) else None
        if origin_kind:
            event["origin"] = origin_kind
        block = extract_voice(text)
        event["block"] = block
        closer = (f"{self._last_pct} percent." if self._last_pct is not None
                  else self.closer_fallback)
        event["closer"] = closer
        if is_empty_machine_result(origin_kind, text):
            event["say"] = []
            event["suppressed"] = "empty machine-origin result"
            return event
        lines: list[dict] = []
        if is_error:
            lines.append(_say(TURN_FAILED, role=STATUS))
        elif block is None:
            lines.append(_say(NO_VOICE_BLOCK, role=STATUS))
        elif block not in self._streamed:
            lines.append(_say(block))
        lines.append(_say(closer, role=CLOSER))
        event["say"] = lines
        return event
