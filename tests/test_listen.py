"""`hands-free-voice listen` end to end, offline: fake STT events in, JSON
lines out, mod events posted over the real Unix socket, a fake TTS."""

import asyncio
import json
import os
import shutil
import signal
import tempfile

import pytest

from hands_free_voice.audio import Playback
from hands_free_voice.config import DEFAULTS, deep_merge
from hands_free_voice.listen import JsonLines, run_listen, wait_for_lock, with_file_keys
from hands_free_voice.state import LockFile
from hands_free_voice.providers import Final
from hands_free_voice.providers.fake import FakeSTT, FakeTTS, FakeVAD
from tests.fakes import until
from tests.test_voice import FakeMic, FakeOutStream, keep_talking


async def http(sock: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_unix_connection(sock)
    data = json.dumps(body).encode() if body is not None else b""
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: hands-free\r\n"
                 f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(payload)


@pytest.fixture
def sock_dir():
    # pytest's tmp_path is too long for a Unix socket path on macOS (104 bytes)
    d = tempfile.mkdtemp(prefix="hfv-t-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Lines:
    def __init__(self):
        self.items = []

    def write(self, s):
        self.items += [json.loads(x) for x in s.splitlines() if x]

    def flush(self):
        pass

    def of(self, kind):
        return [x for x in self.items if x["type"] == kind]


def test_a_heard_turn_goes_out_and_the_answer_comes_back_as_speech(sock_dir):
    cfg = deep_merge(DEFAULTS, {"turns": {"closer_settle_s": 0.03},
                                "working": {"interval_s": 60}, "tts": {"voice": ""},
                                "gate": {"hangover_s": 60, "empty_hangover_s": 60,
                                         "deaf_s": 0}})

    async def scenario():
        lines = Lines()
        stt, tts, mic = FakeSTT(), FakeTTS(), FakeMic()
        playback = Playback(enabled=True, open_stream=FakeOutStream)
        run = asyncio.ensure_future(run_listen(
            cfg, "/proj", stt=stt, tts=tts, write=JsonLines(lines), sock_dir=sock_dir,
            playback=playback, mic=mic, vad=FakeVAD()))
        mic.talker = asyncio.ensure_future(keep_talking(mic, run))
        await until(lambda: lines.of("hello") and stt.sessions)
        hello = lines.of("hello")[0]
        assert "⟦voice⟧" in hello["contract"] and hello["address"] == "operator"
        assert hello["commands"] == ["stop", "resume", "again", "never mind", "status",
                                     "compact", "quit"]
        sock = hello["socket"]

        stt.queue.put_nowait(Final("Operator, run the tests"))
        stt.queue.put_nowait(Final("over"))
        await until(lambda: lines.of("turn"))
        assert lines.of("turn") == [{"type": "turn", "text": "run the tests"}]
        assert {"type": "state", "state": "hearing", "words": "run the tests"} in lines.of("state")

        for ev in ({"kind": "start"},
                   {"kind": "tool", "name": "Bash", "input": {"description": "Run the tests"}},
                   {"kind": "complete", "answer": "⟦voice⟧All green.⟦/voice⟧",
                    "reason": "answer", "pct": 7}):
            assert (await http(sock, "POST", "/event", ev))[0] == 200
        await until(lambda: any(t == "7 percent." for t, _ in tts.spoken))
        assert [t for t, _ in tts.spoken] == ["Received.", "Running: Run the tests.",
                                              "All green.", "7 percent."]
        await until(lambda: lines.of("state")[-1] == {"type": "state", "state": "idle"})
        assert {"type": "state", "state": "speaking"} in lines.of("state")

        status, snap = await http(sock, "GET", "/status")
        assert status == 200 and snap["last_pct"] == 7 and not snap["query_open"]
        assert (await http(sock, "POST", "/nope", {}))[0] == 404

        stt.queue.put_nowait(Final("Operator compact"))
        await until(lambda: lines.of("compact"))
        # the confirm goes to the mod, which files the feedback it answers
        stt.queue.put_nowait(Final("Operator confirm"))
        await until(lambda: lines.of("confirm"))
        assert not any(line == {"type": "turn", "text": "confirm"} for line in lines.of("turn"))

        assert (await http(sock, "POST", "/quit", {}))[0] == 200
        assert await asyncio.wait_for(run, 5) is None  # a quit, not a signal
        assert any(x["text"] == "[session closed]" for x in lines.of("log"))

    asyncio.run(scenario())


def test_a_broken_stdout_stops_the_listener(sock_dir):
    class Gone:
        def write(self, s):
            raise BrokenPipeError

        def flush(self):
            pass

    cfg = deep_merge(DEFAULTS, {"tts": {"voice": ""}, "gate": {"deaf_s": 0}})

    async def scenario():
        mic = FakeMic()
        run = asyncio.ensure_future(run_listen(
            cfg, "/proj", stt=FakeSTT(), tts=FakeTTS(), write=JsonLines(Gone()),
            sock_dir=sock_dir, playback=Playback(enabled=True, open_stream=FakeOutStream),
            mic=mic, vad=FakeVAD()))
        await asyncio.wait_for(run, 5)

    asyncio.run(scenario())


def test_keys_missing_from_the_environment_come_from_the_keys_file(tmp_path):
    keys = tmp_path / ".env"
    keys.write_text("# speech keys\nexport DEEPGRAM_API_KEY='from-file'\n\n"
                    'CARTESIA_API_KEY="cartesia-from-file"\nOTHER=ignored\n')
    env = with_file_keys(["DEEPGRAM_API_KEY", "CARTESIA_API_KEY"],
                         {"DEEPGRAM_API_KEY": "from-env", "PATH": "/bin"}, path=keys)
    assert env == {"DEEPGRAM_API_KEY": "from-env", "CARTESIA_API_KEY": "cartesia-from-file",
                   "PATH": "/bin"}
    assert with_file_keys(["DEEPGRAM_API_KEY"], {}, path=tmp_path / "none") == {}


def test_a_listener_waits_for_the_one_it_replaces_to_let_go(tmp_path):
    lock = LockFile(tmp_path / "listen.lock")
    t = [0.0]

    def sleep(s):
        t[0] += s
        if t[0] >= 1.0:
            lock.release()  # the old listener finished closing

    lock.acquire(None, pid=1)  # pid 1 is always alive
    assert wait_for_lock(lock, 5.0, sleep=sleep, clock=lambda: t[0]) is None
    lock.acquire(None, pid=1)
    assert wait_for_lock(lock, 0.5, sleep=lambda s: t.__setitem__(0, t[0] + s),
                         clock=lambda: t[0]) == 1


def test_a_signal_ends_the_listener_and_says_which(sock_dir):
    cfg = deep_merge(DEFAULTS, {"working": {"interval_s": 60}, "tts": {"voice": ""},
                                "gate": {"deaf_s": 0}})

    async def scenario():
        lines = Lines()
        mic = FakeMic()
        run = asyncio.ensure_future(run_listen(
            cfg, "/proj", stt=FakeSTT(), tts=FakeTTS(), write=JsonLines(lines),
            sock_dir=sock_dir, playback=Playback(enabled=True, open_stream=FakeOutStream),
            mic=mic, vad=FakeVAD()))
        await until(lambda: lines.of("hello"))
        os.kill(os.getpid(), signal.SIGHUP)  # what a hot reload's stop looks like
        assert await asyncio.wait_for(run, 5) == signal.SIGHUP

    asyncio.run(scenario())


def test_a_working_cue_setting_out_of_range_is_refused(tmp_path, capsys):
    from hands_free_voice.listen import main
    base = ["--project", str(tmp_path), "--config", str(tmp_path / "none.toml")]
    for flags, words in ((["--working-interval", "0"], "interval"),
                         (["--working-tone-volume", "-1"], "volume")):
        assert main(base + flags) == 2
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line["type"] == "error" and words in line["text"], line
