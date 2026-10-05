"""The gate: no socket while silent, the pre-roll flushed on open, the
hangover and the turn's hold, reconnects, the deaf watchdog."""

import asyncio

from hands_free_voice.gate import Gate
from hands_free_voice.providers import Final, Partial, SpeechStarted
from hands_free_voice.providers.fake import FakeVAD

VOICED = b"\x01\x00" * 640   # one 40 ms mic chunk of "speech" (two VAD frames)
SILENT = bytes(1280)
CHUNK_S = 0.04


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSession:
    def __init__(self):
        self.sent: list[bytes] = []
        self.q: asyncio.Queue = asyncio.Queue()
        self.close_calls = 0

    async def send(self, pcm16):
        self.sent.append(pcm16)

    async def events(self):
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield ev

    async def close(self):
        self.close_calls += 1
        self.q.put_nowait(None)  # the server acks the close and ends the stream

    def server_close(self):
        self.q.put_nowait(None)


class Opener:
    def __init__(self, fail_first: int = 0):
        self.sessions: list[FakeSession] = []
        self.calls = 0
        self.fail_first = fail_first

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("no route to host")
        s = FakeSession()
        self.sessions.append(s)
        return s


class Harness:
    def __init__(self, hold=False, **kw):
        self.clock = Clock()
        self.opener = Opener(kw.pop("fail_first", 0))
        self.events = []
        self.links = []
        self.deaf = 0
        self.logs = []
        self.out = []
        self.hold = hold
        self.gate = Gate(self.opener, self.events.append, vad=FakeVAD(), rate=16000,
                         pre_roll_s=0.5, hangover_s=10.0, empty_hangover_s=2.0,
                         deaf_s=8.0, hold=lambda: self.hold,
                         on_link=lambda up, d=None: self.links.append(up),
                         on_deaf=self._deaf, out=self.out.append,
                         log=lambda kind, **f: self.logs.append(f),
                         clock=self.clock, tick_s=0.005, **kw)

    def _deaf(self):
        self.deaf += 1

    def feed(self, chunk, n=1):
        for _ in range(n):
            self.gate.feed(chunk)
            self.clock.advance(CHUNK_S)

    async def settle(self, s=0.03):
        await asyncio.sleep(s)

    @property
    def session(self):
        return self.opener.sessions[-1]


def run(coro):
    return asyncio.run(coro)


def test_silence_opens_nothing():
    async def scenario():
        h = Harness()
        h.feed(SILENT, 100)
        await h.settle()
        assert h.opener.calls == 0 and h.gate.state == Gate.CLOSED
        assert h.events == []

    run(scenario())


def test_speech_opens_and_the_pre_roll_reaches_the_session_first():
    async def scenario():
        h = Harness()
        h.feed(SILENT, 30)                     # 1.2 s of quiet; the ring keeps 0.5 s
        h.feed(VOICED, 2)                      # the onset lands in the 2nd chunk
        assert h.gate.state == Gate.OPENING and h.events == [SpeechStarted()]
        h.feed(VOICED, 3)                      # still connecting: buffered
        await h.settle()
        assert h.opener.calls == 1 and h.gate.state == Gate.OPEN
        sent = h.session.sent
        # the ring: 0.5 s = 12 chunks, so 10 silent then the 2 voiced, then the 3
        assert len(sent) == 15 and sent[0] == SILENT and sent[:10] == [SILENT] * 10
        assert sent[10:] == [VOICED] * 5
        h.feed(VOICED, 2)                      # live now
        await h.settle()
        assert h.session.sent == [SILENT] * 10 + [VOICED] * 7

    run(scenario())


def test_hangover_closes_gracefully_after_words_and_sooner_without():
    async def scenario():
        h = Harness()
        h.feed(VOICED, 5)
        await h.settle()
        h.session.q.put_nowait(Partial("op"))
        h.session.q.put_nowait(Final("operator"))
        await h.settle()
        assert h.events[1:] == [Partial("op"), Final("operator")]
        h.feed(SILENT, 5)
        h.clock.advance(9.0)                   # inside the 10 s hangover
        await h.settle()
        assert h.gate.state == Gate.OPEN and h.session.close_calls == 0
        h.clock.advance(2.0)                   # past it
        await h.settle()
        assert h.gate.state == Gate.CLOSED and h.session.close_calls == 1
        assert h.logs[-1] == {"state": "closed", "reason": "hangover", "words": True}
        # a second utterance opens a second session
        h.feed(SILENT, 10)
        h.feed(VOICED, 3)
        await h.settle()
        assert h.opener.calls == 2 and h.gate.state == Gate.OPEN
        # no words at all: the short hangover
        h.feed(SILENT, 2)
        h.clock.advance(2.5)
        await h.settle()
        assert h.gate.state == Gate.CLOSED
        assert h.logs[-1] == {"state": "closed", "reason": "no words", "words": False}
        assert h.links == []                   # ordinary opens and closes never cue

    run(scenario())


