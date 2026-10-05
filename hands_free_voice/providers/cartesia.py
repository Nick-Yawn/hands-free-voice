"""Cartesia: text to speech (Sonic) and speech to text (Ink).

Text to speech, wss://api.cartesia.ai/tts/websocket: sentence-sized
sends under one context (continue: true, then an empty transcript with
continue: false to flush), base64 PCM chunks back until `done`. One
websocket per utterance; the context carries prosody across the
sentence seams. There is no speed field: hands-free-voice has no speed control.

Speech to text, wss://api.cartesia.ai/stt/websocket (the manual
endpoint): raw PCM16 binary frames in, `transcript` JSON out with
`is_final` and `text` (a delta since the last final). Measured live
(2026-09-22): ink-2 and ink-preview send finals only, no partials and
no `words`, whatever parameter is tried; ink-whisper sends `words`
with start and end seconds (and `duration`, `language`). The adapter
declares word timings only for ink-whisper, so the core's closer rule
runs on its timer for ink-2. The text frame "finalize" flushes
buffered audio (`flush_done` comes back); "close" flushes it and ends
the session (`done` comes back, and the server closes). close() sends
"close" and waits for `done`, so the last words of an utterance survive
the gate's close. `keyterm` params (at most 100 terms, 1200 characters)
boost the address and closer words. `language` is honored by
ink-whisper only.

Cartesia also offers a turn-detecting endpoint, /stt/turns/websocket,
which emits turn.start / turn.update / turn.eager_end / turn.resume /
turn.end and carries no word timings. It is the native_turns capability
for inferred mode, which the core does not build yet; when it does, a
second session class here maps those messages to TurnEnd / TurnResumed
and nothing above this module changes.
"""

import asyncio
import base64
import contextlib
import json
import urllib.parse
import uuid

from hands_free_voice.providers import Error, Final, Partial, STTCaps, TTSCaps, Word
from hands_free_voice.text import sentence_chunks

API_VERSION = "2026-08-14"

TTS_URL = "wss://api.cartesia.ai/tts/websocket"
DEFAULT_MODEL = "sonic-3.6-2026-08-27"
SAMPLE_RATE = 24000

STT_URL = "wss://api.cartesia.ai/stt/websocket"
TURNS_URL = "wss://api.cartesia.ai/stt/turns/websocket"  # native turns: not built
DEFAULT_STT_MODEL = "ink-2"
KEYTERM_MAX_TERMS = 100
KEYTERM_MAX_CHARS = 1200
CLOSE_FLUSH_S = 2.0  # how long close() waits for `done`


# -- text to speech -------------------------------------------------------------------

def request_json(model: str, voice_id: str, context_id: str, transcript: str,
                 cont: bool, language: str = "en", sample_rate: int = SAMPLE_RATE) -> str:
    return json.dumps({
        "model_id": model,
        "transcript": transcript,
        "voice": {"id": voice_id},
        "output_format": {"container": "raw", "encoding": "pcm_s16le",
                          "sample_rate": sample_rate},
        "language": language,
        "context_id": context_id,
        "continue": cont,
    })


def parse_message(msg: dict, context_id: str):
    """("chunk", bytes) | ("done", None) | ("error", message) | None."""
    if not isinstance(msg, dict):
        return None
    if msg.get("context_id") not in (None, context_id):
        return None
    kind = msg.get("type")
    if kind == "chunk" and msg.get("data"):
        return ("chunk", base64.b64decode(msg["data"]))
    if kind == "done":
        return ("done", None)
    if kind == "error":
        return ("error", str(msg.get("message") or msg))
    return None


class CartesiaTTS:
    caps = TTSCaps(streaming=True, pauses=False, word_timestamps=False,
                   pronunciation=False)
    default_model = DEFAULT_MODEL

    def __init__(self, api_key: str, voice_id: str, model: str = DEFAULT_MODEL,
                 language: str = "en", connect=None, sample_rate: int = SAMPLE_RATE):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.language = language
        self.sample_rate = sample_rate
        self._connect = connect or self._websocket

    @staticmethod
    def _websocket(url: str, headers: dict):
        import websockets
        return websockets.connect(url, additional_headers=headers)

    async def speak(self, text: str, *, voice: str | None = None):
        voice_id = voice or self.voice_id
        context_id = uuid.uuid4().hex
        chunks = sentence_chunks(text)
        if not chunks:
            return
        url = f"{TTS_URL}?cartesia_version={API_VERSION}"
        async with self._connect(url, {"X-API-Key": self.api_key}) as ws:
            for chunk in chunks:
                await ws.send(request_json(self.model, voice_id, context_id, chunk,
                                           True, self.language, self.sample_rate))
            await ws.send(request_json(self.model, voice_id, context_id, "",
                                       False, self.language, self.sample_rate))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                parsed = parse_message(msg, context_id)
                if parsed is None:
                    continue
                kind, payload = parsed
                if kind == "chunk":
                    yield payload
                elif kind == "done":
                    return
                else:
                    raise RuntimeError(f"cartesia: {payload}")


