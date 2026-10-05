"""The voice loop end to end, offline: fake STT events in, a scripted
claude child, a fake TTS, and a playback with a fake stream so earcons
are observable."""

import asyncio

from hands_free_voice import earcons
from hands_free_voice.app import Host
from hands_free_voice.audio import Playback
from hands_free_voice.config import DEFAULTS, deep_merge
from hands_free_voice.providers import Error, Final, Partial, SpeechStarted
from hands_free_voice.providers.fake import FakeSTT, FakeTTS, FakeVAD
from hands_free_voice.seat import Seat
from hands_free_voice.state import EventLog, LockFile, SessionPin
from hands_free_voice.voice import run_voice
from tests.fakes import ScriptedClaude, assistant_tool, result, until


class FakeOutStream:
    def __init__(self):
        self.writes = []

    def start(self):
        pass

    def write(self, pcm):
        self.writes.append(pcm)

    def abort(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


VOICED = b"\x01\x00" * 640   # 40 ms the FakeVAD calls speech
SILENT = bytes(1280)


class FakeMic:
    rate = 16000

    def __init__(self):
        self.last_frame_t = None
        self.last_live_t = None
        self.started = 0
        self.stopped = 0
        self.on_frame = None
        self.device_name = "fake mic"
        self.device_rate = 16000

    def start(self, loop, on_frame):
        self.started += 1
        self.on_frame = on_frame

    def stop(self):
        self.stopped += 1
        self.on_frame = None


async def keep_talking(mic: FakeMic, run: asyncio.Future) -> None:
    """Voice on the mic for the whole test, so the gate opens at once
    and stays open; the gate's own timing has its own tests."""
    while not run.done():
        if mic.on_frame is not None:
            mic.on_frame(VOICED)
        await asyncio.sleep(0.01)


CUES = earcons.get_set()


def cue_names(stream: FakeOutStream) -> list[str]:
    by_bytes = {v: k for k, v in CUES.items()}
    out = []
    for w in stream.writes:
        name = by_bytes.get(w)
        if name:
            out.append(name)
    return out


def build(tmp_path, answers, overlay=None, tts=None, states=None):
    # tts.voice stays "" here: these scenarios are about turns and
    # commands, not which voice id gets threaded through, and pinning it
    # keeps their `(text, None)` assertions independent of DEFAULTS'
    # shipped voice id.
    cfg = deep_merge(DEFAULTS, {"turns": {"closer_settle_s": 0.03},
                                "volumes": {"earcons": 1.0},
                                "seat": {"still_here_s": 60},
                                "tts": {"voice": ""},
                                "gate": {"hangover_s": 60, "empty_hangover_s": 60,
                                         "deaf_s": 0}, **(overlay or {})})
    out = []
    host = Host(cfg, "/proj", log=EventLog(tmp_path / "log.jsonl"), out=out.append)
    claude = ScriptedClaude(answers)
    pin = SessionPin(tmp_path / "session_id")
    lock = LockFile(tmp_path / "lock")
    lock.acquire(None)
    seat = Seat("/proj", contract_path="/c.md", session_pin=pin,
                on_event=host.on_seat_event, spawn=claude.spawn, env={},
                lock=lock, log=host.log)
    stt = FakeSTT()
    tts = tts or FakeTTS()
    stream = FakeOutStream()
    playback = Playback(enabled=True, open_stream=lambda: stream)
    mic = FakeMic()
    run = asyncio.ensure_future(run_voice(
        host, seat, cfg, stt=stt, tts=tts, playback=playback, mic=mic, vad=FakeVAD(),
        on_state=None if states is None else states.append))
    mic.talker = asyncio.ensure_future(keep_talking(mic, run))
    return host, seat, claude, stt, tts, stream, mic, out, run


def test_address_talk_closer_round_trip(tmp_path):
    answers = {"say hi": [assistant_tool("Read", {"file_path": "README.md"}),
                          result("Hi.\n⟦voice⟧Hi there, operator.⟦/voice⟧",
                                 used=1000, window=10000)]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        assert stt.opened_with[0]["keyterms"] == ["operator", "over"]
        await until(lambda: "connected" in cue_names(stream))
        stt.queue.put_nowait(Final("Hi, are you still at church?"))
        stt.queue.put_nowait(Final("Operator, say hi"))
        await until(lambda: host.spoken.holds == frozenset({"talk"}))
        assert "capture" in cue_names(stream)
        stt.queue.put_nowait(Final("over"))
        await until(lambda: "dispatch" in cue_names(stream))
        assert not host.spoken.paused
        await until(lambda: any(t == "10 percent." for t, _ in tts.spoken))
        # scrubbed at the chokepoint: the address word never reaches the voice
        assert [t for t, _ in tts.spoken] == ["Received.", "Reading read me.md.",
                                              "Hi there, op.", "10 percent."]
        assert any("ignored: Hi, are you still at church?" in line for line in out)
        assert "you ▸ say hi" in out and "→ sent: say hi" in out
        stt.queue.put_nowait(Final("Operator quit"))
        await run
        assert out[-1] == "[session closed]" and not seat.alive and mic.stopped >= 1

    asyncio.run(scenario())


def test_closer_mid_sentence_is_content_when_speech_continues(tmp_path):
    answers = {"bring that over to the other file": [result("⟦voice⟧Moved.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, bring that over"))
        await until(lambda: host.spoken.holds == frozenset({"talk"}))
        stt.queue.put_nowait(Partial("to the"))  # inside the settle window
        await asyncio.sleep(0.05)
        assert "dispatch" not in cue_names(stream)
        stt.queue.put_nowait(Final("to the other file over"))
        await until(lambda: "dispatch" in cue_names(stream))
        await until(lambda: ("Moved.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_speech_started_cancels_the_settle_and_a_silent_final_rearms(tmp_path):
    answers = {"ship it": [result("⟦voice⟧Shipped.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, ship it over"))
        stt.queue.put_nowait(SpeechStarted())
        await asyncio.sleep(0.05)
        assert "dispatch" not in cue_names(stream)  # the onset held the closer
        stt.queue.put_nowait(Final("   "))  # a silent final re-arms the window
        await until(lambda: "dispatch" in cue_names(stream))
        await until(lambda: ("Shipped.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_talking_pauses_playback_and_commands_drive_the_cursor(tmp_path):
    answers = {"talk": [result("⟦voice⟧A long spoken answer that keeps going.⟦/voice⟧",
                               used=100, window=1000)]}
    tts = FakeTTS(chunk_delay_s=0.02, chunks=50)

    async def scenario():
        host, seat, claude, stt, tts_, stream, mic, out, run = build(tmp_path, answers, tts=tts)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, talk over"))
        await until(lambda: any(t.startswith("A long spoken") for t, _ in tts.spoken))
        # the address word pauses the answer mid-line
        stt.queue.put_nowait(Final("Operator."))
        await until(lambda: tts.cancelled == ["A long spoken answer that keeps going."])
        assert host.spoken.paused
        # cancelling the turn resumes from the start of that line
        stt.queue.put_nowait(Final("Operator cancel"))
        await until(lambda: [t for t, _ in tts.spoken].count(
            "A long spoken answer that keeps going.") == 2)
        assert not host.spoken.paused
        # "operator stop" holds; "operator resume" releases
        stt.queue.put_nowait(Final("um operator stop"))
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        stt.queue.put_nowait(Final("Operator resume"))
        await until(lambda: not host.spoken.paused)
        # "again" and its "repeat" alias replay the ANSWER (through its
        # closer), never the closer alone
        await until(lambda: ("10 percent.", None) in tts.spoken)
        await until(lambda: not host.spoken.busy)
        n = len(tts.spoken)
        stt.queue.put_nowait(Final("Operator again"))
        await until(lambda: len(tts.spoken) == n + 2)
        assert [t for t, _ in tts.spoken[-2:]] == \
            ["A long spoken answer that keeps going.", "10 percent."]
        await until(lambda: not host.spoken.busy)
        stt.queue.put_nowait(Final("Operator repeat"))
        await until(lambda: len(tts.spoken) == n + 4)
        assert tts.spoken[-1] == ("10 percent.", None)
        # status speaks the mic and link clauses
        stt.queue.put_nowait(Final("Operator status"))
        await until(lambda: any(t.startswith("Link up.") for t, _ in tts.spoken))
        status = [t for t, _ in tts.spoken if t.startswith("Link up.")][0]
        assert "No mic frames yet." in status and "Context at 10 percent." in status
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_readdress_after_a_gap_discards_with_the_falling_tone(tmp_path):
    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(
            tmp_path, {}, overlay={"turns": {"readdress_gap_s": 0.05}})
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, half a thought"))
        await until(lambda: host.spoken.holds == frozenset({"talk"}))
        await asyncio.sleep(0.1)
        stt.queue.put_nowait(Final("Operator, fresh"))
        await until(lambda: "abandoned" in cue_names(stream))
        assert any("discarded: half a thought" in line for line in out)
        assert host.spoken.holds == frozenset({"talk"})  # the new turn is open
        stt.queue.put_nowait(Final("Operator cancel"))
        await until(lambda: not host.spoken.paused)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_stt_link_drop_reconnects_with_the_cue_pair(tmp_path):
    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, {})
        # the startup probe is session 1; the voice opens session 2
        await until(lambda: len(stt.sessions) == 2 and stt.sessions[0].closed)
        stt.queue.put_nowait(Error("link: server closed 1011"))
        await until(lambda: len(stt.sessions) == 3)  # the gate reopened it
        await until(lambda: cue_names(stream).count("connected") == 2)
        assert cue_names(stream).count("disconnected") == 1
        assert mic.started == 1  # a link drop never touches the microphone
        assert stt.sessions[2].chunks  # audio flows into the new session
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_the_state_follows_hearing_speaking_a_stop_and_a_dropped_link(tmp_path):
    answers = {"tell me a long story": [result("⟦voice⟧A long spoken answer that keeps going.⟦/voice⟧",
                                               used=100, window=1000)]}
    tts = FakeTTS(chunk_delay_s=0.02, chunks=50)

    async def scenario():
        states = []
        host, seat, claude, stt, tts_, stream, mic, out, run = build(
            tmp_path, answers, tts=tts, states=states)
        await until(lambda: len(stt.sessions) == 2 and stt.sessions[0].closed)
        assert states == [{"state": "idle"}]
        stt.queue.put_nowait(Partial("Operator"))
        await until(lambda: states[-1] == {"state": "hearing", "words": ""})
        # words show as they are said: partials first, then the final
        stt.queue.put_nowait(Partial("Operator, tell me"))
        await until(lambda: states[-1] == {"state": "hearing", "words": "tell me"})
        stt.queue.put_nowait(Partial("so anyway"))  # unaddressed: nothing shows
        await until(lambda: states[-1] == {"state": "idle"})
        # hearing carries the turn's last three words, the closer included
        stt.queue.put_nowait(Final("Operator, tell me a long story"))
        await until(lambda: states[-1] == {"state": "hearing", "words": "a long story"})
        stt.queue.put_nowait(Final("over"))
        await until(lambda: any(t.startswith("A long spoken") for t, _ in tts.spoken))
        assert {"state": "hearing", "words": "long story over"} in states
        assert states[-1] == {"state": "speaking"}
        stt.queue.put_nowait(Final("Operator stop"))
        await until(lambda: states[-1] == {"state": "paused"})
        stt.queue.put_nowait(Final("Operator resume"))
        await until(lambda: states[-1] == {"state": "speaking"})
        await until(lambda: ("10 percent.", None) in tts.spoken and states[-1] == {"state": "idle"})
        stt.queue.put_nowait(Error("link: server closed 1011"))
        await until(lambda: {"state": "trouble", "detail": "speech link down"} in states)
        await until(lambda: states[-1] == {"state": "idle"})
        assert all(a != b for a, b in zip(states, states[1:]))  # changes only
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_compact_by_voice_writes_the_slash_command(tmp_path):
    answers = {"one": [result("⟦voice⟧One.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, one over"))
        await until(lambda: ("One.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator compact"))
        await until(lambda: seat.compact_pending)
        assert claude.procs[0].stdin.writes[-1].endswith('"/compact"}]}}\n')
        await until(lambda: ("Compacting.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_confirm_without_the_mod_has_nothing_to_confirm(tmp_path):
    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, {})
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator confirm"))
        await until(lambda: ("Nothing to confirm.", None) in tts.spoken)
        assert not claude.procs or not claude.procs[0].stdin.writes
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_a_stop_holds_against_new_output_but_ends_on_new_input(tmp_path):
    answers = {"first": [result("⟦voice⟧First answer.⟦/voice⟧", used=100, window=1000)],
               "second": [result("⟦voice⟧Second answer.⟦/voice⟧", used=200, window=1000)]}
    tts = FakeTTS(chunk_delay_s=0.02, chunks=8)

    async def scenario():
        host, seat, claude, stt, tts_, stream, mic, out, run = build(tmp_path, answers, tts=tts)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, first over"))
        await until(lambda: ("Received.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator stop"))  # while "Received." plays
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        # the answer arrives while stopped: it queues silently
        await until(lambda: any(e.text == "First answer." for e in host.spoken.entries))
        await asyncio.sleep(0.1)
        assert not any(t == "First answer." for t, _ in tts.spoken)
        assert host.spoken.paused
        # "resume" plays the stopped line and its backlog
        stt.queue.put_nowait(Final("Operator resume"))
        await until(lambda: ("10 percent.", None) in tts.spoken)
        assert [t for t, _ in tts.spoken].count("Received.") == 2
        assert ("First answer.", None) in tts.spoken
        await until(lambda: not host.spoken.busy)
        # a stop, then new INPUT: the stopped line is abandoned, new content plays
        stt.queue.put_nowait(Final("Operator, second over"))
        await until(lambda: [t for t, _ in tts.spoken].count("Received.") == 3)
        stt.queue.put_nowait(Final("Operator stop"))
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        await until(lambda: any(e.text == "Second answer." for e in host.spoken.entries))
        n = len(tts.spoken)
        stt.queue.put_nowait(Final("Operator status"))  # any command is input
        await until(lambda: any(t.startswith("Link up.") for t, _ in tts.spoken))
        assert not host.spoken.paused
        assert "Second answer." not in [t for t, _ in tts.spoken[n:]]  # abandoned
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_compact_after_a_stop_is_heard(tmp_path):
    answers = {"one": [result("⟦voice⟧One.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, one over"))
        await until(lambda: ("One.", None) in tts.spoken)
        await until(lambda: not host.spoken.busy)
        stt.queue.put_nowait(Final("Operator stop"))
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        stt.queue.put_nowait(Final("Operator compact"))  # the live lock-up: a
        await until(lambda: seat.compact_pending)         # command clears the stop
        await until(lambda: ("Compacting.", None) in tts.spoken)
        assert [t for t, _ in tts.spoken][-2:] == ["Received.", "Compacting."]
        assert not host.spoken.paused
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_every_command_plays_its_cue_and_quit_ends_on_the_closing_tone(tmp_path):
    answers = {"one": [result("⟦voice⟧One.⟦/voice⟧", used=100, window=1000)]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, one over"))
        await until(lambda: ("10 percent.", None) in tts.spoken)
        await until(lambda: not host.spoken.busy)

        def cues_after(n):
            return cue_names(stream)[n:]

        for phrase, cue in (("Operator stop", "stop"), ("Operator resume", "resume"),
                            ("Operator again", "command"), ("Operator status", "command"),
                            ("Operator compact", "command")):
            n = len(cue_names(stream))
            stt.queue.put_nowait(Final(phrase))
            await until(lambda: cues_after(n))
            assert cues_after(n)[0] == cue, phrase
            await until(lambda: not host.spoken.busy)
        # cancel with nothing open: the falling tone, once
        n = len(cue_names(stream))
        stt.queue.put_nowait(Final("Operator never mind"))
        await until(lambda: cues_after(n))
        await asyncio.sleep(0.05)
        assert cues_after(n) == ["abandoned"]
        # cancel of a dictated turn: the falling tone, still once
        stt.queue.put_nowait(Final("Operator, half a thought"))
        await until(lambda: host.spoken.holds == frozenset({"talk"}))
        n = len(cue_names(stream))
        stt.queue.put_nowait(Final("Operator cancel"))
        await until(lambda: cues_after(n))
        await asyncio.sleep(0.05)
        assert cues_after(n) == ["abandoned"]
        # quit: got it, then the closing tone, fully played before the exit
        n = len(cue_names(stream))
        stt.queue.put_nowait(Final("Operator quit"))
        await run
        assert cues_after(n) == ["command", "closing"]
        assert stream.writes[-1] == CUES["closing"]

    asyncio.run(scenario())
