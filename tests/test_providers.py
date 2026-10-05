"""The speech-to-text contract, run over every adapter against a
scripted websocket; then each vendor's own builders and parsers, and
the Cartesia text-to-speech adapter."""

import asyncio
import base64
import json
import urllib.parse

import pytest

from hands_free_voice.providers import Error, Final, Partial, SpeechStarted, STT, TTS, Word
from hands_free_voice.providers.cartesia import (
    CartesiaSTT,
    CartesiaTTS,
    parse_message as parse_cartesia,
    parse_stt_message,
    request_json,
    stt_url,
)
from hands_free_voice.providers.deepgram import DeepgramSTT, listen_url, parse_message as parse_deepgram
from hands_free_voice.providers.fake import FakeSTT, FakeTTS


class FakeWS:
    """Both vendors' websockets: send() records, iteration yields scripted
    messages, close() records. Usable as an async context manager and as
    an awaitable (the two shapes the adapters use)."""

    def __init__(self, incoming=()):
        self.sent = []
        self.incoming = list(incoming)
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def __await__(self):
        async def me():
            return self
        return me().__await__()


class ScriptedWS:
    """A websocket the test drives: incoming messages are queued, so a
    reader blocks like a real one; a scripted reply to the vendor's
    close message stands in for the server's flush and close."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self._q: asyncio.Queue = asyncio.Queue()
        self.on_close_message = None  # (message) -> list of replies, or None

    def feed(self, *msgs):
        for m in msgs:
            self._q.put_nowait(m)

    def end(self):
        self._q.put_nowait(StopAsyncIteration())

    async def send(self, data):
        self.sent.append(data)
        if self.on_close_message is not None:
            replies = self.on_close_message(data)
            if replies is not None:
                self.feed(*replies)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if isinstance(item, StopAsyncIteration):
            raise item
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True
        self.end()


class Deepgram:
    """Deepgram's dialect of the contract."""
    name = "deepgram"
    headers = {"Authorization": "Token the-key"}
    url_head = "wss://api.deepgram.com/v1/listen?"

    @staticmethod
    def make(connect):
        return DeepgramSTT("the-key", connect=connect)

    @staticmethod
    def partial(text):
        return json.dumps({"type": "Results", "is_final": False,
                           "channel": {"alternatives": [{"transcript": text}]}})

    @staticmethod
    def final(text, words):
        return json.dumps({"type": "Results", "is_final": True, "channel": {"alternatives": [
            {"transcript": text,
             "words": [{"word": w, "start": a, "end": b, "confidence": 0.9} for w, a, b in words]}]}})

    @staticmethod
    def error(msg):
        return json.dumps({"type": "Error", "message": msg})

    @staticmethod
    def is_close(data):
        return isinstance(data, str) and json.loads(data) == {"type": "CloseStream"}

    @staticmethod
    def close_ack():
        return [json.dumps({"type": "Metadata"}), StopAsyncIteration()]  # the server closes


class Cartesia:
    """Cartesia's dialect of the contract."""
    name = "cartesia"
    headers = {"X-API-Key": "the-key"}
    url_head = "wss://api.cartesia.ai/stt/websocket?"

    @staticmethod
    def make(connect):
        return CartesiaSTT("the-key", connect=connect)

    @staticmethod
    def partial(text):
        return json.dumps({"type": "transcript", "is_final": False, "request_id": "r",
                           "text": text})

    @staticmethod
    def final(text, words):
        return json.dumps({"type": "transcript", "is_final": True, "request_id": "r",
                           "text": text, "duration": 1.5,
                           "words": [{"word": w, "start": a, "end": b} for w, a, b in words]})

    @staticmethod
    def error(msg):
        return json.dumps({"type": "error", "status_code": 400, "title": "Bad request",
                           "message": msg})

    @staticmethod
    def is_close(data):
        return data == "close"

    @staticmethod
    def close_ack():
        return [json.dumps({"type": "done", "request_id": "r"})]


