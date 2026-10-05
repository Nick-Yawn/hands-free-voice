"""The Translator against stream-json records shaped like the CLI's own."""

import json

from hands_free_voice.translator import (
    NO_VOICE_BLOCK,
    TURN_FAILED,
    Translator,
    echo_text,
    extract_voice,
    is_empty_machine_result,
    parse_stream_line,
)

SID = "db54779a-0000-4000-8000-000000000000"


def j(obj) -> str:
    return json.dumps(obj) + "\n"


def init(sid=SID):
    return j({"type": "system", "subtype": "init", "session_id": sid})


def echo(text):
    return j({"type": "user", "session_id": SID, "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}})


def echo_str(text):
    return j({"type": "user", "session_id": SID,
              "message": {"role": "user", "content": text}})


def tool_result_batch():
    return j({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}})


def assistant_tool(name="Read", inp=None, side=False):
    rec = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": name,
         "input": inp if inp is not None else {"file_path": "a.py"}}]}}
    if side:
        rec["parent_tool_use_id"] = "toolu_parent"
    return j(rec)


def assistant_text(text):
    return j({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}})


def result(text="done", is_error=False, used=None, window=None,
           num_turns=None, duration_ms=0, origin=None, iterations=None):
    rec = {"type": "result", "subtype": "success", "is_error": is_error,
           "result": text, "session_id": SID}
    if num_turns is not None:
        rec["num_turns"] = num_turns
    if used is not None:
        rec["usage"] = {"input_tokens": used, "output_tokens": 0}
        if iterations is not None:
            rec["usage"]["iterations"] = iterations
        rec["modelUsage"] = {"m": {"contextWindow": window}}
    if duration_ms is not None:
        rec["duration_ms"] = duration_ms
    if origin is not None:
        rec["origin"] = {"kind": origin}
    return j(rec)


def status_compacting():
    return j({"type": "system", "subtype": "status", "status": "compacting"})


def compact_boundary():
    return j({"type": "system", "subtype": "compact_boundary",
              "compact_metadata": {"trigger": "manual", "pre_tokens": 21674,
                                   "post_tokens": 1980}})


def compact_result(is_error=False):
    return j({"type": "result", "subtype": "success", "is_error": is_error,
              "result": "", "session_id": SID, "num_turns": 0,
              "duration_ms": 22949, "usage": {"input_tokens": 0,
                                               "output_tokens": 0},
              "modelUsage": {"m": {"contextWindow": 1000000}}})


def task_started(tid, owned_by_subagent=False, backgrounded=True):
    rec = {"type": "system", "subtype": "task_started", "task_id": tid,
           "is_backgrounded": backgrounded}
    if owned_by_subagent:
        rec["owned_by_subagent"] = True
    return j(rec)


def task_notification(tid, status="completed"):
    return j({"type": "system", "subtype": "task_notification",
              "task_id": tid, "status": status})


def tasks_changed(*tids):
    return j({"type": "system", "subtype": "background_tasks_changed",
              "tasks": [{"task_id": t} for t in tids]})


def injected(tid):
    return echo_str(f"<task-notification>\n<task-id>{tid}</task-id>")


def feed(tr, *lines):
    out = []
    for line in lines:
        out += tr.record(line)
    return out


def said(events):
    return [(s["text"], s["register"]) for ev in events for s in ev.get("say", [])]


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


# -- pure helpers -------------------------------------------------------

def test_extract_voice():
    body = "Full detail here.\n\n⟦voice⟧\nOne decision needs you.\n⟦/voice⟧"
    assert extract_voice(body) == "One decision needs you."
    assert extract_voice("no block at all") is None
    assert extract_voice("⟦voice⟧first⟦/voice⟧ middle ⟦voice⟧second⟦/voice⟧") == "second"
    assert extract_voice("⟦voice⟧ runs off the end...") is None
    assert extract_voice("⟦voice⟧   ⟦/voice⟧") is None
    assert extract_voice("") is None


