"""The TurnMachine: framing, the closer's silence window, commands."""

from hands_free_voice.turns import COMMANDS, SHOWN_COMMANDS, TurnMachine, command_for


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def machine(**kw) -> TurnMachine:
    return TurnMachine(clock=Clock(), **kw)


def test_unaddressed_speech_is_dropped():
    m = machine()
    # the church test: another person in the room reached the terminal once
    assert m.feed("Hi, are you still at church?") == \
        [("drop", "Hi, are you still at church?")]
    assert m.state == m.IDLE


def test_single_final_turn_closes_after_silence():
    m = machine()
    acts = m.feed("Operator, ship the tail matcher. Over.")
    assert acts == [("open", "ship the tail matcher. Over."),
                    ("closing", "ship the tail matcher.")]
    assert m.state == m.CLOSING
    assert m.settle() == [("dispatch", "ship the tail matcher.")]
    assert m.state == m.IDLE
    assert m.settle() == []  # idempotent


def test_multi_final_turn():
    m = machine()
    assert m.feed("Operator.") == [("open", None)]
    assert m.feed("Let's fix the wake tests.") == [("append", "Let's fix the wake tests.")]
    assert m.feed("Over.") == [("append", "Over."), ("closing", "Let's fix the wake tests.")]
    assert m.settle() == [("dispatch", "Let's fix the wake tests.")]


def test_more_speech_in_the_settle_window_makes_the_closer_content():
    # "I'm gonna bring that over to..." once closed a turn mid-sentence
    m = machine()
    m.feed("Operator, I'm gonna bring that over")
    assert m.state == m.CLOSING
    acts = m.feed("to the other file, over")
    assert acts == [("append", "to the other file, over"),
                    ("closing", "I'm gonna bring that over to the other file,")]
    assert m.settle() == [("dispatch", "I'm gonna bring that over to the other file,")]


def test_mid_sentence_mention_of_the_closer_is_content():
    m = machine()
    m.feed("Operator.")
    assert m.feed("we drove over the bridge and kept going") == \
        [("append", "we drove over the bridge and kept going")]
    assert m.state == m.OPEN


def test_address_counts_only_as_the_first_word():
    m = machine()
    assert m.feed("I told the operator about it yesterday. Over.") == \
        [("drop", "I told the operator about it yesterday. Over.")]
    assert m.feed("um, operator, run the suite over") == \
        [("drop", "um, operator, run the suite over")]
    # inside an open turn a mention is content
    m.feed("Operator, tell me what the operator said")
    assert m.state == m.OPEN
    assert m.feed("about it. Over.")[-1] == \
        ("closing", "tell me what the operator said about it.")


def test_filler_before_address_knob():
    m = machine(filler_before_address=True)
    acts = m.feed("um, operator, run the suite over")
    assert acts == [("open", "run the suite over"), ("closing", "run the suite")]


def test_bare_address_plus_closer_is_inert():
    m = machine()
    assert m.feed("Operator over.") == [("drop", "Operator over.")]
    assert m.state == m.IDLE
    m.feed("Operator.")
    assert m.feed("Over.") == [("append", "Over."), ("closing", "")]
    assert m.settle() == [("dispatch", "")]


def test_commands_never_open_a_turn():
    m = machine()
    assert m.feed("Operator status") == [("command", "status", None)]
    assert m.feed("Operator, say that again.") == [("command", "again", None)]
    assert m.feed("Operator repeat") == [("command", "again", None)]
    assert m.feed("Operator stop") == [("command", "stop", None)]
    assert m.feed("Operator resume") == [("command", "resume", None)]
    assert m.feed("Operator, quit.") == [("command", "quit", None)]
    assert m.feed("Operator, compact yourself.") == [("command", "compact", None)]
    assert m.feed("Operator, confirm.") == [("command", "confirm", None)]
    assert m.feed("Operator confirmed") == [("command", "confirm", None)]
    assert m.state == m.IDLE


def test_command_for():
    assert command_for("stop") == ("stop", None)
    assert command_for("again") == ("again", None)
    assert command_for("repeat") == ("again", None)
    assert command_for("cancel") == ("cancel", None)
    assert command_for("never mind") == ("cancel", None)
    assert command_for("nevermind") == ("cancel", None)
    # "back N" was retired: no phrase names a replay count anymore
    assert command_for("back") is None
    assert command_for("back two") is None
    assert command_for("back to") is None
    assert command_for("back 7") is None
    assert command_for("back somewhere") is None
    assert command_for("run the tests") is None


def test_back_n_no_longer_fires_a_command():
    # "back two" isn't recognized anymore: it's ordinary dictated
    # content, so it opens a turn like any other speech would.
    m = machine()
    assert m.feed("Operator back two") == [("open", "back two")]
    assert m.state == m.OPEN
    assert m.feed("over")[-1] == ("closing", "back two")
    assert m.settle() == [("dispatch", "back two")]
    assert m.state == m.IDLE


def test_stop_fires_anywhere_other_commands_need_the_first_word():
    m = machine()
    assert m.feed("um operator stop") == [("command", "stop", None)]
    assert m.feed("uh, Operator, stop.") == [("command", "stop", None)]
    # a false quit would be fatal: it stays content/drop unless first
    assert m.feed("and then the operator quit") == \
        [("drop", "and then the operator quit")]
    assert m.feed("Operator quit") == [("command", "quit", None)]
    # stop inside an open turn fires without polluting the turn
    m.feed("Operator, half a thought")
    assert m.feed("um operator stop") == [("command", "stop", None)]
    assert m.state == m.OPEN
    assert m.feed("finish it, over")[-1] == ("closing", "half a thought finish it,")


