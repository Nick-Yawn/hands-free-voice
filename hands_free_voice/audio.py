"""Audio devices through sounddevice: the speaker and the microphone.

Playback: play() only enqueues; a dedicated daemon thread does the
blocking stream.write() at real-time pace, off the event loop (an
inline write once stalled the loop long enough to kill two unrelated
websockets at once). abort() drops everything queued and restarts the
stream. A marker enqueued behind audio resolves a future when the drain
thread reaches it, which is how the player awaits "played out".

Mic: a RawInputStream opened at the DEVICE's own rate (queried at every
start, because a Bluetooth headset renegotiates its profile and rate
when playback starts or stops) and resampled here to the rate the
listener was opened with. The callback marks two clocks: the last frame
of any kind and the last LIVE frame (not digital silence), because a
dead Bluetooth mic keeps delivering zeros instead of stopping. The
watchdog (watchdog_tick, pure) rebuilds the device on either symptom
without any help from the speech vendor.
"""

import asyncio
import queue
import sys
import threading
import time
from array import array

OUT_RATE = 24_000
MIC_RATE = 16_000
MIC_CHUNK_MS = 40

MIC_STARVED_S = 5.0   # no frames at all
MIC_SILENT_S = 8.0    # frames, but every sample zero

_UNTRACKED = object()  # watchdog_tick: no separate live-frame clock


def scale_pcm(pcm: bytes, gain: float) -> bytes:
    """Amplitude-scale a frame-aligned PCM16 mono buffer, clamped. An
    odd-length buffer passes through untouched rather than raising."""
    if gain == 1.0 or len(pcm) % 2:
        return pcm
    samples = array("h", pcm)
    return array("h", (max(-32768, min(32767, int(v * gain)))
                       for v in samples)).tobytes()


