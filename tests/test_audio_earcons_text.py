import asyncio
import threading
import time
from array import array

from hands_free_voice import earcons
from hands_free_voice.audio import Mic, Playback, Resampler, scale_pcm, starved, watchdog_tick
from hands_free_voice.config import DEFAULTS
from hands_free_voice.text import Respeller, sentence_chunks


# -- earcons --------------------------------------------------------------

def test_every_cue_is_nonempty_even_pcm16_and_distinct():
    cues = earcons.get_set()
    assert set(cues) == set(earcons.CUE_KEYS)
    for key, pcm in cues.items():
        assert isinstance(pcm, bytes) and pcm and len(pcm) % 2 == 0, key
    vals = list(cues.values())
    assert len({v for v in vals}) == len(vals)
    rate_bytes = earcons.SAMPLE_RATE * 2
    assert int(0.5 * rate_bytes) <= len(cues["disconnected"]) <= int(0.65 * rate_bytes)
    assert len(cues["connected"]) > len(cues["dispatch"]) > len(cues["capture"])
    assert len(earcons.get_set(48000)["capture"]) == 2 * len(cues["capture"])
    # the command family: a short got-it, a stop/resume pair that shares its
    # notes and differs by direction, and the closing mirror of connected
    assert len(cues["command"]) < len(cues["capture"])
    assert len(cues["stop"]) == len(cues["resume"]) and cues["stop"] != cues["resume"]
    assert len(cues["closing"]) == len(cues["connected"]) and cues["closing"] != cues["connected"]


def test_every_cue_plays_at_the_same_level():
    peaks = {}
    for key, pcm in earcons.get_set().items():
        a = array("h")
        a.frombytes(pcm)
        peaks[key] = max(abs(x) for x in a)
    assert max(peaks.values()) <= 1.15 * min(peaks.values()), peaks


