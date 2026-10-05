"""The voice front end.

  mic -> Gate (local VAD; the STT session exists only while you talk)
      -> TurnMachine -> Host (Seat)
  Seat events -> SpokenLog -> TTS -> Playback (speaker)

Plus the earcons, the closer's settle window, pause-while-talking, and
the mic-starvation watchdog. The gate owns the speech-to-text session:
its opens, its flushes, its reconnects. Every piece of audio hardware
and every vendor is injectable, so the whole loop runs offline in tests
with fake providers.
"""

import asyncio
import contextlib
import sys
import time

from hands_free_voice.app import Host
from hands_free_voice.audio import MIC_RATE, Mic, Playback, watchdog_tick
from hands_free_voice.earcons import get_set
from hands_free_voice.gate import Gate
from hands_free_voice.providers import Final, Partial, SpeechStarted, TurnEnd
from hands_free_voice.scrub import Scrubber
from hands_free_voice.spoken_log import SpokenLog
from hands_free_voice.text import Respeller
from hands_free_voice.turns import TurnMachine
from hands_free_voice.vad import make_vad

MIC_CONSTRUCT_MAX_FAILURES = 5
WATCHDOG_INTERVAL_S = 2.0
ABANDON_INTERVAL_S = 5.0


def make_speaker(tts, playback: Playback, scrubber: Scrubber, respell: Respeller,
                 volumes: dict, voice: str | None):
    """The SpokenLog's speak(): scrub and respell, synthesize, play at the
    register's volume, and return once the audio has played out. A
    cancel (pause, replay, quit) aborts queued audio at once."""

    async def speak(text: str, register: str) -> None:
        audio_text = scrubber(respell(text))
        gain = float(volumes.get(register, 1.0))
        try:
            async with contextlib.aclosing(tts.speak(audio_text, voice=voice)) as chunks:
                carry = b""
                async for pcm in chunks:
                    buf = carry + pcm
                    if len(buf) % 2:  # frame-align: a half sample would raise mid-word
                        buf, carry = buf[:-1], buf[-1:]
                    else:
                        carry = b""
                    playback.play(buf, gain)
            await playback.wait_played()
        except asyncio.CancelledError:
            playback.abort()
            raise

    return speak


