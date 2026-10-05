"""Live check of the ears without a microphone.

Synthesizes an utterance with Cartesia text to speech at 16 kHz, pads
it with silence, and pushes it through the real gate, the real VAD, the
real TurnMachine and the real speech-to-text adapter at real-time pace:

    silence 2 s | "operator, what time is it?" | silence GAP s | "over" | silence 3 s

The gap is longer than any vendor's idle close, so the run proves that
the open turn holds the socket up through a mid-thought pause. It then
reports: the gate opened only for speech, the address word and the
closer were heard, word timings arrived, the latency from the gate's
open to the first transcript, and how the session closed.

    CARTESIA_API_KEY=... DEEPGRAM_API_KEY=... python tools/live_check.py --stt deepgram
    CARTESIA_API_KEY=... python tools/live_check.py --stt cartesia

Keys come from the environment only and are never printed. The voice id
comes from --voice or ~/.config/hands-free-voice/config.toml.
"""

import argparse
import asyncio
import os
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hands_free_voice.config import deep_merge, DEFAULTS  # noqa: E402
from hands_free_voice.gate import Gate  # noqa: E402
from hands_free_voice.providers import Final, Partial, SpeechStarted  # noqa: E402
from hands_free_voice.providers.cartesia import CartesiaTTS  # noqa: E402
from hands_free_voice.providers.registry import make_stt  # noqa: E402
from hands_free_voice.turns import TurnMachine  # noqa: E402
from hands_free_voice.vad import make_vad  # noqa: E402

RATE = 16000
CHUNK_S = 0.04


def silence(seconds: float) -> bytes:
    return bytes(int(RATE * seconds) * 2)


async def synth(tts: CartesiaTTS, text: str) -> bytes:
    out = b""
    async for pcm in tts.speak(text):
        out += pcm
    return out


