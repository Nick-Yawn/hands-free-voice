"""The provider interface (design §5). The core never calls a vendor
directly: each adapter declares what it can do, and the core fills in
the gaps.

Speech to text: an STT opens a session; PCM16 mono goes in through
send(), events come out of events(). Finals are the minimum an adapter
must provide. Partials drive the live pane and pause-while-talking.
Word timings make the closer rule exact where they exist. Native turn
events power inferred mode (opt-in, not built in this version).

close() is graceful: the adapter asks the vendor to flush whatever audio
it still holds into Finals, lets them reach events(), and only then
closes the socket, within a bound of a couple of seconds. The core
closes a session after every utterance, so the last word of one must
survive the close. Every vendor detail (message names, timeouts,
keep-alives, close handshakes) lives inside the adapter.

Text to speech: speak(text, voice=...) is an async iterator of PCM16
mono bytes at `sample_rate`; stop iterating to cancel. The core handles
volume; the adapter only turns text into audio. There is no speed
control.
"""

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class STTCaps:
    partials: bool = False
    word_timings: bool = False
    native_turns: bool = False
    keyterms: bool = False
    streaming: bool = True


@dataclass(frozen=True)
class TTSCaps:
    streaming: bool = True
    pauses: bool = False
    word_timestamps: bool = False
    pronunciation: bool = False


@dataclass(frozen=True)
class Word:
    text: str
    start_s: float
    end_s: float
    confidence: float = 1.0


@dataclass(frozen=True)
class Partial:
    text: str


@dataclass(frozen=True)
class Final:
    text: str
    words: tuple[Word, ...] | None = None
    speech_final: bool = False  # the vendor's own endpointer fired


@dataclass(frozen=True)
class SpeechStarted:
    pass


@dataclass(frozen=True)
class TurnEnd:
    text: str
    eager: bool = False


@dataclass(frozen=True)
class TurnResumed:
    pass


@dataclass(frozen=True)
class Error:
    message: str


STTEvent = Partial | Final | SpeechStarted | TurnEnd | TurnResumed | Error


@runtime_checkable
class STTSession(Protocol):
    async def send(self, pcm16: bytes) -> None: ...
    def events(self) -> AsyncIterator[STTEvent]: ...
    async def close(self) -> None: ...  # graceful: flush, then close (see above)


@runtime_checkable
class STT(Protocol):
    caps: STTCaps

    async def open(self, rate: int = 16000, keyterms=(), language: str = "en") -> STTSession: ...


@runtime_checkable
class TTS(Protocol):
    caps: TTSCaps
    sample_rate: int

    def speak(self, text: str, *, voice: str | None = None) -> AsyncIterator[bytes]: ...
