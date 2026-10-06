"""The ModTranslator: the mod's turn events become the host's seat events,
with the same spoken lines the stream-json translator makes."""

from hands_free_voice.bridge import INTERRUPTED, ModTranslator
from hands_free_voice.translator import NO_VOICE_BLOCK, TURN_FAILED


def said(events):
    return [line["text"] for ev in events for line in ev.get("say") or []]


def test_a_turn_narrates_tools_speaks_the_block_and_closes_on_the_percent():
    t = ModTranslator()
    t.submitted()
    assert t.unconsumed == 1
    t.event({"kind": "start"})
    assert t.busy and t.unconsumed == 0
    assert said(t.event({"kind": "tool", "name": "Read",
                         "input": {"file_path": "/a/README.md"}})) == ["Reading README.md."]
    evs = t.event({"kind": "complete", "answer": "Long answer.\n⟦voice⟧All done.⟦/voice⟧",
                   "reason": "answer", "pct": 42.4, "elapsed_s": 3.2})
    assert said(evs) == ["All done.", "42 percent."]
    assert evs[0] == {"kind": "context", "pct": 42}
    assert evs[1]["elapsed_s"] == 3.2 and not t.busy


def test_a_block_streamed_mid_turn_is_not_repeated_at_the_end():
    t = ModTranslator()
    t.event({"kind": "start"})
    assert said(t.event({"kind": "text", "text": "x ⟦voice⟧Plan: fix it.⟦/voice⟧"})) == ["Plan: fix it."]
    assert said(t.event({"kind": "text", "text": "⟦voice⟧Fixed.⟦/voice⟧"})) == ["Fixed."]
    evs = t.event({"kind": "complete", "answer": "⟦voice⟧Fixed.⟦/voice⟧", "reason": "answer"})
    assert said(evs) == ["Done."]  # no usage known: the fallback closer


def test_no_block_failure_and_interrupt_each_say_so():
    t = ModTranslator()
    assert said(t.event({"kind": "complete", "answer": "plain", "reason": "answer"}))[0] == NO_VOICE_BLOCK
    assert said(t.event({"kind": "complete", "answer": "", "reason": "error"}))[0] == TURN_FAILED
    assert said(t.event({"kind": "complete", "answer": "", "reason": "aborted"})) == [INTERRUPTED]


def test_unknown_unnarrated_and_malformed_events_are_quiet():
    t = ModTranslator()
    assert t.event({"kind": "nope"}) == [] and t.event("not a dict") == []
    assert said(t.event({"kind": "tool", "name": "TodoWrite", "input": None})) == []
    # text with no voice block says nothing, but still reports the work
    assert t.event({"kind": "text", "text": "Reading the logs."}) == [{"kind": "text"}]
    assert t.event({"kind": "notice", "text": "  "}) == []


def test_a_notice_from_the_mod_is_spoken():
    t = ModTranslator()
    assert said(t.event({"kind": "notice", "text": "The voice instructions couldn't be added."})) == \
        ["The voice instructions couldn't be added."]
    t.mark_compact_write()
    assert t.compact_pending
    assert said(t.event({"kind": "compacted"})) == ["Compacted."] and not t.compact_pending


def test_the_spinner_word_is_kept_and_is_no_sign_of_work():
    t = ModTranslator()
    assert t.spinner_word is None
    assert t.event({"kind": "spinner", "word": "Sautéing"}) == []
    assert t.event({"kind": "spinner", "word": "  "}) == []
    assert t.spinner_word == "Sautéing"


def test_a_read_delivery_clears_the_unconsumed_count():
    t = ModTranslator()
    t.submitted()
    t.event({"kind": "read"})
    t.event({"kind": "read"})
    assert t.unconsumed == 0
