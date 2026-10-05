"""The SpokenLog's cursor: play in order, pause/resume, replay, holds."""

import asyncio

from hands_free_voice.spoken_log import SpokenLog


class FakeVoice:
    """speak() parks until the test finishes the line, and records
    starts, completions and cancellations."""

    def __init__(self):
        self.started: list[str] = []
        self.finished: list[str] = []
        self.cancelled: list[str] = []
        self._gates: dict[str, asyncio.Event] = {}
        self.fail_on: set[str] = set()

    async def speak(self, text, register):
        self.started.append(f"{text}|{register}")
        if text in self.fail_on:
            raise RuntimeError("vendor down")
        gate = self._gates[text] = asyncio.Event()  # fresh per play
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
        self.finished.append(text)

    def finish(self, text):
        self._gates[text].set()


async def until(pred, tries=200):
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition never held")


def test_plays_in_order_and_records_registers():
    async def scenario():
        v = FakeVoice()
        starts = []
        log = SpokenLog(v.speak, on_start=lambda e: starts.append(e.index))
        log.start()
        log.append("one", "narration")
        log.append("two")
        await until(lambda: v.started == ["one|narration"])
        assert log.playing == 0 and log.busy and log.backlog == 2
        v.finish("one")
        await until(lambda: v.started == ["one|narration", "two|speech"])
        v.finish("two")
        await until(lambda: log.cursor == 2 and not log.busy)
        assert starts == [0, 1] and v.finished == ["one", "two"]
        await log.stop()

    asyncio.run(scenario())


def test_pause_stops_the_line_and_resume_replays_it():
    async def scenario():
        v = FakeVoice()
        log = SpokenLog(v.speak)
        log.start()
        log.append("long line")
        log.append("next")
        await until(lambda: v.started == ["long line|speech"])
        log.pause("user")
        await until(lambda: v.cancelled == ["long line"])
        assert log.paused and log.playing is None and log.cursor == 0
        log.append("queued while paused")
        await asyncio.sleep(0.01)
        assert v.started == ["long line|speech"]  # nothing plays while held
        log.resume("user")
        await until(lambda: v.started == ["long line|speech", "long line|speech"])
        v.finish("long line")
        await until(lambda: len(v.started) == 3)
        assert v.started[-1] == "next|speech"
        await log.stop()

    asyncio.run(scenario())


def test_two_holds_are_independent():
    async def scenario():
        v = FakeVoice()
        log = SpokenLog(v.speak)
        log.start()
        log.append("a")
        await until(lambda: v.started == ["a|speech"])
        log.pause("talk")     # the address word opened a turn
        log.pause("user")     # then "operator stop"
        await until(lambda: v.cancelled == ["a"])
        log.resume("talk")    # the turn was sent: still held by the user
        await asyncio.sleep(0.01)
        assert log.paused and log.holds == frozenset({"user"})
        assert v.started == ["a|speech"]
        log.resume("user")
        await until(lambda: v.started == ["a|speech", "a|speech"])
        log.resume("nobody")  # unknown hold: a no-op
        await log.stop()

    asyncio.run(scenario())


def test_replay_moves_the_cursor_back_and_clears_the_user_hold():
    async def scenario():
        v = FakeVoice()
        log = SpokenLog(v.speak)
        log.start()
        assert log.replay() is None  # nothing has played
        for t in ("one", "two", "three"):
            log.append(t)
        for t in ("one", "two", "three"):
            await until(lambda: v.started and v.started[-1] == f"{t}|speech")
            v.finish(t)
        await until(lambda: log.cursor == 3)
        log.pause("user")
        assert log.replay() == 2          # "again": the last line
        await until(lambda: v.started[-1] == "three|speech" and len(v.started) == 4)
        assert not log.paused
        v.finish("three")
        await until(lambda: log.cursor == 3)
        assert log.replay(2) == 1         # "back two"
        await until(lambda: len(v.started) == 5 and v.started[-1] == "two|speech")
        # replay during playback counts from the line playing
        assert log.replay(5) == 0
        await until(lambda: len(v.started) == 6 and v.started[-1] == "one|speech")
        await log.stop()

    asyncio.run(scenario())


def test_a_failing_line_is_skipped_not_looped():
    async def scenario():
        v = FakeVoice()
        v.fail_on.add("bad")
        errors = []
        log = SpokenLog(v.speak, on_error=lambda e, exc: errors.append((e.text, str(exc))))
        log.start()
        log.append("bad")
        log.append("good")
        await until(lambda: "good|speech" in v.started)
        assert errors == [("bad", "vendor down")]
        v.finish("good")
        await until(lambda: log.cursor == 2)
        await log.stop()

    asyncio.run(scenario())


def test_stop_cancels_the_line_in_flight():
    async def scenario():
        v = FakeVoice()
        log = SpokenLog(v.speak)
        log.start()
        log.append("forever")
        await until(lambda: v.started == ["forever|speech"])
        await log.stop()
        assert v.cancelled == ["forever"]
        assert log._task is None

    asyncio.run(scenario())
