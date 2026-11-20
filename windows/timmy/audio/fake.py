"""Fake capture sources for tests and demos: a WAV file played in real time through the same Feed / Timeline path as
a real WASAPI stream (any rate, 1 to 8 channels, 16/24/32-bit PCM), optionally with "device gone" gaps.

A gap (at, duration) behaves like an unplugged mic: no packets for `duration` seconds, and the audio of that time is
lost, exactly as nobody captured it. The timeline must then show silence there, and the audio after it must land at
its true time.
"""
import threading
import wave

from timmy.audio.padder import Feed, clock
from timmy.audio.resample import Converter, Format


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        fmt = Format(w.getframerate(), w.getnchannels(), w.getsampwidth() * 8, False)
        return fmt, w.readframes(w.getnframes())


def parse_gaps(specs):
    """["1.0:0.5", ...] -> [(1.0, 0.5), ...]"""
    out = []
    for s in specs or []:
        at, _, dur = str(s).partition(":")
        out.append((float(at), float(dur or 0)))
    return sorted(out)


class WavSource(threading.Thread):
    def __init__(self, path, timeline, t0, status, stop, name="fake", chunk_ms=10, gaps=(), start_delay=0.0):
        super().__init__(name=f"fake-{name}", daemon=True)
        self.path, self.status, self.stop_event, self.label = path, status, stop, name
        self.fmt, self.data = read_wav(path)
        self.feed = Feed(timeline, t0, Converter(self.fmt))
        self.chunk = max(1, int(self.fmt.rate * chunk_ms / 1000))
        self.gaps = list(gaps)
        self.start_delay = start_delay

    def run(self):
        fmt, data, ba = self.fmt, self.data, self.fmt.block_align
        total = len(data) // ba
        start = clock() + self.start_delay
        self.status({"event": "fake", "source": self.label, "state": "open", "format": fmt.as_dict(), "seconds": round(total / fmt.rate, 3)})
        pos = 0
        gaps = list(self.gaps)
        while pos < total and not self.stop_event.is_set():
            if gaps and pos / fmt.rate >= gaps[0][0]:
                at, dur = gaps.pop(0)
                self.feed.end_stream()                     # the "device" disappears
                self.status({"event": "fake", "source": self.label, "state": "gap", "at": at, "seconds": dur})
                pos = min(total, int((at + dur) * fmt.rate))
                continue
            n = min(self.chunk, total - pos)
            t_pkt = start + pos / fmt.rate
            wait = t_pkt + n / fmt.rate - clock()          # a packet is ready when its last frame was "recorded"
            if wait > 0 and self.stop_event.wait(wait):
                break
            self.feed.push(t_pkt, data[pos * ba:(pos + n) * ba], n)
            pos += n
        self.feed.end_stream()
        self.status({"event": "fake", "source": self.label, "state": "ended"})