DIALECTS = [Deepgram, Cartesia]


def _connect(ws, seen):
    async def connect(url, headers):
        seen["url"], seen["headers"] = url, headers
        return ws
    return connect


# -- the contract, over every adapter ---------------------------------------------

@pytest.mark.parametrize("vendor", DIALECTS, ids=lambda v: v.name)
def test_contract_open_carries_key_rate_and_keyterms(vendor):
    async def scenario():
        ws, seen = ScriptedWS(), {}
        stt = vendor.make(_connect(ws, seen))
        assert isinstance(stt, STT)
        assert stt.caps.keyterms and stt.caps.streaming
        session = await stt.open(rate=16000, keyterms=["operator", "over"])
        assert seen["headers"] == vendor.headers
        assert seen["url"].startswith(vendor.url_head)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
        assert q["sample_rate"] == ["16000"] and q["keyterm"] == ["operator", "over"]
        assert isinstance(session.events(), object)

    asyncio.run(scenario())


@pytest.mark.parametrize("vendor", DIALECTS, ids=lambda v: v.name)
def test_contract_audio_passes_through_and_transcripts_come_back_typed(vendor):
    async def scenario():
        ws = ScriptedWS()
        session = await vendor.make(_connect(ws, {})).open()
        await session.send(b"\x01\x02" * 4)
        assert ws.sent == [b"\x01\x02" * 4]
        ws.feed(vendor.partial("   "), vendor.partial("opera"), "not json", b"binary noise",
                vendor.final("operator, run it over", [("operator", 0.1, 0.5), ("over", 1.0, 1.3)]))
        ws.end()
        events = [ev async for ev in session.events()]
        assert events[0] == Partial("opera")
        final = events[1]
        assert isinstance(final, Final) and final.text == "operator, run it over"
        assert final.words[0] == Word("operator", 0.1, 0.5, final.words[0].confidence)
        assert (final.words[1].text, final.words[1].start_s, final.words[1].end_s) == ("over", 1.0, 1.3)
        assert len(events) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("vendor", DIALECTS, ids=lambda v: v.name)
