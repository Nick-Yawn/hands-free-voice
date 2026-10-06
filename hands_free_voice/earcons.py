"""Earcons: machinery cues, never narration. Earcons for machinery,
voice for content.

Pure PCM16 mono synthesis, no audio deps. A marimba-ish family:
fundamental plus a quiet octave overtone, quick percussive decay, every
cue at the same level.

  capture      a soft tick: the address word opened a turn
  dispatch     a rising two-note run: the turn is on its way
  abandoned    a falling two-note figure: an open turn was discarded
               (also "cancel" / "never mind": the same meaning)
  command      a short high tick, "got it": a local command was heard
               (again, status, compact, quit)
  stop         a falling minor third, G5 to E5: playback holds
  resume       the same two notes rising, E5 to G5: playback goes on
               (stop and resume share their notes and differ by
               direction, so the ear tells them apart at once)
  still_here   one held note after a quiet stretch while Claude works:
               the first note of the Conet Project's Three Note Oddity
               (about 467 Hz, near B flat 4), keyed on in 15 ms, with a
               faint second harmonic; level, so it never reads as an
               ending. It plays well below the other cues (the working
               cue's tone_volume)
  connected    a triple rising triad: mic and link are up
  closing      the same triad falling: the mirror of connected, played
               to completion before the process exits
  disconnected one long low note: the link dropped
"""

import math
from array import array

SAMPLE_RATE = 24_000

CUE_KEYS = ("capture", "dispatch", "abandoned", "command", "stop", "resume",
            "still_here", "connected", "closing", "disconnected")


def _note(freq: float, dur_s: float, sample_rate: int = SAMPLE_RATE,
          amp: int = 9000) -> array:
    n = max(1, int(sample_rate * dur_s))
    out = array("h")
    for i in range(n):
        t = i / sample_rate
        decay = math.exp(-6.0 * t / dur_s)
        attack = min(1.0, i / 30)
        s = (math.sin(2 * math.pi * freq * i / sample_rate)
             + 0.4 * math.sin(2 * math.pi * 2 * freq * i / sample_rate))
        out.append(int(amp * 0.7 * attack * decay * s))
    return out


def _tone(freq: float, dur_s: float, sample_rate: int = SAMPLE_RATE,
          attack_s: float = 0.02, release_s: float = 0.15, amp: int = 7100,
          harmonics: tuple[tuple[int, float], ...] = ()) -> array:
    """A held tone with raised-cosine edges, plus any (multiple, level)
    harmonics; `amp` is the fundamental's peak."""
    n = max(1, int(sample_rate * dur_s))
    a, r = max(1, int(sample_rate * attack_s)), max(1, int(sample_rate * release_s))
    out = array("h")
    for i in range(n):
        g = 1.0
        if i < a:
            g = 0.5 - 0.5 * math.cos(math.pi * i / a)
        if i > n - r:
            g *= 0.5 - 0.5 * math.cos(math.pi * (n - i) / r)
        ph = 2 * math.pi * freq * i / sample_rate
        s = math.sin(ph) + sum(level * math.sin(k * ph) for k, level in harmonics)
        out.append(int(amp * g * s))
    return out


def _gap(dur_s: float, sample_rate: int = SAMPLE_RATE) -> array:
    return array("h", [0] * int(sample_rate * dur_s))


def _bytes(*parts: array) -> bytes:
    out = array("h")
    for part in parts:
        out.extend(part)
    return out.tobytes()


def get_set(sample_rate: int = SAMPLE_RATE) -> dict[str, bytes]:
    return {
        "capture": _bytes(_note(523.25, 0.12, sample_rate)),                     # C5
        "dispatch": _bytes(_note(523.25, 0.08, sample_rate), _gap(0.02, sample_rate),
                           _note(659.25, 0.12, sample_rate)),                    # C5 -> E5
        "abandoned": _bytes(_note(392.00, 0.10, sample_rate), _gap(0.02, sample_rate),
                            _note(293.66, 0.22, sample_rate)),                   # G4 -> D4
        "command": _bytes(_note(659.25, 0.07, sample_rate)),                     # E5, short
        "stop": _bytes(_note(783.99, 0.07, sample_rate), _gap(0.02, sample_rate),
                       _note(659.25, 0.09, sample_rate)),                        # G5 -> E5
        "resume": _bytes(_note(659.25, 0.07, sample_rate), _gap(0.02, sample_rate),
                         _note(783.99, 0.09, sample_rate)),                      # E5 -> G5
        # the recording's note and length (467 Hz, 0.65 s), softened by ear for
        # laptop speakers: a 15 ms onset, the second harmonic 23 dB down and
        # no third; at 7000 it is as loud as the other cues (the working cue's
        # tone_volume brings it down at play)
        "still_here": _bytes(_tone(467.0, 0.65, sample_rate, attack_s=0.015, release_s=0.04,
                                   amp=7000, harmonics=((2, 0.072),))),
        "connected": _bytes(_note(523.25, 0.10, sample_rate), _gap(0.02, sample_rate),
                            _note(659.25, 0.10, sample_rate), _gap(0.02, sample_rate),
                            _note(783.99, 0.18, sample_rate)),                   # C5 E5 G5
        "closing": _bytes(_note(783.99, 0.10, sample_rate), _gap(0.02, sample_rate),
                          _note(659.25, 0.10, sample_rate), _gap(0.02, sample_rate),
                          _note(523.25, 0.18, sample_rate)),                     # G5 E5 C5
        "disconnected": _bytes(_note(196.00, 0.60, sample_rate)),                # G3, long
    }