class VoiceFront:
    def __init__(self, host: Host, cfg: dict, *, stt, tts, playback: Playback, mic: Mic,
                 vad=None, clock=time.monotonic, on_state=None):
        self.host = host
        self.cfg = cfg
        self.stt = stt
        self.tts = tts
        self.playback = playback
        self.mic = mic
        self._clock = clock
        self._out = host._out
        words = cfg["words"]
        turns = cfg["turns"]
        self.address = words["address"]
        self.closer = words["closer"]
        self.language = cfg["stt"].get("language", "en")
        self.settle_s = float(turns["closer_settle_s"])
        self.machine = TurnMachine(
            self.address, self.closer,
            filler_before_address=bool(words.get("filler_before_address")),
            readdress_gap_s=float(turns["readdress_gap_s"]),
            idle_turn_s=float(turns["idle_turn_s"]), clock=clock)
        self.earcons = get_set(playback.rate)
        self.earcon_gain = float(cfg["volumes"].get("earcons", 1.0))
        self.link_up = False
        self._settle: asyncio.Task | None = None
        self.pass_start = clock()
        self._tasks: list[asyncio.Task] = []
        self._rebuild = asyncio.Event()
        self._rebuild_reason: str | None = None
        gate_cfg = cfg.get("gate") or {}
        self.vad = vad or make_vad(mic.rate, gate_cfg)
        self.gate = Gate(
            self._open_session, self.on_stt_event, vad=self.vad, rate=mic.rate,
            pre_roll_s=float(gate_cfg.get("pre_roll_s", 0.5)),
            hangover_s=float(gate_cfg.get("hangover_s", 10.0)),
            empty_hangover_s=float(gate_cfg.get("empty_hangover_s", 2.0)),
            deaf_s=float(gate_cfg.get("deaf_s", 8.0)),
            hold=lambda: self.machine.state != TurnMachine.IDLE,
            on_link=self._gate_link,
            on_deaf=lambda: self.request_rebuild("voice but no words, twice"),
            out=self._out, log=host.log.write, clock=clock)
        host.status_extra = self.status_extra
        host.on_still_here = lambda: self.cue("still_here")
        self.on_state = on_state  # (state) whenever state() changes
        self._shown: dict | None = None
        self._mic_trouble: str | None = None
        self._link_trouble: str | None = None
        self._partial = ""  # the speech-to-text's words in progress, shown as said
        host.spoken.on_change = self.publish

    async def _open_session(self):
        return await self.stt.open(rate=self.mic.rate,
                                   keyterms=[self.address, self.closer],
                                   language=self.language)

    # -- cues ------------------------------------------------------------------

    def cue(self, name: str) -> None:
        self.playback.play(self.earcons[name], self.earcon_gain)

    def _link(self, up: bool) -> None:
        self._link_trouble = None if up else "speech link down"
        self.publish()
        if up == self.link_up:
            return
        self.link_up = up
        self.cue("connected" if up else "disconnected")
        self.host.log.write("link", up=up)

    def _gate_link(self, up: bool, detail: str | None = None) -> None:
        if detail:
            self._out(f"[ears: {detail}]")
        self._link(up)

    def request_rebuild(self, reason: str) -> None:
        """Ask the ears loop to stop and restart the input device."""
        self._rebuild_reason = reason
        self._rebuild.set()

    # -- the status line ---------------------------------------------------------

    def state(self) -> dict:
        """What the ears and the voice are doing, for the mod's status line:
        trouble, hearing (with the turn's last three words, words still
        in progress included), paused, speaking or idle, first match wins.
        Whether Claude is working is the mod's to know."""
        trouble = self._mic_trouble or self._link_trouble
        if trouble:
            return {"state": "trouble", "detail": trouble}
        words = self.machine.preview(self._partial) if self._partial else None
        if words is None and self.machine.state != TurnMachine.IDLE:
            words = self.machine.text()
        if words is not None:
            return {"state": "hearing", "words": " ".join(words.split()[-3:])}
        spoken = self.host.spoken
        if "user" in spoken.holds:
            return {"state": "paused"}
        if spoken.busy or (spoken.backlog and not spoken.paused):
            return {"state": "speaking"}
        return {"state": "idle"}

    def publish(self) -> None:
        if self.on_state is None:
            return
        state = self.state()
        if state != self._shown:
            self._shown = state
            self.on_state(state)

    def status_extra(self) -> dict:
        t = self.mic.last_frame_t
        return {"link_up": self.link_up,
                "mic_age_s": (self._clock() - t) if t is not None else None}

    # -- the turn machine's actions --------------------------------------------

    def handle_actions(self, acts: list[tuple]) -> None:
        cued_abandoned = False
        for act in acts:
            kind = act[0]
            if kind == "drop":
                self._out(f"  ∅ ignored: {act[1]}")
                self.host.log.write("dropped", text=act[1])
            elif kind == "open":
                self.host.spoken.user_input()
                self.host.spoken.pause("talk")
                self.cue("capture")
                self._out(f"you ▸ {act[1] or ''}")
                self.host.log.write("open", text=act[1])
            elif kind == "append":
                self._out(f"      {act[1]}")
            elif kind == "closing":
                pass  # the settle window is armed below
            elif kind == "dispatch":
                self.host.spoken.resume("talk")
                if act[1]:
                    self.cue("dispatch")
                    self.host.enqueue_turn(act[1])
                else:
                    self._out("  (empty turn, nothing sent)")
            elif kind == "command":
                self.host.spoken.resume("talk")
                self.command(act[1], act[2], cued_abandoned=cued_abandoned)
            elif kind == "abandoned":
                self.host.spoken.resume("talk")
                self.cue("abandoned")
                cued_abandoned = True
                self._out(f"  [discarded: {act[1]}]")
                self.host.log.write("abandoned", text=act[1])
        self._sync_settle()
        self.publish()

    def _sync_settle(self) -> None:
        """Arm the closer's silence window while the machine is CLOSING
        (a silent final re-arms it); drop it once the machine is not."""
        closing = self.machine.state == TurnMachine.CLOSING
        if closing and self._settle is None:
            self._arm_settle(self.settle_s)
        elif not closing:
            self._cancel_settle()

    def _arm_settle(self, seconds: float) -> None:
        self._cancel_settle()
        self._settle = asyncio.ensure_future(self._settle_after(seconds))

    def _cancel_settle(self) -> None:
        if self._settle is not None:
            self._settle.cancel()
            self._settle = None

    async def _settle_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._settle = None
        self.handle_actions(self.machine.settle())

    @property
    def speech_hold_s(self) -> float:
        """How long a speech onset (no words yet) holds a pending closer:
        long enough for the words to arrive, short enough that a cough
        after the closer cannot hang the turn."""
        return max(self.settle_s, 1.5)

    def on_stt_event(self, ev) -> None:
        closing = self.machine.state == TurnMachine.CLOSING
        if isinstance(ev, Partial):
            if ev.text and closing:
                self._cancel_settle()  # more words: the next final decides
            self._partial = ev.text or ""
            self.publish()
        elif isinstance(ev, SpeechStarted):
            if closing:
                self._arm_settle(self.speech_hold_s)
        elif isinstance(ev, (Final, TurnEnd)):
            self._partial = ""  # the final settles what the partials guessed
            text = (ev.text or "").strip()
            if text:
                self.host.log.write("heard", text=text)
                self.handle_actions(self.machine.feed(text))
            elif closing:
                # silence ended a segment with no words: whatever held the
                # closer was noise, so the normal window runs from here
                self._arm_settle(self.settle_s)
            self.publish()

    # -- local commands ------------------------------------------------------------

    def command(self, name: str, arg, *, cued_abandoned: bool = False) -> None:
        """Every command is acknowledged by ear the moment it is heard:
        stop and resume by their own pair, cancel by the falling tone a
        discarded turn gets (the same meaning; once, even when the turn
        it discarded already played it), everything else by the short
        "got it" tick. Quit adds the closing triad as the process ends."""
        host = self.host
        host.log.write("command", name=name, arg=arg)
        if name in ("stop", "resume"):
            self.cue(name)
        elif name == "cancel":
            if not cued_abandoned:
                self.cue("abandoned")
        else:
            self.cue("command")
        if name not in ("stop", "resume"):
            host.spoken.user_input()  # any other command is new input: a stop ends
        if name == "stop":
            host.spoken.pause("user")
            self._out("  · stopped")
        elif name == "resume":
            host.spoken.resume("user")
            self._out("  · resumed")
        elif name == "again":
            if host.spoken.replay_answer() is None:
                host.say_local("Nothing to replay yet.")
        elif name == "status":
            host.command_status()
        elif name == "cancel":
            self._out("  · cancelled")
        elif name == "compact":
            host.command_compact()
        elif name == "quit":
            self._out("  · quit")
            asyncio.ensure_future(host.quit())

    # -- the ears --------------------------------------------------------------------

    async def ears(self) -> None:
        """The link check, then the mic feeding the gate; the loop only
        turns when a rebuild is requested (the watchdog, a deaf gate)."""
        loop = asyncio.get_running_loop()
        quitting = self.host.quitting
        backoff = 1.0
        while not quitting.is_set():
            try:
                await self.gate.probe()
                break
            except Exception as exc:
                self._out(f"[ears: speech-to-text link failed ({exc!r});"
                          f" retrying in {backoff:.0f}s]")
                self._link(False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        self._link(True)
        mic_failures = 0
        while not quitting.is_set():
            self.pass_start = self._clock()
            try:
                self.mic.start(loop, self.gate.feed)
            except Exception as exc:
                mic_failures += 1
                self._out(f"[ears: mic failed ({exc!r}); retrying]")
                self.host.log.write("mic", state="failed", error=repr(exc)[:200])
                self.mic.stop()
                if mic_failures >= MIC_CONSTRUCT_MAX_FAILURES:
                    raise
                await asyncio.sleep(1.0)
                continue
            mic_failures = 0
            self.host.log.write("mic", state="started",
                                device=getattr(self.mic, "device_name", None),
                                device_rate=getattr(self.mic, "device_rate", None),
                                rate=self.mic.rate)
            self._out(f"[mic: {getattr(self.mic, 'device_name', None) or 'default'}"
                      f" at {getattr(self.mic, 'device_rate', None) or self.mic.rate} Hz]")
            self._rebuild.clear()
            await self._rebuild.wait()
            self.mic.stop()
            self.host.log.write("mic", state="rebuilding", reason=self._rebuild_reason)
            await asyncio.sleep(0.2)

    async def watchdog(self) -> None:
        down, down_since = False, None
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            down, down_since, event, starved_now = watchdog_tick(
                self.mic.last_frame_t, self.pass_start, down, down_since, self._clock(),
                last_live_t=getattr(self.mic, "last_live_t", None))
            if event == "down":
                self._out("[ears: no mic frames; rebuilding the microphone]")
                self.host.log.write("watchdog", event="mic_down")
                self._mic_trouble = "mic lost, rebuilding"
            elif event == "silent":
                self._out("[ears: the microphone delivers only silence; rebuilding it]")
                self.host.log.write("watchdog", event="mic_silent")
                self._mic_trouble = "mic silent, rebuilding"
            elif event == "up":
                self._out("[ears: mic frames back]")
                self.host.log.write("watchdog", event="mic_up")
                self._mic_trouble = None
            self.publish()
            if starved_now:
                self.request_rebuild("no mic frames" if event != "silent" else "silent frames")

    async def abandon_ticker(self) -> None:
        while True:
            await asyncio.sleep(ABANDON_INTERVAL_S)
            self.handle_actions(self.machine.abandon_stale())

    # -- the run ---------------------------------------------------------------------

    async def run(self) -> None:
        host = self.host
        host.start()
        self._out(f"[hands-free-voice in {host.project_dir}]")
        self._out(f"[say '{self.address} ...' to open a turn and end it with"
                  f" '{self.closer}'; '{self.address} stop / resume / again / never mind /"
                  " status / cancel / compact / quit' are local; unaddressed speech"
                  " is ignored]")
        self.publish()
        self._tasks = [
            asyncio.ensure_future(self.ears()),
            asyncio.ensure_future(self.watchdog()),
            asyncio.ensure_future(self.abandon_ticker()),
            asyncio.ensure_future(host.still_here_ticker(float(host.cfg["seat"]["still_here_s"]))),
        ]
        ears = self._tasks[0]

        def ears_dead(task):
            if task.cancelled() or task.exception() is None:
                return
            self._out(f"[ears failed: {task.exception()!r}; shutting down]")
            host.quitting.set()

        ears.add_done_callback(ears_dead)
        try:
            await host.quitting.wait()
        finally:
            self._cancel_settle()
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self.mic.stop()
            await self.gate.stop()
            await host.shutdown()
            self.cue("closing")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.playback.wait_played(), 2.0)
            self.playback.stop()
            self._out("[session closed]")


async def run_voice(host: Host, seat, cfg: dict, *, stt, tts,
                    playback: Playback | None = None, mic: Mic | None = None,
                    vad=None, on_state=None) -> None:
    """The providers arrive built (providers/registry.py): nothing in the
    voice loop knows which vendor is listening or speaking. on_state, when
    given, hears every change of VoiceFront.state()."""
    if playback is None:
        playback = Playback(enabled=True, device=cfg["audio"].get("output_device"),
                            rate=tts.sample_rate)
        if not playback.enabled:
            print("hands-free-voice: no audio output device; run with --text", file=sys.stderr)
            return
    if mic is None:
        mic = Mic(rate=MIC_RATE, device=cfg["audio"].get("input_device"))
    scrubber = Scrubber(cfg["words"]["address"], cfg["words"]["closer"])
    respell = Respeller(cfg.get("respell") or {})
    speak = make_speaker(tts, playback, scrubber, respell, cfg["volumes"],
                         cfg["tts"].get("voice") or None)
    host.bind(seat, SpokenLog(speak))
    front = VoiceFront(host, cfg, stt=stt, tts=tts, playback=playback, mic=mic, vad=vad,
                       on_state=on_state)
    await front.run()
