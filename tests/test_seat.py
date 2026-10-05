"""The Seat against a fake subprocess: spawn, resume pin, stacking,
death and respawn, close, idle close, and the group kill seam."""

import asyncio
import json
import signal

import pytest

import hands_free_voice.seat as seat_mod
from hands_free_voice.seat import Seat, SEAT_FLAGS, build_seat_command, user_message_line
from hands_free_voice.state import LockFile, SessionPin

SID = "db54779a-0000-4000-8000-000000000000"


def j(obj) -> str:
    return json.dumps(obj) + "\n"


def init(sid=SID):
    return j({"type": "system", "subtype": "init", "session_id": sid})


def echo(text):
    return j({"type": "user", "message": {"role": "user",
                                          "content": [{"type": "text", "text": text}]}})


def assistant_tool(name="Read", inp=None):
    return j({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp or {"file_path": "a.py"}}]}})


def result(text="done", used=None, window=None):
    rec = {"type": "result", "is_error": False, "result": text, "duration_ms": 10}
    if used is not None:
        rec["usage"] = {"input_tokens": used, "output_tokens": 0}
        rec["modelUsage"] = {"m": {"contextWindow": window}}
    return j(rec)


class FakeStdin:
    def __init__(self, fail=False):
        self.writes = []
        self.closed = False
        self.fail = fail

    def write(self, data: bytes):
        if self.fail:
            raise BrokenPipeError("stdin gone")
        self.writes.append(data.decode("utf-8"))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    async def readline(self) -> bytes:
        item = await self.q.get()
        if isinstance(item, Exception):
            raise item
        return item

    def feed(self, *lines: str):
        for line in lines:
            self.q.put_nowait(line.encode("utf-8"))

    def fail(self, exc: Exception):
        self.q.put_nowait(exc)

    def eof(self):
        self.q.put_nowait(b"")


class FakeProc:
    pid = None  # no process group: _signal_group falls back to kill()

    def __init__(self, fail_stdin=False):
        self.stdin = FakeStdin(fail=fail_stdin)
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.returncode = None
        self._exit = asyncio.get_event_loop().create_future()
        self.killed = False

    async def wait(self):
        return await asyncio.shield(self._exit)

    def kill(self):
        self.killed = True
        self.die(-9)

    def die(self, rc: int):
        if self.returncode is None:
            self.returncode = rc
            self._exit.set_result(rc)

    def exit(self, rc: int):
        self.die(rc)
        self.stdout.eof()
        self.stderr.eof()


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


def make_seat(tmp_path, fail_stdin=False, clock=None, pinned=None, **kw):
    events, spawned, procs = [], [], []

    async def spawn(cmd, cwd, env):
        proc = FakeProc(fail_stdin=fail_stdin)
        spawned.append((cmd, cwd, env))
        procs.append(proc)
        return proc

    pin = SessionPin(tmp_path / "session_id")
    if pinned:
        pin.save(pinned)
    lock = LockFile(tmp_path / "lock")
    lock.acquire(pinned)
    s = Seat("/proj", contract_path="/c.md", session_pin=pin, on_event=events.append,
             spawn=spawn, clock=clock or Clock(), env={"PATH": "/bin"},
             lock=lock, **kw)
    return s, events, spawned, procs, pin, lock


def kinds(events):
    return [e["kind"] for e in events]


def test_build_seat_command():
    assert build_seat_command(None, None) == ["claude", "-p"] + SEAT_FLAGS
    assert build_seat_command("sid-1", "/c.md", ["--permission-mode", "auto"], "/opt/claude") == \
        ["/opt/claude", "-p", "--resume", "sid-1"] + SEAT_FLAGS + \
        ["--append-system-prompt-file", "/c.md", "--permission-mode", "auto"]


def test_user_message_line_shape():
    line = user_message_line("hello there")
    assert line.endswith("\n") and "\n" not in line[:-1]
    obj = json.loads(line)
    assert obj == {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "hello there"}]}}


