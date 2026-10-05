"""The text front end end to end: real Host, real Seat, a scripted
claude child, and a recording voice. No audio, no network, no claude."""

import asyncio

from hands_free_voice.app import Host, compose_status, parse_text_command, run_text
from hands_free_voice.config import DEFAULTS, deep_merge
from hands_free_voice.seat import Seat
from hands_free_voice.spoken_log import SpokenLog
from hands_free_voice.state import EventLog, LockFile, SessionPin
from tests.fakes import SID, ScriptedClaude, assistant_text, assistant_tool, result, until


class Voice:
    def __init__(self):
        self.lines = []

    async def speak(self, text, register):
        self.lines.append((text, register))


def make_host(tmp_path, answers, cfg_overlay=None):
    cfg = deep_merge(DEFAULTS, cfg_overlay or {})
    out = []
    host = Host(cfg, "/proj", log=EventLog(tmp_path / "log.jsonl"), out=out.append)
    claude = ScriptedClaude(answers)
    pin = SessionPin(tmp_path / "session_id")
    lock = LockFile(tmp_path / "lock")
    lock.acquire(None)
    seat = Seat("/proj", contract_path="/c.md", session_pin=pin,
                on_event=host.on_seat_event, spawn=claude.spawn, env={},
                lock=lock, log=host.log)
    voice = Voice()
    host.bind(seat, SpokenLog(voice.speak))
    return host, seat, claude, voice, out, pin


def test_round_trip_speaks_the_block_then_the_percent(tmp_path):
    answers = {
        "say hi": [assistant_tool("Read", {"file_path": "README.md"}),
                   assistant_text("Plan.\n⟦voice⟧Reading first.⟦/voice⟧"),
                   result("Hi there.\n\n⟦voice⟧Hi. That is the whole answer.⟦/voice⟧",
                          used=3200, window=10000, num_turns=2)],
    }

    async def scenario():
        host, seat, claude, voice, out, pin = make_host(tmp_path, answers)
        lines: asyncio.Queue = asyncio.Queue()
        run = asyncio.ensure_future(run_text(host, lines, prompt=False))
        lines.put_nowait("say hi\n")
        await until(lambda: any(t == "32 percent." for t, _ in voice.lines))
        assert voice.lines == [("Received.", "speech"),
                               ("Reading README.md.", "narration"),
                               ("Reading first.", "speech"),
                               ("Hi. That is the whole answer.", "speech"),
                               ("32 percent.", "speech")]
        assert pin.load() == SID
        assert any("Hi there." in line for line in out)
        assert "→ sent: say hi" in out
        assert host.last_pct == 32
        lines.put_nowait(":status\n")
        await until(lambda: any(t.startswith("Session ") for t, _ in voice.lines))
        status = [t for t, _ in voice.lines if t.startswith("Session ")][0]
        assert status == f"Session {SID[:8]}. No open turns. Context at 32 percent."
        lines.put_nowait(":quit\n")
        await run
        assert out[-1] == "[session closed]"
        assert claude.procs[0].stdin.closed and not seat.alive

    asyncio.run(scenario())


def test_commands_pause_resume_replay_and_compact(tmp_path):
    answers = {"one": [result("⟦voice⟧First answer.⟦/voice⟧", used=100, window=1000)]}

    async def scenario():
        host, seat, claude, voice, out, pin = make_host(tmp_path, answers)
        lines: asyncio.Queue = asyncio.Queue()
        run = asyncio.ensure_future(run_text(host, lines, prompt=False))
        lines.put_nowait("one\n")
        await until(lambda: ("10 percent.", "speech") in voice.lines)
        lines.put_nowait(":stop\n")
        await until(lambda: host.spoken.paused)
        lines.put_nowait(":resume\n")
        await until(lambda: not host.spoken.paused)
        n = len(voice.lines)
        lines.put_nowait(":again\n")  # the last answer, through its closer
        await until(lambda: len(voice.lines) == n + 2)
        assert [t for t, _ in voice.lines[-2:]] == ["First answer.", "10 percent."]
        lines.put_nowait(":back 2\n")  # two raw lines back
        await until(lambda: len(voice.lines) == n + 4)
        assert [t for t, _ in voice.lines[-2:]] == ["First answer.", "10 percent."]
        lines.put_nowait(":compact\n")
        await until(lambda: seat.compact_pending)
        assert claude.procs[0].stdin.writes[-1].endswith('"/compact"}]}}\n')
        assert ("Received.", "speech") in voice.lines[-3:] and \
            ("Compacting.", "speech") in voice.lines[-2:]
        lines.put_nowait("quit\n")
        await run

    asyncio.run(scenario())