def test_parse_stream_line_shapes():
    tool = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "thinking aloud"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}}]}})
    assert parse_stream_line(tool) == [("tool", "Read", {"file_path": "/a/b.py"})]
    assert parse_stream_line(json.dumps({"type": "result", "result": "done",
                                         "is_error": False})) == [("result", "done", False)]
    assert parse_stream_line(json.dumps({"type": "result", "is_error": True})) == \
        [("result", "", True)]
    assert parse_stream_line("not json at all") == []
    assert parse_stream_line(json.dumps({"type": "system"})) == []
    assert parse_stream_line(json.dumps(["a", "list"])) == []
    assert parse_stream_line(init()) == [("session", SID)]
    plan = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Full prose.\n⟦voice⟧\nPlan: fix, then test.\n⟦/voice⟧"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}})
    assert parse_stream_line(plan) == [("speak", "Plan: fix, then test."),
                                       ("tool", "Edit", {"file_path": "a.py"})]


def test_context_uses_the_last_usage_iteration():
    rich = json.dumps({
        "type": "result", "result": "done", "is_error": False,
        "usage": {"input_tokens": 100, "output_tokens": 900,
                  "cache_read_input_tokens": 40_000,
                  "cache_creation_input_tokens": 9_000},
        "modelUsage": {"claude": {"contextWindow": 1_000_000}}})
    assert parse_stream_line(rich) == [("context", 50_000, 1_000_000),
                                       ("result", "done", False)]
    # cumulative multi-round usage once read "108 percent": the last
    # iteration is the truth
    rounds = json.dumps({
        "type": "result", "result": "done", "is_error": False,
        "usage": {"input_tokens": 300, "output_tokens": 3_000,
                  "cache_read_input_tokens": 900_000,
                  "cache_creation_input_tokens": 200_000,
                  "iterations": [
                      {"input_tokens": 100, "output_tokens": 1_000,
                       "cache_read_input_tokens": 300_000,
                       "cache_creation_input_tokens": 100_000},
                      {"input_tokens": 200, "output_tokens": 2_000,
                       "cache_read_input_tokens": 380_000,
                       "cache_creation_input_tokens": 20_000}]},
        "modelUsage": {"claude": {"contextWindow": 1_000_000}}})
    assert parse_stream_line(rounds) == [("context", 402_200, 1_000_000),
                                         ("result", "done", False)]


def test_sidechain_records_are_skipped():
    sidechain = json.dumps({
        "type": "assistant", "parent_tool_use_id": "toolu_123",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}},
            {"type": "text", "text": "⟦voice⟧sidechain progress⟦/voice⟧"}]}})
    assert parse_stream_line(sidechain) == []
    assert Translator().record(sidechain) == []
    main_chain = json.dumps({
        "type": "assistant", "parent_tool_use_id": None,
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"description": "run"}}]}})
    assert parse_stream_line(main_chain) == [("tool", "Bash", {"description": "run"})]


def test_echo_text_shapes():
    assert echo_text(json.loads(echo("hello there"))) == "hello there"
    assert echo_text(json.loads(echo_str("plain"))) == "plain"
    assert echo_text(json.loads(tool_result_batch())) is None
    assert echo_text({"message": {}}) is None


def test_is_empty_machine_result():
    assert is_empty_machine_result("task-notification", "") is True
    assert is_empty_machine_result("task-notification", "   ") is True
    assert is_empty_machine_result("task-notification", "a report") is False
    assert is_empty_machine_result(None, "") is False
    assert is_empty_machine_result("human", "") is False


# -- the translator -----------------------------------------------------

def test_init_pins_the_session_and_opens_a_query():
    tr = Translator()
    assert feed(tr, init()) == [{"kind": "session", "id": SID}]
    assert tr.session_id == SID and tr.busy
    assert feed(tr, init()) == []


def test_echo_is_consumed_and_counted():
    tr = Translator()
    tr.submitted()
    tr.submitted()
    assert tr.unconsumed == 2
    assert feed(tr, echo("run the suite")) == [{"kind": "consumed", "text": "run the suite"}]
    assert tr.unconsumed == 1
    assert feed(tr, echo_str("plain")) == [{"kind": "consumed", "text": "plain"}]
    assert feed(tr, echo("more")) == [{"kind": "consumed", "text": "more"}]
    assert tr.unconsumed == 0  # floored
    assert feed(tr, tool_result_batch()) == []