def test_cancel_discards_the_open_turn():
    m = machine()
    m.feed("Operator, half a thought")
    assert m.feed("Operator, never mind.") == \
        [("abandoned", "half a thought"), ("command", "cancel", None)]
    assert m.feed("so anyway") == [("drop", "so anyway")]
    # cancel with nothing captured reports no loss
    m.feed("Operator.")
    assert m.feed("Operator cancel") == [("command", "cancel", None)]
    assert m.state == m.IDLE
    # "nevermind" (dictation often joins the two words) is the same alias
    m.feed("Operator, another thought")
    assert m.feed("Operator, nevermind.") == \
        [("abandoned", "another thought"), ("command", "cancel", None)]
    assert m.state == m.IDLE


def test_command_word_inside_a_sentence_is_content():
    m = machine()
    acts = m.feed("Operator, I need to compact you soon, over")
    assert ("command", "compact", None) not in acts
    assert acts[-1] == ("closing", "I need to compact you soon,")
    m.settle()
    m.feed("Operator, remember this")
    assert m.feed("compact") == [("append", "compact")]
    m.feed("Operator, make stop the default and quit early")
    assert m.state == m.OPEN


def test_split_final_commands_ride_one_final_of_grace():
    m = machine()
    assert m.feed("Operator.") == [("open", None)]
    assert m.feed("Stop.") == [("command", "stop", None)]
    assert m.state == m.IDLE  # the pair was a command, not a turn
    # mid-turn: the trailing address was the command's, popped from the buffer
    m.feed("Operator, fix the wake tests operator")
    assert m.feed("stop") == [("command", "stop", None)]
    assert m.state == m.OPEN
    assert m.feed("over")[-1] == ("closing", "fix the wake tests")
    m.settle()
    # the grace lasts exactly one final
    m.feed("Operator.")
    m.feed("let me think")
    assert m.feed("stop") == [("append", "stop")]
    m.feed("Operator, never mind.")
    # the address restated before the command rides the inline path and
    # closes the empty open with it
    m.feed("Operator.")
    assert m.feed("operator stop") == [("command", "stop", None)]
    assert m.state == m.IDLE
    assert m.feed("unrelated chatter") == [("drop", "unrelated chatter")]
    # a non-command next final: the closer still closes, the armed
    # trailing address stays as content debris
    m2 = machine()
    m2.feed("Operator, ship it operator")
    assert m2.feed("over") == [("append", "over"), ("closing", "ship it operator")]


def test_readdress_after_a_gap_discards_the_open_turn():
    clock = Clock(0.0)
    m = TurnMachine(clock=clock, readdress_gap_s=2.0)
    m.feed("Operator, first thought")
    clock.t = 1.0
    # within the gap: the re-address is content
    assert m.feed("operator and more") == [("append", "operator and more")]
    clock.t = 10.0
    acts = m.feed("Operator, fresh start, over")
    assert acts == [("abandoned", "first thought operator and more"),
                    ("open", "fresh start, over"), ("closing", "fresh start,")]
    assert m.settle() == [("dispatch", "fresh start,")]
    # a bare open that is re-addressed after a gap reports no loss
    m.feed("Operator.")
    clock.t = 20.0
    assert m.feed("Operator, hello over") == [("open", "hello over"), ("closing", "hello")]


def test_abandon_stale():
    clock = Clock(0.0)
    m = TurnMachine(clock=clock, idle_turn_s=60.0)
    m.feed("Operator, half a")
    clock.t = 30.0
    assert m.abandon_stale() == []
    clock.t = 61.0
    assert m.abandon_stale() == [("abandoned", "half a")]
    assert m.state == m.IDLE
    # a closing turn is never stale: settle owns it
    m.feed("Operator, done, over")
    clock.t = 200.0
    assert m.abandon_stale() == []
    assert m.settle() == [("dispatch", "done,")]


def test_configured_words():
    m = TurnMachine(address="computer", closer="send it", clock=Clock())
    assert m.feed("Computer, do the thing, send it") == \
        [("open", "do the thing, send it"), ("closing", "do the thing,")]
    assert m.feed("Computer stop") == [("command", "stop", None)]
    assert m.state == m.CLOSING  # a command never discards a real turn
    assert m.settle() == [("dispatch", "do the thing,")]
    assert m.feed("operator hello over") == [("drop", "operator hello over")]


def test_the_status_line_shows_one_phrase_for_every_command():
    assert sorted(COMMANDS[p] for p in SHOWN_COMMANDS) == sorted(set(COMMANDS.values()))


def test_a_partial_previews_the_turn_without_changing_it():
    m = machine()
    assert m.preview("so anyway") is None            # unaddressed
    assert m.preview("Operator") == ""               # the address alone: a turn is starting
    assert m.preview("Operator, check the") == "check the"
    assert m.preview("Operator stop") is None        # a command, not a turn
    m.feed("Operator, check the weather")
    assert m.preview("in Houston") == "check the weather in Houston"
    assert m.text() == "check the weather"