def aclosing(gen):
    return contextlib.aclosing(gen)


# -- speech to text -------------------------------------------------------------------

def bounded_keyterms(keyterms) -> list[str]:
    """At most KEYTERM_MAX_TERMS terms and KEYTERM_MAX_CHARS characters,
    in the order given (the address and closer words come first)."""
    out: list[str] = []
    chars = 0
    for term in keyterms:
        term = str(term or "").strip()
        if not term:
            continue
        if len(out) >= KEYTERM_MAX_TERMS or chars + len(term) > KEYTERM_MAX_CHARS:
            break
        out.append(term)
        chars += len(term)
    return out


def stt_params(model: str, rate: int, keyterms=(), language: str = "en") -> list[tuple]:
    params = [
        ("cartesia_version", API_VERSION),
        ("model", model),
        ("encoding", "pcm_s16le"),
        ("sample_rate", rate),
    ]
    if model.startswith("ink-whisper") and language:
        params.append(("language", language))
    params += [("keyterm", k) for k in bounded_keyterms(keyterms)]
    return params


def stt_url(model: str, rate: int, keyterms=(), language: str = "en") -> str:
    return f"{STT_URL}?{urllib.parse.urlencode(stt_params(model, rate, keyterms, language))}"


def parse_stt_message(msg: dict):
    """One STT message -> Partial | Final | Error | "done" | None
    (flush_done and anything unknown)."""
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "transcript":
        text = (msg.get("text") or "").strip()
        if not msg.get("is_final"):
            return Partial(text) if text else None
        words = tuple(
            Word(str(w.get("word") or ""), float(w.get("start") or 0.0),
                 float(w.get("end") or 0.0), 1.0)
            for w in (msg.get("words") or []) if isinstance(w, dict))
        return Final(text, words, False)
    if kind == "done":
        return "done"
    if kind == "error":
        title = msg.get("title")
        message = str(msg.get("message") or msg)
        return Error(f"{title}: {message}" if title else message)
    return None


class CartesiaSession:
    def __init__(self, ws, flush_s: float = CLOSE_FLUSH_S):
        self._ws = ws
        self._flush_s = flush_s
        self._closing = False
        self._reading = False
        self._ended = asyncio.Event()

    async def send(self, pcm16: bytes) -> None:
        await self._ws.send(pcm16)

    async def events(self):
        self._reading = True
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                ev = parse_stt_message(msg)
                if ev is None:
                    continue
                if ev == "done":
                    return
                yield ev
        except Exception as exc:  # a closed or broken link: the caller reconnects
            if not self._closing:
                yield Error(f"link: {exc!r}")
        finally:
            self._reading = False
            self._ended.set()

    async def close(self) -> None:
        """Graceful: "close", then the flushed finals and `done` reach
        events() (bounded by flush_s); then the socket closes."""
        if self._closing:
            return
        self._closing = True
        try:
            await self._ws.send("close")
            if self._reading:
                await asyncio.wait_for(self._ended.wait(), self._flush_s)
        except Exception:
            pass
        try:
            await self._ws.close()
        except Exception:
            pass


def stt_caps(model: str) -> STTCaps:
    """What the model was measured to deliver over the manual endpoint."""
    whisper = model.startswith("ink-whisper")
    return STTCaps(partials=False, word_timings=whisper, native_turns=False,
                   keyterms=not whisper, streaming=True)


class CartesiaSTT:
    default_model = DEFAULT_STT_MODEL

    def __init__(self, api_key: str, model: str = DEFAULT_STT_MODEL, connect=None):
        self.api_key = api_key
        self.model = model
        self.caps = stt_caps(model)
        self._connect = connect or self._websocket

    @staticmethod
    async def _websocket(url: str, headers: dict):
        import websockets
        return await websockets.connect(url, additional_headers=headers)

    async def open(self, rate: int = 16000, keyterms=(), language: str = "en") -> CartesiaSession:
        url = stt_url(self.model, rate, keyterms, language)
        ws = await self._connect(url, {"X-API-Key": self.api_key})
        return CartesiaSession(ws)
