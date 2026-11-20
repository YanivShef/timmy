"""Keep both channels on one wall-clock timeline (DESIGN.md 5.3).

The worker (mac/live.py and its port) derives every timestamp from sample counts, and the echo detector compares
the mic and Chrome channels by position. So a channel must never fall behind wall-clock time when its source goes
quiet: Chrome's process loopback delivers *nothing* while Chrome plays nothing, a mic disappears for a moment when
the default device changes, Chrome can restart mid-call.

clock()       seconds on the QueryPerformanceCounter scale (the same scale as WASAPI's u64QPCPosition / 1e7)
SourceClock   where each packet of one source goes on the channel timeline: contiguous while the stream flows,
              jumps forward (zeros in between) after a real gap; never backwards, so a source never overlaps itself
Timeline      one channel: sources add samples at positions (summed when several Chrome processes play), the writer
              takes everything up to `now - delay`; what nobody delivered is silence. Real audio is never dropped:
              a packet that arrives after its slot was written is placed right after the written part instead.
Feed          Converter (resample.py) + SourceClock + Timeline for one capture stream
"""
import math
import sys
import threading
import time

import numpy as np

SR = 16000
GAP_TOLERANCE = 0.25      # s: timestamp jitter / clock drift tolerated before a gap is assumed
DELAY = 0.3               # s: jitter buffer of the writer (sources deliver late by up to this without padding)
MAX_AHEAD = 120.0         # s: sanity cap for a position far in the future (broken timestamps)

_qpc_freq = None


def clock() -> float:
    """Monotonic seconds. On Windows exactly QueryPerformanceCounter / frequency, so WASAPI QPC positions compare."""
    global _qpc_freq
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        if _qpc_freq is None:
            f = ctypes.c_int64()
            k.QueryPerformanceFrequency(ctypes.byref(f))
            _qpc_freq = f.value or 1
        c = ctypes.c_int64()
        k.QueryPerformanceCounter(ctypes.byref(c))
        return c.value / _qpc_freq
    return time.perf_counter()


def level(rms: float) -> float:
    """0..1 meter value, the same mapping as the Mac card (-50 dB -> 0, -5 dB -> 1)."""
    return min(1.0, max(0.0, (20 * math.log10(max(rms, 1e-6)) + 50) / 45))


class Timeline:
    def __init__(self, sr: int = SR):
        self.sr = sr
        self.written = 0                       # samples already handed to the writer
        self._acc = np.zeros(0, np.float32)    # samples from `written` on (zeros where nothing was added)
        self._end = 0                          # one past the last placed sample
        self._lock = threading.Lock()
        self.late_samples = 0                  # diagnostics: samples placed later than their timestamp
        self.added_samples = 0

    def add(self, pos: int, samples: np.ndarray) -> int:
        """Mix `samples` in at sample position `pos`. Returns the position actually used (>= written)."""
        a = np.asarray(samples, np.float32).reshape(-1)
        with self._lock:
            pos = int(pos)
            if pos < self.written:
                self.late_samples += self.written - pos
                pos = self.written
            off = pos - self.written
            if off > MAX_AHEAD * self.sr:
                off = len(self._acc)              # absurd timestamp: append instead of leaving minutes of silence
                pos = self.written + off
            end = off + len(a)
            if end > len(self._acc):
                grown = np.zeros(max(end, 2 * len(self._acc)), np.float32)
                grown[: len(self._acc)] = self._acc
                self._acc = grown
            self._acc[off:end] += a
            self._end = max(self._end, pos + len(a))
            self.added_samples += len(a)
            return pos

    @property
    def end(self) -> int:
        """One past the last sample any source has placed (>= written)."""
        with self._lock:
            return max(self.written, self._end)

    def take(self, upto: int):
        """Everything in [written, upto) as one float32 array (silence where nothing was added), or None."""
        with self._lock:
            n = int(upto) - self.written
            if n <= 0:
                return None
            out = np.zeros(n, np.float32)
            m = min(n, len(self._acc))
            out[:m] = self._acc[:m]
            self._acc = self._acc[m:].copy() if m < len(self._acc) else np.zeros(0, np.float32)
            self.written += n
        np.clip(out, -1.0, 1.0, out=out)
        return out


class SourceClock:
    """Maps the packets of one source to timeline positions (see module docstring)."""

    def __init__(self, t0: float, sr: int = SR, tolerance: float = GAP_TOLERANCE):
        self.t0, self.sr, self.tol = t0, sr, int(tolerance * sr)
        self.next = None          # position right after this source's last sample
        self.gaps = 0

    def reset(self):
        """The stream restarted (new device / new process): the next packet is placed by its timestamp."""
        self.next = None

    def place(self, t: float, n: int) -> int:
        ts = max(0, int(round((t - self.t0) * self.sr)))
        if self.next is None:
            pos = ts
        elif ts > self.next + self.tol:
            pos = ts                 # a real gap: the timeline fills it with silence
            self.gaps += 1
        else:
            pos = self.next          # flowing: stay contiguous, ignore jitter (and never step back over our own audio)
        self.next = pos + n
        return pos

    def placed(self, actual_pos: int, n: int):
        """The timeline moved the packet (late arrival): continue after where it really went."""
        self.next = actual_pos + n


class Feed:
    """converter -> clock -> timeline for one capture stream. push() is called from the stream's thread.

    Output samples are timed from the stream's anchor (its first packet) plus the samples produced so far, not from
    the packet that happened to flush them out of the resampler (soxr releases output in bursts, 30 to 40 ms late).
    A packet that comes later than the input so far explains (by more than the tolerance) means the stream paused
    (Chrome silent, device switch): the resampler tail is flushed and the stream re-anchors at that packet.
    """

    def __init__(self, timeline: Timeline, t0: float, converter=None, tolerance: float = GAP_TOLERANCE):
        self.timeline, self.clock = timeline, SourceClock(t0, timeline.sr, tolerance)
        self.converter = converter
        self.tol = tolerance
        self.samples = 0
        self.anchors = 0
        self._anchor = None
        self._in = 0          # input frames since the anchor
        self._out = 0         # output samples since the anchor

    @property
    def _rate(self):
        return self.converter.fmt.rate if self.converter is not None else self.timeline.sr

    def set_converter(self, converter):
        self.end_stream()
        self.converter = converter

    def push(self, t: float, data, frames: int, silent: bool = False):
        """One captured packet whose first frame was recorded at clock() time `t`."""
        if self._anchor is not None and t > self._anchor + self._in / self._rate + self.tol:
            self.end_stream()
        if self._anchor is None:
            self._anchor, self._in, self._out = t, 0, 0
            self.anchors += 1
        out = self.converter.process(data, frames, silent) if self.converter is not None else np.asarray(data, np.float32)
        self._in += frames
        self._place(out)

    def push_samples(self, t: float, out: np.ndarray):
        """Already 16 kHz mono samples (no converter)."""
        out = np.asarray(out, np.float32)
        self.push(t, out, len(out)) if self.converter is None else self._place_at(t, out)

    def _place(self, out):
        if not len(out):
            return
        self._place_at(self._anchor + self._out / self.timeline.sr, out)
        self._out += len(out)

    def _place_at(self, t, out):
        pos = self.clock.place(t, len(out))
        actual = self.timeline.add(pos, out)
        if actual != pos:
            self.clock.placed(actual, len(out))
        self.samples += len(out)

    def end_stream(self):
        """The stream stopped or paused: flush the resampler tail; the next packet re-anchors."""
        if self.converter is not None and self._anchor is not None:
            self._place(self.converter.flush())
            self.converter.reset()
        self._anchor = None
        self.clock.reset()