def test_injected_task_notification_is_reported_not_consumed():
    tr = Translator()
    tr.submitted()
    out = feed(tr, injected("t1"))
    assert out[0]["kind"] == "injected" and "say" not in out[0]
    assert tr.unconsumed == 1


def test_tools_narrate_at_the_narration_register():
    tr = Translator()
    out = feed(tr, assistant_tool("Read", {"file_path": "a.py"}))
    assert out == [{"kind": "tool", "name": "Read", "input": {"file_path": "a.py"},
                    "say": [{"text": "Reading a.py.", "register": "narration", "role": "narration"}]}]
    silent = feed(tr, assistant_tool("TodoWrite", {}))
    assert silent == [{"kind": "tool", "name": "TodoWrite", "input": {}}]


def test_progress_blocks_speak_and_dedupe_the_closing_block():
    tr = Translator()
    out = feed(tr, init(), assistant_text("Plan.\n⟦voice⟧Plan: fix then test.⟦/voice⟧"))
    assert said(out) == [("Plan: fix then test.", "speech")]
    # the same block closing the result is not read twice; the closer is
    res = feed(tr, result("Long answer.\n⟦voice⟧Plan: fix then test.⟦/voice⟧",
                          used=1200, window=10000, num_turns=3, duration_ms=4200))
    assert res[0] == {"kind": "context", "used": 1200, "window": 10000, "pct": 12}
    assert res[1]["kind"] == "result"
    assert res[1]["block"] == "Plan: fix then test."
    assert res[1]["closer"] == "12 percent."
    assert res[1]["elapsed_s"] == 4.2 and res[1]["num_turns"] == 3
    assert said(res) == [("12 percent.", "speech")]
    assert not tr.busy


def test_result_speaks_block_then_percent_closer():
    tr = Translator()
    feed(tr, init())
    out = feed(tr, result("Full text.\n⟦voice⟧All four tests pass now.⟦/voice⟧",
                          used=3200, window=10000))
    assert said(out) == [("All four tests pass now.", "speech"),
                         ("32 percent.", "speech")]
    assert "origin" not in out[-1]


def test_result_without_usage_uses_the_fallback_closer():
    tr = Translator(closer_fallback="Done.")
    out = feed(tr, init(), result("⟦voice⟧Hi.⟦/voice⟧"))
    assert said(out) == [("Hi.", "speech"), ("Done.", "speech")]
    assert out[-1]["closer"] == "Done."


def test_result_without_a_block_speaks_the_placeholder():
    tr = Translator()
    out = feed(tr, init(), result("plain prose only", used=500, window=10000))
    assert said(out) == [(NO_VOICE_BLOCK, "speech"), ("5 percent.", "speech")]


def test_error_result_says_so():
    tr = Translator()
    out = feed(tr, init(), result("boom", is_error=True))
    assert said(out) == [(TURN_FAILED, "speech"), ("Done.", "speech")]
    assert out[-1]["is_error"] is True


def test_empty_machine_origin_result_is_not_spoken():
    tr = Translator()
    out = feed(tr, init(), result("", num_turns=0, origin="task-notification"))
    assert out[-1]["kind"] == "result" and out[-1]["origin"] == "task-notification"
    assert out[-1]["say"] == [] and out[-1]["suppressed"]
    # a machine-started query WITH text is an answer like any other
    out2 = feed(tr, init(), result("⟦voice⟧The agent finished.⟦/voice⟧",
                                   origin="task-notification", used=100, window=1000))
    assert said(out2) == [("The agent finished.", "speech"), ("10 percent.", "speech")]


def test_query_boundary_resets_streamed_blocks_and_percent():
    tr = Translator()
    feed(tr, init(), assistant_text("⟦voice⟧Same words.⟦/voice⟧"))
    feed(tr, result("", num_turns=0, origin="task-notification"))  # suppressed
    # the next query must not think "Same words." already streamed
    out = feed(tr, init(), result("⟦voice⟧Same words.⟦/voice⟧"))
    assert said(out) == [("Same words.", "speech"), ("Done.", "speech")]


def test_non_numeric_duration_ms_survives():
    tr = Translator()
    out = feed(tr, result("ok", used=1000, window=10000, num_turns=2, duration_ms="4200"))
    assert out[0]["kind"] == "context"
    assert out[1]["kind"] == "result" and "elapsed_s" not in out[1]
    out2 = feed(Translator(), result("ok", duration_ms=True))
    assert "elapsed_s" not in out2[0]