def test_first_submit_spawns_with_contract_and_stripped_env(tmp_path):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        assert not s.alive and s.session_id is None
        await s.submit("run the tests")
        cmd, cwd, env = spawned[0]
        assert cwd == "/proj" and env == {"PATH": "/bin"}
        assert "--resume" not in cmd and cmd[-2:] == ["--append-system-prompt-file", "/c.md"]
        proc = procs[0]
        assert proc.stdin.writes == [user_message_line("run the tests")]
        assert kinds(events) == ["seat", "accepted"]
        assert events[0]["state"] == "started"
        assert s.alive and s.unconsumed == 1 and not s.busy
        proc.stdout.feed(init(), echo("run the tests"), assistant_tool(),
                         result("green\n⟦voice⟧All green.⟦/voice⟧", used=1000, window=10000))
        await settle()
        assert pin.load() == SID and s.session_id == SID
        assert lock.read()["session_id"] == SID
        assert kinds(events) == ["seat", "accepted", "session", "consumed", "tool",
                                 "context", "result"]
        assert [x["text"] for x in events[-1]["say"]] == ["All green.", "10 percent."]
        assert s.unconsumed == 0 and not s.busy
        # a second message rides the same child
        await s.submit("and again")
        assert len(spawned) == 1
        assert proc.stdin.writes[-1] == user_message_line("and again")
        close_task = asyncio.ensure_future(s.shutdown())
        await settle()
        assert proc.stdin.closed
        proc.exit(0)
        await close_task
        await settle()
        assert not s.alive and not proc.killed
        assert events[-1] == {"kind": "seat", "state": "exited", "rc": 0, "stderr": []}

    asyncio.run(scenario())


def test_resume_rides_the_pin_and_stacks_while_busy(tmp_path):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path, pinned="sid-old")
        await s.submit("long job")
        assert spawned[0][0][:4] == ["claude", "-p", "--resume", "sid-old"]
        assert events[0]["state"] == "resumed"
        proc = procs[0]
        proc.stdout.feed(init("sid-old"), echo("long job"),
                         assistant_tool("Bash", {"command": "sleep 60"}))
        await settle()
        assert s.busy and s.unconsumed == 0
        await s.submit("also check X")  # written at once, not queued
        assert proc.stdin.writes[-1] == user_message_line("also check X")
        assert s.unconsumed == 1 and s.occupied
        proc.stdout.feed(echo("also check X"))
        await settle()
        assert events[-1] == {"kind": "consumed", "text": "also check X"}
        proc.stdout.feed(result("both"))
        await settle()
        assert events[-1]["kind"] == "result" and events[-1]["text"] == "both"
        proc.exit(0)
        await settle()

    asyncio.run(scenario())


def test_death_respawns_and_a_late_reader_cannot_clobber(tmp_path):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        await s.submit("x")
        old = procs[0]
        old.stdout.feed(init(), echo("x"), assistant_tool())
        await settle()
        assert s.busy
        old.die(1)  # dead, EOF not yet drained
        assert not s.alive
        await s.submit("y")  # respawns on the pin
        assert len(spawned) == 2 and spawned[1][0][:4] == ["claude", "-p", "--resume", SID]
        new = procs[1]
        new.stdout.feed(init(), echo("y"))
        await settle()
        assert s.busy
        old.stderr.feed("some stderr noise\n")
        old.stdout.eof()
        old.stderr.eof()
        await settle()
        errors = [e for e in events if e["kind"] == "error"]
        assert len(errors) == 1 and "mid-query" in errors[0]["message"]
        exited = [e for e in events if e["kind"] == "seat" and e["state"] == "exited"]
        assert exited[-1]["rc"] == 1 and exited[-1]["stderr"] == ["some stderr noise"]
        assert s.alive and s.busy  # the live run is untouched
        new.stdout.feed(result("y's answer"))
        await settle()
        assert events[-1]["text"] == "y's answer"
        new.exit(0)
        await settle()

    asyncio.run(scenario())


def test_unreadable_stream_tears_down(tmp_path):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        await s.submit("x")
        proc = procs[0]
        proc.stdout.feed(init(), echo("x"))
        await settle()
        proc.stdout.fail(ValueError("chunk exceeds the limit"))
        await settle()
        assert proc.killed and not s.alive
        assert any("unreadable" in e.get("message", "") for e in events if e["kind"] == "error")
        await s.submit("y")
        assert len(spawned) == 2 and s.alive
        procs[1].exit(0)
        await settle()

    asyncio.run(scenario())


def test_write_failure_raises_and_counts_nothing(tmp_path):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path, fail_stdin=True)
        with pytest.raises(BrokenPipeError):
            await s.submit("x")
        assert s.unconsumed == 0 and kinds(events) == ["seat"]
        procs[0].exit(0)
        await settle()
        assert kinds(events) == ["seat", "seat"]

    asyncio.run(scenario())


def test_close_signals_a_wedged_child(tmp_path, monkeypatch):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        await s.submit("x")
        proc = procs[0]
        monkeypatch.setattr(seat_mod, "CLOSE_GRACE_S", 0.02)
        monkeypatch.setattr(seat_mod, "TERM_GRACE_S", 0.02)
        await s.close()
        assert proc.stdin.closed and proc.killed and proc.returncode == -9
        proc.stdout.eof()
        proc.stderr.eof()
        await settle()
        errors = [e for e in events if e["kind"] == "error"]
        assert len(errors) == 1 and "1 message(s) written, never read" in errors[0]["message"]
        assert lock.read()["child_pid"] is None

    asyncio.run(scenario())