def test_contract_close_is_graceful_and_idempotent(vendor):
    """close() sends the vendor's close message and the finals the server
    flushes in reply still reach a reader; then the socket closes."""
    async def scenario():
        ws = ScriptedWS()
        session = await vendor.make(_connect(ws, {})).open()
        ws.on_close_message = lambda data: (
            [vendor.final("over", [("over", 2.0, 2.3)])] + vendor.close_ack()
            if vendor.is_close(data) else None)
        got = []

        async def read():
            async for ev in session.events():
                got.append(ev)

        reader = asyncio.ensure_future(read())
        await asyncio.sleep(0.01)
        await session.close()
        await asyncio.wait_for(reader, 1.0)
        assert got == [Final("over", (Word("over", 2.0, 2.3, got[0].words[0].confidence),), False)]
        assert ws.closed and sum(1 for d in ws.sent if vendor.is_close(d)) == 1
        await session.close()  # a second close sends nothing more
        assert sum(1 for d in ws.sent if vendor.is_close(d)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("vendor", DIALECTS, ids=lambda v: v.name)
def test_contract_server_errors_and_broken_links_become_error_events(vendor):
    async def scenario():
        ws = ScriptedWS()
        session = await vendor.make(_connect(ws, {})).open()
        ws.feed(vendor.error("bad key"))
        ws.end()
        events = [ev async for ev in session.events()]
        assert len(events) == 1 and isinstance(events[0], Error) and "bad key" in events[0].message
        ws2 = ScriptedWS()
        session2 = await vendor.make(_connect(ws2, {})).open()
        ws2.feed(RuntimeError("1011 keepalive timeout"))
        events = [ev async for ev in session2.events()]
        assert len(events) == 1 and isinstance(events[0], Error) and "1011" in events[0].message
        assert vendor.name not in events[0].message  # the core never learns the vendor

    asyncio.run(scenario())


# -- Deepgram -------------------------------------------------------------

def test_listen_url_carries_model_encoding_and_keyterms():
    url = listen_url("nova-3", 16000, ["operator", "over"], "en")
    assert url.startswith("wss://api.deepgram.com/v1/listen?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["model"] == ["nova-3"] and q["encoding"] == ["linear16"]
    assert q["sample_rate"] == ["16000"] and q["channels"] == ["1"]
    assert q["interim_results"] == ["true"] and q["vad_events"] == ["true"]
    assert q["keyterm"] == ["operator", "over"]
    assert "keyterm" not in urllib.parse.parse_qs(
        urllib.parse.urlparse(listen_url("nova-3", 16000, [], "en")).query)
    # out of Deepgram's model training unless the config says otherwise
    assert q["mip_opt_out"] == ["true"]
    assert "mip_opt_out" not in urllib.parse.parse_qs(urllib.parse.urlparse(
        listen_url("nova-3", 16000, [], "en", training_opt_out=False)).query)


def test_parse_deepgram_messages():
    assert parse_deepgram({"type": "Metadata"}) is None
    assert parse_deepgram({"type": "SpeechStarted"}) == SpeechStarted()
    assert parse_deepgram({"type": "Error", "message": "bad key"}) == Error("bad key")
    empty_partial = {"type": "Results", "is_final": False,
                     "channel": {"alternatives": [{"transcript": "   "}]}}
    assert parse_deepgram(empty_partial) is None
    empty_final = {"type": "Results", "is_final": True, "speech_final": True,
                   "channel": {"alternatives": [{"transcript": ""}]}}
    assert parse_deepgram(empty_final) == Final("", None, True)  # silence is news
    partial = {"type": "Results", "is_final": False,
               "channel": {"alternatives": [{"transcript": "operator run"}]}}
    assert parse_deepgram(partial) == Partial("operator run")
    final = {"type": "Results", "is_final": True, "speech_final": True,
             "channel": {"alternatives": [{"transcript": "Operator, run it. Over.",
                                           "words": [
                                               {"word": "operator", "punctuated_word": "Operator,",
                                                "start": 0.1, "end": 0.5, "confidence": 0.99},
                                               {"word": "over", "start": 1.0, "end": 1.3,
                                                "confidence": 0.9}]}]}}
    ev = parse_deepgram(final)
    assert isinstance(ev, Final) and ev.text == "Operator, run it. Over." and ev.speech_final
    assert [w.text for w in ev.words] == ["Operator,", "over"]
    assert ev.words[1].start_s == 1.0 and ev.words[1].end_s == 1.3
    assert parse_deepgram(["not", "a", "dict"]) is None


def test_deepgram_session_streams_events_and_closes():
    async def scenario():
        ws = FakeWS([
            json.dumps({"type": "Results", "is_final": False,
                        "channel": {"alternatives": [{"transcript": "hel"}]}}),
            b"binary noise",
            "not json",
            json.dumps({"type": "Results", "is_final": True,
                        "channel": {"alternatives": [{"transcript": "hello"}]}}),
        ])
        seen = {}

        async def connect(url, headers):
            seen["url"], seen["headers"] = url, headers
            return ws

        stt = DeepgramSTT("dg-key", connect=connect)
        assert isinstance(stt, STT)
        session = await stt.open(rate=16000, keyterms=["operator", "over"])
        assert seen["headers"] == {"Authorization": "Token dg-key"}
        assert "keyterm=operator" in seen["url"]
        await session.send(b"\x00\x00")
        assert ws.sent == [b"\x00\x00"]
        events = [ev async for ev in session.events()]
        assert events == [Partial("hel"), Final("hello", None, False)]
        await session.close()
        assert ws.closed and json.loads(ws.sent[-1]) == {"type": "CloseStream"}
        await session.close()  # idempotent
        assert ws.sent.count(json.dumps({"type": "CloseStream"})) == 1

    asyncio.run(scenario())


def test_deepgram_link_failure_surfaces_as_an_error_event():
    async def scenario():
        ws = FakeWS([RuntimeError("1011 keepalive timeout")])
        stt = DeepgramSTT("k", connect=lambda url, headers: ws)
        session = await stt.open()
        events = [ev async for ev in session.events()]
        assert len(events) == 1 and isinstance(events[0], Error)
        assert "1011" in events[0].message

    asyncio.run(scenario())


# -- Cartesia speech to text --------------------------------------------------

def test_stt_url_carries_version_model_encoding_and_bounded_keyterms():
    url = stt_url("ink-2", 16000, ["operator", "over"])
    assert url.startswith("wss://api.cartesia.ai/stt/websocket?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["cartesia_version"] == ["2026-08-14"] and q["model"] == ["ink-2"]
    assert q["encoding"] == ["pcm_s16le"] and q["sample_rate"] == ["16000"]
    assert q["keyterm"] == ["operator", "over"] and "language" not in q
    # language rides only for the whisper model; keyterms stop at 100 / 1200 chars
    assert "language=de" in stt_url("ink-whisper", 16000, [], "de")
    many = [f"word{i}" for i in range(150)]
    assert len(urllib.parse.parse_qs(urllib.parse.urlparse(
        stt_url("ink-2", 16000, many)).query)["keyterm"]) == 100
    longs = ["x" * 500, "y" * 500, "z" * 500]
    assert urllib.parse.parse_qs(urllib.parse.urlparse(
        stt_url("ink-2", 16000, longs)).query)["keyterm"] == ["x" * 500, "y" * 500]


def test_cartesia_caps_follow_the_model():
    assert CartesiaSTT("k").caps.word_timings is False          # ink-2, measured live
    assert CartesiaSTT("k").caps.partials is False
    assert CartesiaSTT("k", model="ink-whisper").caps.word_timings is True
    assert CartesiaSTT("k", model="ink-whisper").caps.keyterms is False


def test_parse_stt_messages():
    assert parse_stt_message({"type": "flush_done", "request_id": "r"}) is None
    assert parse_stt_message({"type": "done"}) == "done"
    assert parse_stt_message(["nope"]) is None
    assert parse_stt_message({"type": "transcript", "is_final": False, "text": "  "}) is None
    assert parse_stt_message({"type": "transcript", "is_final": False, "text": "hel"}) == \
        Partial("hel")
    ev = parse_stt_message({"type": "transcript", "is_final": True, "text": "hello there",
                            "duration": 1.2, "words": [{"word": "hello", "start": 0.2, "end": 0.5},
                                                       {"word": "there", "start": 0.6, "end": 0.9}]})
    assert ev == Final("hello there", (Word("hello", 0.2, 0.5, 1.0), Word("there", 0.6, 0.9, 1.0)),
                       False)
    assert parse_stt_message({"type": "transcript", "is_final": True, "text": ""}) == \
        Final("", (), False)
    err = parse_stt_message({"type": "error", "status_code": 401, "title": "Unauthorized",
                             "message": "invalid api key"})
    assert err == Error("Unauthorized: invalid api key")


# -- Cartesia text to speech ---------------------------------------------------------

def test_request_json_shape_has_no_speed_field():
    req = json.loads(request_json("sonic-3.6-2026-08-27", "voice-1", "ctx", "Hello.", True))
    assert req == {
        "model_id": "sonic-3.6-2026-08-27", "transcript": "Hello.",
        "voice": {"id": "voice-1"},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
        "language": "en", "context_id": "ctx", "continue": True}
    assert "generation_config" not in req and "speed" not in req
    assert json.loads(request_json("m", "v", "ctx", "", False))["continue"] is False


def test_parse_cartesia_messages():
    data = base64.b64encode(b"\x01\x02").decode()
    assert parse_cartesia({"type": "chunk", "data": data, "context_id": "c"}, "c") == \
        ("chunk", b"\x01\x02")
    assert parse_cartesia({"type": "chunk", "data": data, "context_id": "other"}, "c") is None
    assert parse_cartesia({"type": "done"}, "c") == ("done", None)
    assert parse_cartesia({"type": "error", "message": "voice not found"}, "c") == \
        ("error", "voice not found")
    assert parse_cartesia({"type": "timestamps"}, "c") is None


def test_cartesia_speak_sends_sentences_then_flush_and_yields_pcm():
    async def scenario():
        pcm1, pcm2 = b"\x00\x01" * 10, b"\x02\x03" * 10
        ws = FakeWS()
        seen = {}

        def connect(url, headers):
            seen["url"], seen["headers"] = url, headers
            ws.incoming = [
                json.dumps({"type": "chunk", "data": base64.b64encode(pcm1).decode()}),
                json.dumps({"type": "chunk", "data": base64.b64encode(pcm2).decode()}),
                json.dumps({"type": "done"}),
                json.dumps({"type": "chunk", "data": "never read"}),
            ]
            return ws

        tts = CartesiaTTS("ck", "voice-1", connect=connect)
        assert isinstance(tts, TTS) and tts.sample_rate == 24000
        out = [pcm async for pcm in tts.speak("First one. Second one!", voice=None)]
        assert out == [pcm1, pcm2]
        assert seen["headers"] == {"X-API-Key": "ck"}
        assert seen["url"] == "wss://api.cartesia.ai/tts/websocket?cartesia_version=2026-08-14"
        reqs = [json.loads(s) for s in ws.sent]
        assert [(r["transcript"], r["continue"]) for r in reqs] == \
            [("First one.", True), ("Second one!", True), ("", False)]
        assert len({r["context_id"] for r in reqs}) == 1
        assert all(r["voice"]["id"] == "voice-1" for r in reqs)
        assert ws.closed
        # an explicit voice overrides the default; empty text sends nothing
        ws2 = FakeWS([json.dumps({"type": "done"})])
        tts2 = CartesiaTTS("ck", "voice-1", connect=lambda u, h: ws2)
        assert [p async for p in tts2.speak("Hi.", voice="voice-2")] == []
        assert json.loads(ws2.sent[0])["voice"]["id"] == "voice-2"
        assert [p async for p in tts2.speak("   ")] == []

    asyncio.run(scenario())


def test_cartesia_error_raises():
    async def scenario():
        ws = FakeWS([json.dumps({"type": "error", "message": "voice not found"})])
        tts = CartesiaTTS("ck", "v", connect=lambda u, h: ws)
        with pytest.raises(RuntimeError, match="voice not found"):
            async for _ in tts.speak("Hello."):
                pass
        assert ws.closed

    asyncio.run(scenario())


def test_cartesia_cancel_mid_stream_closes_the_socket():
    async def scenario():
        ws = FakeWS([json.dumps({"type": "chunk", "data": base64.b64encode(b"\x00\x00").decode()})
                     for _ in range(5)] + [json.dumps({"type": "done"})])
        tts = CartesiaTTS("ck", "v", connect=lambda u, h: ws)
        import contextlib
        async with contextlib.aclosing(tts.speak("Hello there.")) as chunks:
            async for _ in chunks:
                break  # the consumer stops iterating: cancel
        assert ws.closed

    asyncio.run(scenario())


# -- the fakes honor the interface -----------------------------------------

def test_fakes_are_providers():
    async def scenario():
        stt = FakeSTT()
        assert isinstance(stt, STT)
        session = await stt.open(keyterms=["operator"])
        stt.queue.put_nowait(Final("hi"))
        stt.queue.put_nowait(None)
        assert [ev async for ev in session.events()] == [Final("hi")]
        assert stt.opened_with[0]["keyterms"] == ["operator"]
        tts = FakeTTS()
        assert isinstance(tts, TTS)
        chunks = [c async for c in tts.speak("Hello.", voice="v")]
        assert len(chunks) == 2 and tts.spoken == [("Hello.", "v")]

    asyncio.run(scenario())