async def main(args) -> int:
    cfg = deep_merge(DEFAULTS, {"stt": {"provider": args.stt}})
    voice = args.voice
    if not voice:
        user = Path("~/.config/hands-free-voice/config.toml").expanduser()
        if user.exists():
            with open(user, "rb") as f:
                voice = (tomllib.load(f).get("tts") or {}).get("voice")
    if not voice:
        print("no voice id: pass --voice or set [tts] voice in the user config")
        return 2
    key = os.environ.get("CARTESIA_API_KEY")
    if not key:
        print("CARTESIA_API_KEY is not set")
        return 2
    tts = CartesiaTTS(key, voice, sample_rate=RATE)
    t0 = time.monotonic()
    clip1 = await synth(tts, args.text)
    clip2 = await synth(tts, args.closer)
    print(f"tts: {len(clip1) / RATE / 2:.2f}s + {len(clip2) / RATE / 2:.2f}s of 16 kHz audio"
          f" in {time.monotonic() - t0:.2f}s")
    stream = silence(2.0) + clip1 + silence(args.gap) + clip2 + silence(3.0)
    total_s = len(stream) / RATE / 2

    stt = make_stt(cfg)
    machine = TurnMachine("operator", "over")
    start = time.monotonic()
    timeline: list[tuple[float, str]] = []
    marks = {"opened_at": None, "first_text_at": None, "finals": [], "actions": [],
             "words": [], "closed": None, "opens": 0, "onsets": []}

    def now():
        return time.monotonic() - start

    def note(line):
        timeline.append((now(), line))
        print(f"{now():6.2f}s  {line}", flush=True)

    def on_event(ev):
        if isinstance(ev, SpeechStarted):
            marks["onsets"].append(now())
            note("speech onset (VAD)")
        elif isinstance(ev, Partial):
            if marks["first_text_at"] is None:
                marks["first_text_at"] = now()
            note(f"partial: {ev.text!r}")
        elif isinstance(ev, Final):
            if ev.text.strip():
                if marks["first_text_at"] is None:
                    marks["first_text_at"] = now()
                marks["finals"].append(ev.text)
                if ev.words:
                    marks["words"].extend(ev.words)
                acts = machine.feed(ev.text)
                marks["actions"].extend(a[0] for a in acts)
                note(f"final: {ev.text!r} words={[(w.text, w.start_s, w.end_s) for w in (ev.words or ())]}"
                     f" -> {[a[0] for a in acts]}")
                if machine.state == TurnMachine.CLOSING:
                    asyncio.get_running_loop().call_later(0.4, lambda: marks["actions"].extend(
                        a[0] for a in machine.settle()) or note(f"settled: {machine.state}"))
            else:
                note("final: (silence)")

    def log(kind, **fields):
        if fields.get("state") == "open":
            marks["opens"] += 1
            marks["opened_at"] = now()
        if fields.get("state") == "closed":
            marks["closed"] = fields.get("reason")
        note(f"log {kind} {fields}")

    async def open_session():
        return await stt.open(rate=RATE, keyterms=["operator", "over"])

    gate = Gate(open_session, on_event, vad=make_vad(RATE, cfg["gate"]), rate=RATE,
                pre_roll_s=0.5, hangover_s=args.hangover, empty_hangover_s=2.0, deaf_s=8.0,
                hold=lambda: machine.state != TurnMachine.IDLE,
                on_link=lambda up, d=None: note(f"link {'up' if up else 'down'}: {d}"),
                out=lambda s: note(s), log=log)
    await gate.probe()
    note(f"probe ok ({args.stt}); streaming {total_s:.1f}s at real-time pace")
    step = int(RATE * CHUNK_S) * 2
    for i in range(0, len(stream), step):
        gate.feed(stream[i:i + step])
        await asyncio.sleep(CHUNK_S)
    # a real mic keeps delivering silence: so do we, until the hangover closes the gate
    deadline = time.monotonic() + args.hangover + 8.0
    while gate.state != Gate.CLOSED and time.monotonic() < deadline:
        gate.feed(bytes(step))
        await asyncio.sleep(CHUNK_S)
    await gate.stop()

    heard = " ".join(marks["finals"]).lower()
    checks = [
        ("the gate opened once, for speech only", marks["opens"] == 1
         and marks["opened_at"] is not None and 1.5 <= marks["opened_at"] <= 3.5),
        ("the address word was heard", "operator" in heard),
        ("the closer was heard", "over" in heard.split()[-1:] or heard.rstrip(".!? ").endswith("over")),
        ("word timings arrived" if stt.caps.word_timings
         else "word timings: none, as the adapter declares for this model",
         bool(marks["words"]) if stt.caps.word_timings else not marks["words"]),
        ("the turn opened, closed and dispatched",
         "open" in marks["actions"] and "dispatch" in marks["actions"]),
        ("the socket closed on the hangover after the dispatch", marks["closed"] == "hangover"),
    ]
    print()
    print(f"== {args.stt}: heard {heard!r}")
    if marks["opened_at"] is not None and marks["first_text_at"] is not None:
        print(f"   latency from gate open to first transcript:"
              f" {(marks['first_text_at'] - marks['opened_at']) * 1000:.0f} ms"
              f" (onset at {marks['onsets'][0]:.2f}s, open at {marks['opened_at']:.2f}s,"
              f" first text at {marks['first_text_at']:.2f}s)")
    ok = True
    for name, passed in checks:
        ok &= bool(passed)
        print(f"   [{'PASS' if passed else 'FAIL'}] {name}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stt", default="deepgram", choices=["cartesia", "deepgram"])
    ap.add_argument("--voice", default=None)
    ap.add_argument("--text", default="Operator, what time is it?")
    ap.add_argument("--closer", default="Over.")
    ap.add_argument("--gap", type=float, default=12.0,
                    help="seconds of silence inside the open turn (default 12)")
    ap.add_argument("--hangover", type=float, default=3.0)
    sys.exit(asyncio.run(main(ap.parse_args())))