def test_shutdown_closes_a_respawn_during_an_idle_close(tmp_path, monkeypatch):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        monkeypatch.setattr(seat_mod, "CLOSE_GRACE_S", 0.05)
        monkeypatch.setattr(seat_mod, "TERM_GRACE_S", 0.05)
        await s.submit("x")
        first = procs[0]
        first.stdout.feed(init(), echo("x"), result("ok"))
        await settle()
        close_task = asyncio.ensure_future(s.close())
        await settle()
        assert first.stdin.closed and not s.alive
        await s.submit("y")
        second = procs[1]
        assert s.alive
        await s.shutdown()
        await close_task
        assert first.killed and second.killed and not s.alive
        assert not s._closings

    asyncio.run(scenario())


def test_idle_close_then_respawn_on_the_pin(tmp_path):
    async def scenario():
        clock = Clock(0.0)
        s, events, spawned, procs, pin, lock = make_seat(
            tmp_path, clock=clock, idle_close_s=200.0, sweep_interval_s=0.01)
        await s.submit("x")
        proc = procs[0]
        proc.stdout.feed(init(), echo("x"), result("waiting"))
        await settle()
        clock.t = 100.0
        await asyncio.sleep(0.05)
        assert s.alive
        clock.t = 400.0
        await asyncio.sleep(0.03)
        proc.exit(0)  # the child obeys stdin EOF
        await asyncio.sleep(0.03)
        await settle()
        assert not s.alive and proc.stdin.closed
        tail = [e for e in events if e["kind"] == "seat"][-2:]
        assert [t["state"] for t in tail] == ["idle_closed", "exited"]
        await s.submit("y")
        assert len(spawned) == 2 and spawned[1][0][:4] == ["claude", "-p", "--resume", SID]
        procs[1].exit(0)
        await settle()
        await s.shutdown()

    asyncio.run(scenario())


def test_compact_write_emits_no_accepted_and_arms_the_flag(tmp_path):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        await s.submit("hello")
        proc = procs[0]
        proc.stdout.feed(init(), echo("hello"), result("hi"))
        await settle()
        before = len(events)
        await s.compact()
        assert proc.stdin.writes[-1] == user_message_line("/compact")
        assert len(events) == before and s.unconsumed == 0 and s.compact_pending
        proc.exit(0)
        await settle()

    asyncio.run(scenario())


def test_pin_failure_does_not_kill_the_reader(tmp_path):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        pin.save = lambda sid: (_ for _ in ()).throw(OSError("disk full"))
        await s.submit("x")
        proc = procs[0]
        proc.stdout.feed(init(), echo("x"), result("fine"))
        await settle()
        assert events[-1]["kind"] == "result" and s.alive
        proc.exit(0)
        await settle()

    asyncio.run(scenario())


def test_event_handler_failure_is_contained(tmp_path, capsys):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        s._on_event = lambda ev: (_ for _ in ()).throw(RuntimeError("bad handler"))
        await s.submit("x")
        proc = procs[0]
        proc.stdout.feed(init(), result("fine"))
        await settle()
        assert s.alive and s.session_id == SID
        proc.exit(0)
        await settle()

    asyncio.run(scenario())
    assert "event handler failed" in capsys.readouterr().err


def test_signal_group_uses_killpg_for_a_real_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(seat_mod.os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    class P:
        pid = 4242

    seat_mod._signal_group(P(), signal.SIGTERM)
    assert calls == [(4242, signal.SIGTERM)]
    # a group already gone never raises
    monkeypatch.setattr(seat_mod.os, "killpg",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    seat_mod._signal_group(P(), signal.SIGKILL)


def test_kill_now_terminates_the_group(tmp_path, monkeypatch):
    async def scenario():
        s, events, spawned, procs, pin, lock = make_seat(tmp_path)
        await s.submit("x")
        proc = procs[0]
        proc.pid = 777
        sent = []

        def fake_killpg(pid, sig):
            sent.append((pid, sig))
            proc.die(-15)

        monkeypatch.setattr(seat_mod.os, "killpg", fake_killpg)
        monkeypatch.setattr(seat_mod.os, "kill",
                            lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
        s.kill_now()
        assert sent == [(777, signal.SIGTERM)]
        proc.stdout.eof()
        proc.stderr.eof()
        await settle()

    asyncio.run(scenario())
