"""Local voice activity detection for the gate.

The pick is WebRTC's VAD through the `webrtcvad-wheels` package: MIT,
no dependencies, about 70 KB, two microseconds a frame, and it builds
from source in seconds where no wheel exists (Python 3.14 today). A
Silero ONNX model hears better in noise, but it brings onnxruntime and
numpy (tens of megabytes) to a tool whose whole pitch is a couple of
keys and you're talking; the gate's onset rule and its short no-words
hangover absorb what WebRTC gets wrong (a click that opens the gate
costs two seconds of streaming, not a word).

An energy floor sits in front of it: a frame below FLOOR_RMS is never
speech, whatever the model says. Real microphones never deliver digital
silence, but a dead Bluetooth link does, and the model's answer on a
run of zeros after speech is not to be trusted.
"""

import math
from array import array

FRAME_MS = 20            # WebRTC accepts 10, 20 or 30 ms frames
FLOOR_RMS = 80.0         # about -52 dBFS: below this nothing counts as speech
DEFAULT_AGGRESSIVENESS = 2  # 0 (permissive) .. 3 (strict); 2 rejects room noise


def rms(frame: bytes) -> float:
    """Root mean square of a PCM16 mono buffer; 0.0 for an empty one."""
    if len(frame) < 2:
        return 0.0
    samples = array("h", frame[: len(frame) - (len(frame) % 2)])
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def is_digital_silence(frame: bytes) -> bool:
    """Every byte zero: what a dead input device delivers instead of
    stopping."""
    return not frame.strip(b"\x00")


class Framer:
    """Cut arbitrary-sized PCM chunks into fixed-size VAD frames, carrying
    the remainder to the next chunk."""

    def __init__(self, frame_bytes: int):
        self.frame_bytes = frame_bytes
        self._carry = b""

    def frames(self, chunk: bytes) -> list[bytes]:
        buf = self._carry + chunk
        n = len(buf) // self.frame_bytes
        out = [buf[i * self.frame_bytes:(i + 1) * self.frame_bytes] for i in range(n)]
        self._carry = buf[n * self.frame_bytes:]
        return out


class WebRtcVAD:
    """is_speech(frame) for frames of exactly `frame_bytes` PCM16 mono
    at `rate` (8, 16, 32 or 48 kHz)."""

    def __init__(self, rate: int = 16000, aggressiveness: int = DEFAULT_AGGRESSIVENESS,
                 frame_ms: int = FRAME_MS, floor_rms: float = FLOOR_RMS):
        import webrtcvad
        self.rate = rate
        self.frame_ms = frame_ms
        self.frame_bytes = rate * frame_ms // 1000 * 2
        self.floor_rms = floor_rms
        self._vad = webrtcvad.Vad(int(aggressiveness))

    def is_speech(self, frame: bytes) -> bool:
        if len(frame) != self.frame_bytes or rms(frame) < self.floor_rms:
            return False
        return bool(self._vad.is_speech(frame, self.rate))


def make_vad(rate: int, cfg: dict | None = None) -> WebRtcVAD:
    gate = cfg or {}
    return WebRtcVAD(rate, aggressiveness=int(gate.get("vad_aggressiveness",
                                                       DEFAULT_AGGRESSIVENESS)))