class Playback:
    _STOP = object()

    def __init__(self, enabled: bool = True, device=None, rate: int = OUT_RATE,
                 open_stream=None):
        self.rate = rate
        self.device = device
        self._open = open_stream or self._raw_output
        self.stream = None
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        self._thread: threading.Thread | None = None
        self._markers: list = []  # (loop, future) not yet reached
        if not enabled:
            return
        try:
            self.stream = self._open()
            self.stream.start()
        except Exception as exc:
            print(f"[no audio output: {exc}]", file=sys.stderr)
            return
        self._start_thread()

    def _raw_output(self):
        import sounddevice as sd
        return sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16",
                                  device=self.device)

    @property
    def enabled(self) -> bool:
        return self._thread is not None

    def _start_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._drain, daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is self._STOP:
                return
            if isinstance(item, tuple):  # a marker: (loop, future)
                self._resolve(item)
                continue
            with self._lock:
                stream, gen = self.stream, self._gen
            if stream is None:
                continue
            try:
                stream.write(item)
            except Exception as exc:
                with self._lock:
                    caused_by_abort = self._gen != gen
                if not caused_by_abort:
                    print(f"[playback write failed: {exc}]", file=sys.stderr)

    @staticmethod
    def _resolve(marker) -> None:
        loop, fut = marker
        try:
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))
        except RuntimeError:
            pass  # the loop is closed

    def play(self, pcm: bytes, gain: float = 1.0) -> None:
        if self._thread is None or not pcm:
            return
        self._q.put(scale_pcm(pcm, gain))

    def marker(self) -> "asyncio.Future":
        """A future resolved when everything queued before it has been
        written to the device (resolved at once with no device)."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        if self._thread is None:
            fut.set_result(None)
            return fut
        self._q.put((loop, fut))
        return fut

    async def wait_played(self) -> None:
        await self.marker()

    def _drop_queued(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                if isinstance(item, tuple):
                    self._resolve(item)
        except queue.Empty:
            pass

    def abort(self) -> None:
        """Drop everything queued and cut the audio in flight."""
        with self._lock:
            self._gen += 1
            stream = self.stream
        self._drop_queued()
        if stream is None:
            return
        try:
            stream.abort()
        except Exception as exc:
            print(f"[playback abort failed: {exc}]", file=sys.stderr)
        try:
            stream.start()
        except Exception as exc:
            print(f"[playback restart failed: {exc}; muting]", file=sys.stderr)
            with self._lock:
                if self.stream is stream:
                    self.stream = None

    def stop(self) -> None:
        thread = self._thread
        if thread is not None:
            with self._lock:
                self._gen += 1
            self._drop_queued()
            self._q.put(self._STOP)
            if self.stream is not None:
                try:
                    self.stream.abort()
                except Exception:
                    pass
            thread.join(timeout=2.0)
            self._thread = None
        stream, self.stream = self.stream, None
        if stream is not None:
            for op in (stream.stop, stream.close):
                try:
                    op()
                except Exception:
                    pass


def is_digital_silence(pcm: bytes) -> bool:
    return not pcm.strip(b"\x00")


class Resampler:
    """Linear-interpolation resampling of PCM16 mono, stateful across
    chunks (the seam between two chunks is interpolated, not dropped).
    Pure Python: at 48 kHz in, about a percent of a core."""

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._step = self.src_rate / self.dst_rate
        self._pos = 1.0     # position in [prev] + chunk index space; 1 = the first sample
        self._prev = 0

    def __call__(self, pcm: bytes) -> bytes:
        if self.src_rate == self.dst_rate:
            return pcm
        src = array("h", pcm[: len(pcm) - (len(pcm) % 2)])
        if not src:
            return b""
        ext = array("h", [self._prev])
        ext.extend(src)
        out = array("h")
        pos, step, last = self._pos, self._step, len(ext) - 1
        while pos < last:
            i = int(pos)
            frac = pos - i
            a, b = ext[i], ext[i + 1]
            out.append(int(a + (b - a) * frac))
            pos += step
        self._pos = pos - last
        self._prev = src[-1]
        return out.tobytes()


class Mic:
    """The input device: frames reach on_frame(bytes) on the loop thread
    as PCM16 mono at `rate`, whatever rate the device runs at.
    last_frame_t and last_live_t are written only by a real callback."""

    def __init__(self, rate: int = MIC_RATE, chunk_ms: int = MIC_CHUNK_MS, device=None,
                 open_stream=None, query_device=None):
        self.rate = rate
        self.chunk_ms = chunk_ms
        self.device = device
        self._open = open_stream or self._raw_input
        self._query = query_device or self._query_input
        self.stream = None
        self.device_name: str | None = None
        self.device_rate: int | None = None
        self.last_frame_t: float | None = None
        self.last_live_t: float | None = None

    def _query_input(self) -> tuple[str, int]:
        import sounddevice as sd
        info = sd.query_devices(self.device, "input") if self.device is not None \
            else sd.query_devices(kind="input")
        return str(info["name"]), int(info["default_samplerate"] or self.rate)

    def _raw_input(self, callback, rate: int, blocksize: int):
        import sounddevice as sd
        return sd.RawInputStream(samplerate=rate, channels=1, dtype="int16",
                                 blocksize=blocksize, device=self.device,
                                 callback=callback)

    def start(self, loop, on_frame) -> None:
        """Query the device afresh, open at its rate, resample to ours."""
        self.device_name, self.device_rate = self._query()
        resample = Resampler(self.device_rate, self.rate)

        def callback(indata, frames, t, status):
            now = time.monotonic()
            self.last_frame_t = now
            pcm = bytes(indata)
            if not is_digital_silence(pcm):
                self.last_live_t = now
            loop.call_soon_threadsafe(on_frame, resample(pcm))

        self.stream = self._open(callback, self.device_rate,
                                 self.device_rate * self.chunk_ms // 1000)
        self.stream.start()

    def stop(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            for op in (stream.stop, stream.close):
                try:
                    op()
                except Exception:
                    pass


def starved(last_frame_t: float | None, now: float, threshold: float = MIC_STARVED_S) -> bool:
    return last_frame_t is not None and (now - last_frame_t) > threshold


def watchdog_tick(last_frame_t: float | None, pass_start: float, down: bool,
                  down_since: float | None, now: float,
                  threshold: float = MIC_STARVED_S, last_live_t=_UNTRACKED,
                  silent_threshold: float = MIC_SILENT_S,
                  ) -> tuple[bool, float | None, str | None, bool]:
    """One tick of the mic watchdog, pure. Returns (down, down_since,
    event, starved_now): event is "down" (no frames) or "silent" (frames
    of pure zeros) on the falling edge, "up" once a LIVE frame newer
    than the moment we went down has arrived, else None; starved_now is
    the rebuild trigger. The reference clocks are the newer of the last
    frame (or last live frame) and the pass start, so a fresh pass gets
    its grace window without ever faking a frame. Without a separate
    last_live_t every frame counts as live; None means none yet."""
    live = last_frame_t if last_live_t is _UNTRACKED else last_live_t
    ref_frames = pass_start if last_frame_t is None or pass_start > last_frame_t \
        else last_frame_t
    ref_live = pass_start if live is None or pass_start > live else live
    no_frames = starved(ref_frames, now, threshold)
    silent = not no_frames and starved(ref_live, now, silent_threshold)
    if no_frames or silent:
        if not down:
            return True, now, "down" if no_frames else "silent", True
        return True, down_since, None, True
    if down:
        if live is not None and down_since is not None and live > down_since:
            return False, None, "up", False
        return True, down_since, None, False
    return False, None, None, False
