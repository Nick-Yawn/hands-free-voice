"""The local VAD and its framing."""

import math
import random
from array import array

from hands_free_voice.vad import FLOOR_RMS, Framer, WebRtcVAD, is_digital_silence, make_vad, rms

RATE = 16000
N = 320  # a 20 ms frame


def frame(gen) -> bytes:
    return array("h", (max(-32768, min(32767, int(gen(i)))) for i in range(N))).tobytes()


def test_rms_and_digital_silence():
    assert rms(b"") == 0.0 and rms(bytes(640)) == 0.0
    assert rms(array("h", [1000] * N).tobytes()) == 1000.0
    assert is_digital_silence(bytes(640)) and not is_digital_silence(b"\x00\x01" + bytes(638))


def test_framer_cuts_chunks_and_carries_the_remainder():
    f = Framer(640)
    assert f.frames(bytes(1280)) == [bytes(640), bytes(640)]
    assert f.frames(bytes(700)) == [bytes(640)]
    assert f.frames(bytes(580)) == [bytes(640)]  # 60 carried + 580
    assert f.frames(b"") == []


def test_webrtc_vad_rejects_silence_and_noise_and_hears_a_voice_like_signal():
    vad = make_vad(RATE, {"vad_aggressiveness": 2})
    assert isinstance(vad, WebRtcVAD) and vad.frame_bytes == 640
    random.seed(7)
    assert not vad.is_speech(bytes(640))                              # digital zeros
    assert not vad.is_speech(frame(lambda i: random.gauss(0, 40)))    # room noise
    assert not vad.is_speech(bytes(320))                              # the wrong size
    voiced = frame(lambda i: 6000 * math.sin(2 * math.pi * 180 * i / RATE)
                   * (0.6 + 0.4 * math.sin(2 * math.pi * 5 * i / RATE))
                   + 2500 * math.sin(2 * math.pi * 900 * i / RATE))
    assert rms(voiced) > FLOOR_RMS and vad.is_speech(voiced)
    quiet_voice = frame(lambda i: 20 * math.sin(2 * math.pi * 180 * i / RATE))
    assert not vad.is_speech(quiet_voice)                             # under the floor