def test_compaction_marks():
    tr = Translator()
    feed(tr, result("earlier", used=8000, window=10000))
    # an unprompted (automatic) compaction narrates both marks
    assert said(feed(tr, status_compacting())) == [("Compacting.", "speech")]
    assert said(feed(tr, compact_boundary())) == [("Compacted.", "speech")]
    assert feed(tr, echo_str("continuing...")) == []  # synthetic, not an echo
    assert feed(tr, compact_result()) == []  # the hollow result is swallowed
    assert not tr.busy
    # a compact we wrote already said "Compacting." at write time
    tr.mark_compact_write()
    assert tr.compact_pending
    assert feed(tr, status_compacting()) == []
    assert said(feed(tr, init(), compact_boundary())) == [("Compacted.", "speech")]
    assert feed(tr, compact_result()) == []
    assert not tr.compact_pending


def test_compaction_never_swallows_a_real_query_result():
    tr = Translator()
    feed(tr, status_compacting(), compact_boundary())
    real = feed(tr, result("", used=700, window=10000, num_turns=1))
    assert [e["kind"] for e in real] == ["context", "result"]
    err = Translator()
    err.mark_compact_write()
    feed(err, status_compacting(), compact_boundary())
    out = feed(err, compact_result(is_error=True))
    assert out[-1]["kind"] == "result" and out[-1]["is_error"]


def test_compact_write_flag_clears_at_any_result():
    tr = Translator()
    tr.mark_compact_write()
    feed(tr, status_compacting())
    feed(tr, result("", num_turns=0))
    assert not tr.compact_pending
    assert said(feed(tr, status_compacting())) == [("Compacting.", "speech")]


def test_live_tasks_and_idle_rule():
    clock = Clock()
    tr = Translator(clock=clock)
    assert feed(tr, task_started("t1")) == []
    assert tr.idle is False
    feed(tr, task_started("t2", owned_by_subagent=True))
    assert "t2" not in tr.tasks
    feed(tr, task_started("t4", backgrounded=False))
    assert "t4" not in tr.tasks
    feed(tr, tasks_changed())
    assert tr.idle is True
    feed(tr, task_started("t1"), task_notification("t1", "completed"))
    assert tr.idle is True
    feed(tr, task_started("t3"))
    clock.t += 3 * 3600 + 1
    assert tr.live_tasks() == 0 and tr.idle


def test_process_exit_and_idle_close_report_honestly():
    tr = Translator()
    tr.query_open = True
    tr.submitted()
    out = tr.process_exited(1)
    assert out[0]["kind"] == "error"
    assert out[0]["message"] == "claude exited (rc=1) mid-query with 1 message(s) written, never read"
    assert out[0]["say"]
    assert out[1] == {"kind": "seat", "state": "exited", "rc": 1}
    assert not tr.busy and tr.unconsumed == 0
    assert Translator().process_exited(0) == [{"kind": "seat", "state": "exited", "rc": 0}]
    tr2 = Translator()
    tr2.mark_compact_write()
    assert tr2.process_exited(3)[0]["message"] == \
        "claude exited (rc=3) with a compact write unanswered"
    tr3 = Translator()
    tr3.submitted()
    out3 = tr3.idle_closed()
    assert out3[0]["kind"] == "error" and out3[1] == {"kind": "seat", "state": "idle_closed"}
    assert Translator().idle_closed() == [{"kind": "seat", "state": "idle_closed"}]


def test_replay_specimen_two_results_in_order_no_error():
    # On --resume the CLI once replayed a stale task_notification and ran
    # an empty wake-up query before consuming stdin. Both results simply
    # forward in order; the phantom is not spoken.
    tr = Translator()
    tr.submitted()
    out = feed(tr, init(), result("", num_turns=0, origin="task-notification"),
               init(), echo("x"), assistant_text("plain text, no block"),
               result("the real answer", num_turns=1, duration_ms=500))
    assert [o["kind"] for o in out] == ["session", "result", "consumed", "result"]
    assert out[1]["say"] == []
    assert said(out) == [(NO_VOICE_BLOCK, "speech"), ("Done.", "speech")]