def test_eof_closes_cleanly_and_errors_are_spoken(tmp_path):
    answers = {"boom": [result("", is_error=True)],
               "phantom": [result("", origin="task-notification", num_turns=0)]}

    async def scenario():
        host, seat, claude, voice, out, pin = make_host(tmp_path, answers)
        lines: asyncio.Queue = asyncio.Queue()
        run = asyncio.ensure_future(run_text(host, lines, prompt=False))
        lines.put_nowait("boom\n")
        await until(lambda: ("Done.", "speech") in voice.lines)
        assert ("The Claude turn failed.", "speech") in voice.lines
        lines.put_nowait("phantom\n")
        await until(lambda: any("not spoken" in line for line in out))
        assert voice.lines.count(("Done.", "speech")) == 1  # the phantom said nothing
        lines.put_nowait(None)  # EOF
        await run
        assert not seat.alive

    asyncio.run(scenario())


def test_quit_waits_for_the_turn_in_flight(tmp_path):
    answers = {"slow": [assistant_tool("Bash", {"description": "sleep"})]}

    async def scenario():
        host, seat, claude, voice, out, pin = make_host(tmp_path, answers)
        lines: asyncio.Queue = asyncio.Queue()
        run = asyncio.ensure_future(run_text(host, lines, prompt=False))
        lines.put_nowait("slow\n")
        await until(lambda: seat.busy)
        lines.put_nowait(":quit\n")
        await until(lambda: any("in flight" in t for t, _ in voice.lines))
        assert not run.done()
        claude.procs[0].stdout.feed(result("⟦voice⟧Done now.⟦/voice⟧"))
        await run
        assert ("Done now.", "speech") in voice.lines

    asyncio.run(scenario())


def test_still_here_fires_only_while_busy_and_quiet(tmp_path):
    answers = {"slow": [assistant_tool("Bash", {"description": "sleep"})]}

    async def scenario():
        host, seat, claude, voice, out, pin = make_host(tmp_path, answers)
        ticks = []
        host.on_still_here = lambda: ticks.append(1)
        host.start()
        ticker = asyncio.ensure_future(host.still_here_ticker(0.02, interval_s=0.01))
        await asyncio.sleep(0.05)
        assert ticks == []  # idle: never
        host.enqueue_turn("slow")
        await until(lambda: seat.busy)
        await until(lambda: len(ticks) >= 2)
        ticker.cancel()
        await host.shutdown()

    asyncio.run(scenario())


def test_parse_text_command():
    assert parse_text_command("hello there") is None
    assert parse_text_command("quit") == ("quit", None)
    assert parse_text_command(":again") == ("again", None)
    assert parse_text_command(":back 3") == ("again", 3)
    assert parse_text_command(":back") == ("again", 1)
    assert parse_text_command(":stop") == ("stop", None)
    assert parse_text_command(":exit") == ("quit", None)
    assert parse_text_command(":status") == ("status", None)
    assert parse_text_command(":") is None


def test_compose_status():
    assert compose_status({}) == "A fresh session. No open turns. Context not yet known."
    assert compose_status({"link_up": True, "mic_age_s": 0.5, "session_id": SID,
                           "query_open": True, "queued_turns": 1, "unconsumed": 1,
                           "last_pct": 12}) == \
        (f"Link up. Mic just heard. Session {SID[:8]}. One query running, 1 queued,"
         " 1 written, not yet read. Context at 12 percent.")
    assert compose_status({"link_up": False, "mic_age_s": None, "claude_alive": False}) == \
        ("Link down. No mic frames yet. A fresh session. Claude is closed until the"
         " next turn. No open turns. Context not yet known.")
    assert "Mic quiet 7 seconds." in compose_status({"mic_age_s": 7.4})
