"""The capture process: `python -m timmy.audio.capture` (DESIGN.md 2 and 5).

stdout   audio frames for the worker, engine.protocol format: channel 0 = Chrome (the other side), 1 = the mic
stderr   one JSON object per line: {"event": ..., "t": seconds since start, ...}
           start, ready, chrome (attached / closed / unsupported / error), chrome_stream, mic (open / switched /
           lost / none / denied / error), levels {"chrome": 0..1, "mic": 0..1} 5 times a second, stopped
stdin    a line "stop" (or EOF: the UI went away) stops the capture: everything captured is flushed, both channels
         end at the same length, then the process exits 0

Options (the UI uses none of the test ones):
  --seconds N          stop by itself after N seconds
  --no-chrome / --no-mic
  --pid N              capture this process tree instead of Chrome
  --process NAME       browser executable (default chrome.exe)
  --mic-test           microphone only (channel 1), for the onboarding level meter
  --list               print {"chrome_roots", "inputs", ...} as JSON on stdout and exit
  --fake-chrome WAV    channel 0 from a WAV file in real time instead of WASAPI (also --fake-wav)
  --fake-mic WAV       channel 1 from a WAV file; --fake-mic-gap AT:DUR simulates a device switch / unplug
  --ignore-stdin       do not stop on stdin EOF (manual runs from a console)
"""
import argparse
import json
import os
import sys
import threading

import numpy as np

from timmy.audio import padder
from timmy.audio.padder import SR, Timeline, clock
from timmy.engine import protocol as P

MAX_FRAME = SR                 # samples per frame written (a pump normally writes about 1600)
PUMP = 0.1                     # s between writes
LEVELS_EVERY = 0.2


class Status:
    def __init__(self, stream, t0):
        self.stream, self.t0 = stream, t0
        self.lock = threading.Lock()

    def __call__(self, obj):
        obj = {"event": obj.get("event"), "t": round(clock() - self.t0, 3), **{k: v for k, v in obj.items() if k != "event"}}
        line = json.dumps(obj, ensure_ascii=False)
        with self.lock:
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass


class Writer:
    """Takes due samples from each channel's timeline and writes frames. Returns False once stdout is closed."""

    def __init__(self, out, timelines):
        self.out, self.timelines = out, timelines
        self.peak = {ch: 0.0 for ch in timelines}
        self.ok = True

    def pump(self, upto: int) -> bool:
        if not self.ok:
            return False
        for ch, tl in self.timelines.items():
            a = tl.take(upto)
            if a is None:
                continue
            if len(a):
                self.peak[ch] = max(self.peak[ch], float(np.sqrt(np.mean(a * a))))
            try:
                for i in range(0, len(a), MAX_FRAME):
                    self.out.write(P.encode_frame(ch, a[i:i + MAX_FRAME]))
                self.out.flush()
            except (OSError, ValueError):
                self.ok = False
                return False
        return True

    def levels(self):
        lv = {ch: round(padder.level(v), 3) for ch, v in self.peak.items()}
        for ch in self.peak:
            self.peak[ch] = 0.0
        return lv