def test_still_working_is_the_oddity_first_note():
    # one held note near B flat 4, keyed on quickly: at full level within
    # 20 ms, still there halfway through, two thirds of a second
    a = array("h")
    a.frombytes(earcons.get_set()["still_here"])
    rate = earcons.SAMPLE_RATE
    assert abs(len(a) - 0.65 * rate) < 0.01 * rate
    peak = max(abs(x) for x in a)
    assert max(abs(x) for x in a[int(0.015 * rate):int(0.02 * rate)]) > 0.9 * peak
    middle = a[len(a) // 2 - 100:len(a) // 2 + 100]
    assert max(abs(x) for x in middle) > 0.9 * peak
    crossings = sum((p < 0) != (q < 0) for p, q in zip(a, a[1:]))
    hz = crossings / 2 / (len(a) / rate)
    assert 455 < hz < 480, hz


# -- pcm and the watchdog ---------------------------------------------------

def test_scale_pcm():
    pcm = array("h", [1000, -1000, 32767, -32768]).tobytes()
    assert scale_pcm(pcm, 1.0) is pcm
    assert list(array("h", scale_pcm(pcm, 0.5))) == [500, -500, 16383, -16384]
    assert list(array("h", scale_pcm(pcm, 2.0))) == [2000, -2000, 32767, -32768]
    assert scale_pcm(b"odd", 0.5) == b"odd"


def test_starved_and_watchdog_tick():
    assert not starved(None, 100.0)
    assert not starved(96.0, 100.0)
    assert starved(90.0, 100.0)
    # a never-heard mic: down once, then periodic rebuilds, never "up"
    d, since, ev, s = watchdog_tick(None, 0.0, False, None, 6.0)
    assert (d, since, ev, s) == (True, 6.0, "down", True)
    d, since, ev, s = watchdog_tick(None, 0.0, d, since, 8.0)
    assert (d, ev, s) == (True, None, True)
    # a fresh pass gets its grace window without a rebuild
    d, since, ev, s = watchdog_tick(None, 8.0, d, since, 9.0)
    assert (d, ev, s) == (True, None, False)
    # a real frame newer than the drop: up exactly once
    d, since, ev, s = watchdog_tick(9.5, 8.0, d, since, 10.0)
    assert (d, since, ev, s) == (False, None, "up", False)
    d, since, ev, s = watchdog_tick(9.5, 8.0, d, since, 11.0)
    assert (d, ev, s) == (False, None, False)


def test_watchdog_treats_frames_of_pure_silence_as_a_dead_mic():
    # frames keep coming (last_frame_t fresh) but none has been live since the pass began
    d, since, ev, s = watchdog_tick(19.9, 10.0, False, None, 20.0, last_live_t=None)
    assert (d, since, ev, s) == (True, 20.0, "silent", True)      # 10 s > MIC_SILENT_S
    d, since, ev, s = watchdog_tick(21.9, 10.0, d, since, 22.0, last_live_t=None)
    assert (d, ev, s) == (True, None, True)                        # periodic rebuilds
    # a rebuild starts a new pass: grace again, still down
    d, since, ev, s = watchdog_tick(22.5, 22.2, d, since, 23.0, last_live_t=None)
    assert (d, ev, s) == (True, None, False)
    # frames that are still zeros never count as "up"; a live one does
    d, since, ev, s = watchdog_tick(30.9, 22.2, d, since, 31.0, last_live_t=None)
    assert (d, ev, s) == (True, None, True)
    d, since, ev, s = watchdog_tick(31.5, 22.2, d, since, 31.6, last_live_t=31.5)
    assert (d, since, ev, s) == (False, None, "up", False)
    # a live frame within the silent threshold: nothing to do
    assert watchdog_tick(40.0, 22.2, False, None, 41.0, last_live_t=36.0)[2] is None


# -- the resampler and the mic ------------------------------------------------------

def test_resampler_rates_and_seams():
    same = Resampler(16000, 16000)
    pcm = array("h", [1, 2, 3, 4]).tobytes()
    assert same(pcm) is pcm
    down = Resampler(48000, 16000)
    ramp = array("h", range(0, 4800, 1)).tobytes()  # 100 ms at 48 kHz
    out = array("h", down(ramp))
    assert 1598 <= len(out) <= 1601                  # 1600 samples of 16 kHz
    assert out[0] == 0 and out[1] == 3 and out[100] == 300  # every third sample
    # chunked input gives the same stream as one piece (the seam is carried)
    chunked = Resampler(48000, 16000)
    parts = [ramp[:2000], ramp[2000:5000], ramp[5000:]]
    joined = array("h", b"".join(chunked(p) for p in parts))
    assert list(joined) == list(out)
    up = Resampler(8000, 16000)
    first = len(array("h", up(array("h", [0] * 80).tobytes())))
    assert 158 <= first <= 160                        # the seam waits for the next chunk
    second = len(array("h", up(array("h", [0] * 80).tobytes())))
    assert 318 <= first + second <= 320               # and is carried, not lost
    assert Resampler(48000, 16000)(b"") == b""


class FakeInStream:
    def __init__(self):
        self.started = 0
        self.stops = 0
        self.closes = 0
        self.callback = None
        self.rate = None
        self.blocksize = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1


def test_mic_opens_at_the_device_rate_and_hands_over_16k_frames():
    streams = []
    devices = [("Built-in Microphone", 48000), ("WH-1000XM5", 16000)]

    def open_stream(callback, rate, blocksize):
        fs = FakeInStream()
        fs.callback, fs.rate, fs.blocksize = callback, rate, blocksize
        streams.append(fs)
        return fs

    delivered = []

    class Loop:
        def call_soon_threadsafe(self, fn, *args):
            fn(*args)

    mic = Mic(rate=16000, chunk_ms=40, open_stream=open_stream,
              query_device=lambda: devices.pop(0))
    mic.start(Loop(), delivered.append)
    assert (mic.device_name, mic.device_rate) == ("Built-in Microphone", 48000)
    assert streams[0].rate == 48000 and streams[0].blocksize == 1920
    live = array("h", [500] * 1920).tobytes()          # 40 ms at 48 kHz
    streams[0].callback(live, 1920, None, None)
    assert 1276 <= len(delivered[0]) <= 1282           # about 640 samples
    assert mic.last_frame_t is not None and mic.last_live_t is not None
    mic.last_live_t = None
    streams[0].callback(bytes(3840), 1920, None, None)  # digital silence
    assert mic.last_live_t is None and len(delivered) == 2
    # a rebuild re-queries: the headset flipped to its 16 kHz profile
    mic.stop()
    assert streams[0].stops == 1 and streams[0].closes == 1
    mic.start(Loop(), delivered.append)
    assert (mic.device_name, mic.device_rate) == ("WH-1000XM5", 16000)
    assert streams[1].rate == 16000 and streams[1].blocksize == 640
    streams[1].callback(bytes(1280), 640, None, None)
    assert len(delivered[2]) == 1280                    # passed through untouched


# -- playback with a fake stream -----------------------------------------------

class FakeStream:
    def __init__(self):
        self.writes = []
        self.aborts = 0
        self.starts = 0
        self.stops = 0
        self.closes = 0
        self.block = None

    def start(self):
        self.starts += 1

    def write(self, pcm):
        if self.block is not None:
            self.block.wait(timeout=2.0)
        self.writes.append(pcm)

    def abort(self):
        self.aborts += 1
        if self.block is not None:
            self.block.set()

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_disabled_playback_is_a_no_op():
    pb = Playback(enabled=False)
    assert not pb.enabled
    pb.play(b"\x00\x00")
    pb.abort()
    pb.stop()

    async def marker_resolves_at_once():
        await asyncio.wait_for(pb.wait_played(), 1.0)

    asyncio.run(marker_resolves_at_once())


def test_playback_writes_in_order_scales_and_markers_resolve():
    fs = FakeStream()
    pb = Playback(enabled=True, open_stream=lambda: fs)
    assert pb.enabled and fs.starts == 1
    pcm = array("h", [1000, -1000]).tobytes()
    pb.play(pcm, 0.5)
    pb.play(pcm)

    async def scenario():
        await asyncio.wait_for(pb.wait_played(), 2.0)

    asyncio.run(scenario())
    assert fs.writes == [array("h", [500, -500]).tobytes(), pcm]
    pb.stop()
    assert fs.stops == 1 and fs.closes == 1 and not pb.enabled


def test_abort_drops_queued_audio_and_releases_markers():
    fs = FakeStream()
    fs.block = threading.Event()
    pb = Playback(enabled=True, open_stream=lambda: fs)
    pb.play(b"\x00\x00" * 4)  # picked up, blocks in write()
    pb.play(b"\x01\x01" * 4)  # queued

    async def scenario():
        fut = pb.marker()
        assert wait_until(lambda: fs.block is not None and not fs.block.is_set() or True)
        pb.abort()
        await asyncio.wait_for(fut, 2.0)  # a dropped marker still resolves

    asyncio.run(scenario())
    assert fs.aborts == 1 and fs.starts == 2
    time.sleep(0.05)
    assert b"\x01\x01" * 4 not in fs.writes
    pb.stop()


def test_a_failed_open_degrades_to_silence(capsys):
    def boom():
        raise RuntimeError("no such device")

    pb = Playback(enabled=True, open_stream=boom)
    assert not pb.enabled and pb.stream is None
    pb.play(b"\x00\x00")
    assert "no audio output" in capsys.readouterr().err


# -- text helpers ---------------------------------------------------------------

def test_sentence_chunks():
    assert sentence_chunks("One. Two! Three?") == ["One.", "Two!", "Three?"]
    assert sentence_chunks("Just  the one,\n  no gap.") == ["Just the one, no gap."]
    assert sentence_chunks("Editing voice_dev.py now.") == ["Editing voice_dev.py now."]
    assert sentence_chunks("Reading wake.py. Then tests.") == ["Reading wake.py.", "Then tests."]
    assert sentence_chunks("   ") == []


def test_respeller():
    r = Respeller({"dev": "devv", "CLI": "C L I"})
    assert r("the dev seat and the cli") == "the devv seat and the C L I"
    assert r("the device is fine") == "the device is fine"
    assert r("Editing voice_dev.py.") == "Editing voice devv dot pie."
    assert r("x.py.bak stays") == "x.py.bak stays"
    assert Respeller()("untouched text") == "untouched text"
    assert Respeller()("") == ""


def test_readme_is_said_as_two_words_by_default():
    assert Respeller(DEFAULTS["respell"])("Rewrote the README.") == "Rewrote the read me."