def test_an_open_turn_holds_the_socket_past_the_hangover():
    async def scenario():
        h = Harness(hold=True)
        h.feed(VOICED, 5)
        await h.settle()
        h.session.q.put_nowait(Final("operator, thinking"))
        await h.settle()
        h.feed(SILENT, 5)
        h.clock.advance(120.0)                 # a very long pause mid-thought
        await h.settle()
        assert h.gate.state == Gate.OPEN and h.session.close_calls == 0
        h.hold = False                         # the turn was sent (or expired)
        await h.settle()
        assert h.gate.state == Gate.CLOSED and h.session.close_calls == 1

    run(scenario())


def test_a_server_close_mid_speech_reconnects_and_flushes_the_ring():
    async def scenario():
        h = Harness()
        h.feed(VOICED, 5)
        await h.settle()
        first = h.session
        first.server_close()
        await h.settle()
        assert h.opener.calls == 2 and h.gate.state == Gate.OPEN
        assert h.links == [False, True]        # the cue pair, once
        second = h.session
        assert second is not first and second.sent == [VOICED] * 5  # the ring, again
        h.feed(VOICED, 2)
        await h.settle()
        assert second.sent == [VOICED] * 7
        # a server close after the voice stopped and the hangover passed: no reconnect
        h.feed(SILENT, 2)
        h.clock.advance(15.0)
        second.server_close()
        await h.settle()
        assert h.opener.calls == 2 and h.gate.state == Gate.CLOSED

    run(scenario())


def test_open_failure_backs_off_and_loses_nothing():
    async def scenario():
        h = Harness(fail_first=1, hold=True, backoff_s=0.0)
        h.feed(VOICED, 5)
        await h.settle(0.05)
        assert h.opener.calls >= 2 and h.gate.state == Gate.OPEN
        assert h.links == [False, True]
        assert h.session.sent[:5] == [VOICED] * 5

    run(scenario())


def test_voice_with_no_words_reconnects_then_asks_for_a_new_mic():
    async def scenario():
        h = Harness(hold=True)
        h.feed(VOICED, 3)
        await h.settle()
        first = h.session
        # a long SILENT pause is not deafness, however long since the last words
        h.session.q.put_nowait(Final("operator"))
        await h.settle()
        h.feed(SILENT, 5)
        h.clock.advance(30.0)
        await h.settle()
        assert h.opener.calls == 1
        for _ in range(9):                     # 9 s of voice, nothing heard
            h.feed(VOICED, 25)                 # 1 s
            await h.settle()
        assert h.opener.calls == 2 and first.close_calls == 1 and h.deaf == 0
        assert any(f.get("reason") == "deaf" for f in h.logs)
        assert any("no words" in line for line in h.out)
        for _ in range(9):
            h.feed(VOICED, 25)
            await h.settle()
        assert h.opener.calls == 3 and h.deaf == 1  # twice deaf: rebuild the mic
        # words reset the count and the strikes
        h.feed(VOICED, 25 * 5)
        h.session.q.put_nowait(Final("hello"))
        await h.settle()
        assert h.gate._deaf_strikes == 0 and h.gate._voiced_no_text_s == 0.0
        h.feed(VOICED, 25 * 5)
        await h.settle()
        assert h.opener.calls == 3

    run(scenario())


def test_probe_and_stop():
    async def scenario():
        h = Harness()
        await h.gate.probe()
        assert h.opener.calls == 1 and h.session.close_calls == 1
        assert h.gate.state == Gate.CLOSED
        h.feed(VOICED, 5)
        await h.settle()
        assert h.gate.state == Gate.OPEN
        await h.gate.stop()
        assert h.gate.state == Gate.CLOSED and h.session.close_calls >= 1
        h.feed(VOICED, 5)                      # after a stop: inert
        await h.settle()
        assert h.opener.calls == 2
        failing = Harness(fail_first=1)
        try:
            await failing.gate.probe()
        except RuntimeError as exc:
            assert "no route" in str(exc)
        else:
            raise AssertionError("the probe should raise")

    run(scenario())
