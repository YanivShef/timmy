"""Chrome's audio only: WASAPI process loopback of every Chrome browser root (DESIGN.md 5.1).

LoopbackSource   one capture thread for one root PID (its whole process tree)
ChromeWatcher    rescans the process list every 2 s: attaches new roots (Chrome started or restarted mid-call),
                 stops sources whose process is gone, retries failures, and reports one state to the UI:
                 attached / closed (Chrome not running) / unsupported (Windows too old) / error
                 as {"event": "chrome", "state": ...}; each stream also reports {"event": "chrome_stream", ...}
"""
import threading

from timmy.audio import procs
from timmy.audio import wasapi as W
from timmy.audio.padder import Feed, clock
from timmy.audio.resample import Converter

# activation failures that mean "this Windows cannot do process loopback" (not "try again later")
UNSUPPORTED = {W.E_NOTIMPL, W.E_INVALIDARG, W.REGDB_E_CLASSNOTREG, W.E_NOINTERFACE, W.E_ILLEGAL_METHOD_CALL}
RETRY_AFTER = 5.0


def open_loopback(pid: int, include_tree: bool = True):
    """(CaptureStream, tried formats). Raises ComError; `.stage` on it says whether activation or Initialize failed."""
    def make():
        try:
            return W.activate_process_loopback(pid, include_tree)
        except W.ComError as e:
            e.stage = "activate"
            raise
    try:
        return W.CaptureStream.open(make, W.LOOPBACK_FORMATS, loopback=True)
    except W.ComError as e:
        if not hasattr(e, "stage"):
            e.stage = "initialize"
        raise


class LoopbackSource(threading.Thread):
    def __init__(self, pid, timeline, t0, status, opener=open_loopback):
        super().__init__(name=f"loopback-{pid}", daemon=True)
        self.pid, self.status, self.opener = pid, status, opener
        self.feed = Feed(timeline, t0)
        self.stop_event = threading.Event()
        self.state = "starting"      # starting -> running -> ended | failed
        self.hr = None
        self.stage = None
        self.started_at = clock()

    def stop(self):
        self.stop_event.set()

    def run(self):
        need_uninit = False
        stream = None
        try:
            need_uninit = W.com_init()
            stream, tried = self.opener(self.pid)
            stream.start()
            self.feed.set_converter(Converter(stream.fmt))
            self.state = "running"
            self.status({"event": "chrome_stream", "state": "open", "pid": self.pid, "format": stream.fmt.as_dict(), "tried": tried})
            while not self.stop_event.is_set():
                stream.wait(100)
                for pk in stream.drain():
                    self.feed.push(pk.t, pk.data, pk.frames, pk.silent)
            self.state = "ended"
        except W.ComError as e:
            self.state, self.hr, self.stage = "failed", e.hr, getattr(e, "stage", "capture")
            self.status({"event": "chrome_stream", "state": "error", "pid": self.pid, "stage": self.stage, "hr": W.hr_hex(e.hr),
                         "error": W.hr_name(e.hr)})
        except (OSError, AttributeError) as e:      # AttributeError: Mmdevapi without ActivateAudioInterfaceAsync
            self.state, self.stage = "failed", "activate"
            self.hr = W.E_NOTIMPL if isinstance(e, AttributeError) else getattr(e, "winerror", None) or W.E_FAIL
            self.status({"event": "chrome_stream", "state": "error", "pid": self.pid, "stage": self.stage, "error": str(e)})
        finally:
            try:
                self.feed.end_stream()
            except Exception:
                pass
            if stream is not None:
                stream.close()
            if need_uninit:
                W.com_uninit()

    @property
    def unsupported(self) -> bool:
        return self.state == "failed" and self.stage == "activate" and self.hr in UNSUPPORTED


class ChromeWatcher(threading.Thread):
    def __init__(self, timeline, t0, status, stop, exe=procs.CHROME_EXE, pid=None, interval=2.0,
                 roots_fn=None, source_factory=LoopbackSource):
        super().__init__(name="chrome-watcher", daemon=True)
        self.timeline, self.t0, self.status, self.stop_event = timeline, t0, status, stop
        self.exe, self.fixed_pid, self.interval = exe, pid, interval
        self.roots_fn = roots_fn or (lambda: procs.chrome_roots(self.exe))
        self.source_factory = source_factory
        self.sources = {}            # pid -> LoopbackSource
        self.retry_at = {}           # pid -> clock() time of the next attempt after a failure
        self.reported = None         # last state reported: attached / closed / unsupported
        self.unsupported = False

    def run(self):
        while True:
            try:
                self.scan()
            except Exception as e:                 # never let the watcher die mid-call
                self.status({"event": "chrome", "state": "error", "stage": "scan", "error": str(e)})
            if self.stop_event.wait(self.interval):
                break
        for s in self.sources.values():
            s.stop()
        for s in self.sources.values():
            s.join(1.5)

    def _report(self, state, **kw):
        key = (state, tuple(kw.get("pids") or ()))
        if key != self.reported:
            self.reported = key
            self.status({"event": "chrome", "state": state, **kw})

    def scan(self):
        roots = [self.fixed_pid] if self.fixed_pid else list(self.roots_fn())
        now = clock()
        for pid in [p for p in self.sources if p not in roots]:
            self.sources.pop(pid).stop()                  # the process is gone (Chrome closed or restarted)
            self.retry_at.pop(pid, None)
        if self.unsupported:
            return
        # 1. report the state as it is now
        for s in self.sources.values():
            if not s.is_alive() and s.unsupported:
                self.unsupported = True
                self._report("unsupported", hr=W.hr_hex(s.hr), error=W.hr_name(s.hr))
                return
        running = sorted(p for p, s in self.sources.items() if s.state == "running")
        if not roots:
            self._report("closed")
        elif running:
            self._report("attached", pids=running)
        elif all(p in self.sources and self.sources[p].state == "failed" for p in roots):
            self._report("error")
        # 2. start sources for new roots, retry failed ones after RETRY_AFTER
        for pid in roots:
            s = self.sources.get(pid)
            if s is not None and s.is_alive():
                continue
            if s is not None:                              # failed or ended while the process still runs
                if pid not in self.retry_at:
                    self.retry_at[pid] = now + RETRY_AFTER
                if now < self.retry_at[pid]:
                    continue
                self.retry_at.pop(pid, None)
            src = self.source_factory(pid, self.timeline, self.t0, self.status)
            self.sources[pid] = src
            src.start()