def _parse(argv):
    ap = argparse.ArgumentParser(prog="python -m timmy.audio.capture", description="Timmy capture: Chrome audio + mic -> frames on stdout")
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--no-chrome", action="store_true")
    ap.add_argument("--no-mic", action="store_true")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--process", default="chrome.exe")
    ap.add_argument("--mic-test", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fake-chrome", "--fake-wav", dest="fake_chrome", default="")
    ap.add_argument("--fake-mic", default="")
    ap.add_argument("--fake-chrome-gap", action="append", default=[])
    ap.add_argument("--fake-mic-gap", action="append", default=[])
    ap.add_argument("--ignore-stdin", action="store_true")
    ap.add_argument("--delay", type=float, default=padder.DELAY, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def list_report(exe="chrome.exe"):
    from timmy.audio import procs
    from timmy.winsys import winver
    rep = {"platform": sys.platform, "windows_build": winver.current_build(), "loopback_supported": winver.loopback_supported(),
           "process": exe, "chrome_roots": procs.chrome_roots(exe), "inputs": None}
    if sys.platform == "win32":
        try:
            from timmy.audio.mic import list_inputs
            rep["inputs"] = list_inputs()
        except OSError as e:
            rep["inputs_error"] = str(e)
    return rep


def _stdin_watch(stop):
    try:
        for line in sys.stdin.buffer:
            if line.strip().lower() in (b"stop", b"q", b"quit"):
                break
    except (OSError, ValueError):
        pass
    stop.set()


def main(argv=None) -> int:
    a = _parse(sys.argv[1:] if argv is None else argv)
    if a.list:
        print(json.dumps(list_report(a.process), ensure_ascii=False))
        return 0

    # import everything the sources need before the clock starts, so a slow first import is not recorded as a delay
    from timmy.audio.fake import WavSource, parse_gaps
    if sys.platform == "win32":
        from timmy.audio.loopback import ChromeWatcher
        from timmy.audio.mic import MicSource
    try:
        import soxr  # noqa: F401  (resampler for 44.1 / 48 kHz devices)
    except ImportError:
        pass

    t0 = clock()
    status = Status(sys.stderr, t0)
    stop = threading.Event()
    want_chrome = not (a.no_chrome or a.mic_test)
    want_mic = not a.no_mic
    timelines = {}
    if want_chrome:
        timelines[P.CH_CHROME] = Timeline()
    if want_mic:
        timelines[P.CH_MIC] = Timeline()
    status({"event": "start", "channels": sorted(timelines), "platform": sys.platform, "delay": a.delay})

    threads = []
    if want_chrome:
        if a.fake_chrome:
            threads.append(WavSource(a.fake_chrome, timelines[0], t0, status, stop, "chrome", gaps=parse_gaps(a.fake_chrome_gap)))
        elif sys.platform == "win32":
            threads.append(ChromeWatcher(timelines[0], t0, status, stop, exe=a.process, pid=a.pid or None))
        else:
            status({"event": "chrome", "state": "unsupported", "error": "not Windows"})
    if want_mic:
        if a.fake_mic:
            threads.append(WavSource(a.fake_mic, timelines[1], t0, status, stop, "mic", gaps=parse_gaps(a.fake_mic_gap)))
        elif sys.platform == "win32":
            threads.append(MicSource(timelines[1], t0, status, stop))
        else:
            status({"event": "mic", "state": "none", "error": "not Windows"})
    for th in threads:
        th.start()
    if not a.ignore_stdin:
        threading.Thread(target=_stdin_watch, args=(stop,), name="stdin", daemon=True).start()

    out = sys.stdout.buffer
    writer = Writer(out, timelines)
    status({"event": "ready"})
    next_levels = clock() + LEVELS_EVERY
    reason = "stop"
    while not stop.wait(PUMP):
        now = clock()
        if not writer.pump(int((now - t0 - a.delay) * SR)):
            reason = "stdout closed"
            break
        if now >= next_levels:
            next_levels = now + LEVELS_EVERY
            lv = writer.levels()
            status({"event": "levels", "chrome": lv.get(0), "mic": lv.get(1)})
        if a.seconds and now - t0 >= a.seconds:
            reason = "seconds"
            break
    stop.set()
    for th in threads:
        th.join(2.0)
    # flush: everything any source placed, and both channels end at the same sample
    end = max([int((clock() - t0) * SR)] + [tl.end for tl in timelines.values()])
    if a.seconds:
        end = max(int(a.seconds * SR), max([tl.end for tl in timelines.values()] or [0]))
    writer.pump(end)
    try:
        out.flush()
    except OSError:
        pass
    if not writer.ok:
        # the reader went away: point stdout at the null device so the interpreter's final flush cannot fail (exit 120)
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, sys.stdout.fileno())
        except OSError:
            pass
    status({"event": "stopped", "reason": reason, "samples": {str(ch): tl.written for ch, tl in timelines.items()},
            "late_samples": {str(ch): tl.late_samples for ch, tl in timelines.items()}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
