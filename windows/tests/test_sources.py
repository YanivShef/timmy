"""Capture threads against fake COM objects: mic follows the default device, Chrome watcher attaches/re-attaches."""
import threading
import time

import numpy as np

from tests.fakecom import FakeAudioClient, FakeDevice, FakeEnumerator, LiveCaptureClient
from timmy.audio import loopback as LB
from timmy.audio import mic as MIC
from timmy.audio import wasapi as W
from timmy.audio.padder import SR, Timeline, clock
from timmy.audio.resample import Format


class Events(list):
    def __call__(self, obj):
        self.append(obj)

    def of(self, event):
        return [e for e in self if e.get("event") == event]


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def live_device(dev_id, name, fmt, value, accept_target=False):
    caps = []

    def factory():
        cap = LiveCaptureClient(fmt, value, clock)
        caps.append(cap)
        accept = {fmt} | ({Format(16000, 1, 32, True)} if accept_target else set())
        return FakeAudioClient(accept=accept, mix=fmt, capture=cap)
    d = FakeDevice(dev_id, name, factory)
    d.caps = caps
    return d


def test_mic_follows_default_device_and_pads_the_switch():
    a = live_device("dev-a", "Laptop mic", Format(48000, 2, 32, True), 0.3)
    b = live_device("dev-b", "Headset", Format(16000, 1, 32, True), -0.3, accept_target=True)
    fe = FakeEnumerator([a, b], default="dev-a")
    tl, ev, stop = Timeline(), Events(), threading.Event()
    t0 = clock()
    m = MIC.MicSource(tl, t0, ev, stop, poll=0.1, enum_factory=fe.as_enumerator)
    m.start()
    try:
        assert wait_for(lambda: ev.of("mic") and ev.of("mic")[0]["state"] == "open")
        time.sleep(0.8)
        switch_at = clock() - t0
        fe.default = "dev-b"                                        # the user picks the headset
        assert wait_for(lambda: any(e["state"] == "switched" for e in ev.of("mic")))
        time.sleep(0.8)
        b.caps[-1].dead = True                                      # headset unplugged
        fe.default = None
        assert wait_for(lambda: any(e["state"] == "lost" for e in ev.of("mic")))
        assert wait_for(lambda: any(e["state"] == "none" for e in ev.of("mic")))
    finally:
        stop.set()
        m.join(3)
    assert not m.is_alive()
    states = [e["state"] for e in ev.of("mic")]
    assert states[:2] == ["open", "switched"] and "lost" in states and states[-1] == "none"
    opened = ev.of("mic")[0]
    assert opened["name"] == "Laptop mic" and opened["format"] == {"rate": 48000, "channels": 2, "bits": 32, "float": True}
    assert ev.of("mic")[1]["format"]["rate"] == 16000
    out = tl.take(tl.end)
    k = int(switch_at * SR)
    before = out[int(0.2 * SR): k - int(0.1 * SR)]
    assert np.mean(np.abs(before - 0.3) < 0.01) > 0.95, "48 kHz stereo device resampled into the timeline"
    after = out[k + int(0.35 * SR): k + int(0.7 * SR)]
    assert np.mean(np.abs(after + 0.3) < 0.01) > 0.95, "the new device lands right after the switch"
    assert not np.any((out > 0.1) & (np.arange(len(out)) > k + int(0.3 * SR))), "no old-device audio after the switch"


def test_mic_denied_and_no_device():
    d = FakeDevice("dev-x", "Mic", FakeAudioClient, activate_hr=W.E_ACCESSDENIED)
    fe = FakeEnumerator([d], default="dev-x")
    ev, stop = Events(), threading.Event()
    m = MIC.MicSource(Timeline(), clock(), ev, stop, enum_factory=fe.as_enumerator)
    m.start()
    assert wait_for(lambda: ev.of("mic"))
    stop.set()
    m.join(3)
    assert ev.of("mic")[0]["state"] == "denied" and ev.of("mic")[0]["hr"] == "0x80070005"


def test_loopback_source_with_fake_activation():
    fmt = Format(48000, 2, 16, False)
    cap = LiveCaptureClient(fmt, 0.25, clock)

    def opener(pid):
        return W.CaptureStream.open(lambda: W.ComPtr(FakeAudioClient(accept={fmt}, capture=cap).com.addr),
                                    W.LOOPBACK_FORMATS, loopback=True)
    tl, ev = Timeline(), Events()
    src = LB.LoopbackSource(1234, tl, clock(), ev, opener=opener)
    src.start()
    assert wait_for(lambda: src.state == "running")
    time.sleep(0.5)
    src.stop()
    src.join(3)
    assert src.state == "ended" and not src.unsupported
    e = ev.of("chrome_stream")[0]
    assert e["state"] == "open" and e["pid"] == 1234 and e["format"]["rate"] == 48000
    assert [t[1] for t in e["tried"]] == ["0x88890008", "0x00000000"]
    out = tl.take(tl.end)
    nz = np.flatnonzero(np.abs(out) > 0.1)
    body = out[nz[0] + 400: nz[-1] - 400]                      # skip the resampler's edges
    assert len(body) > 0.3 * SR and np.mean(np.abs(body - 0.25) < 0.01) > 0.95


def test_loopback_source_unsupported_windows():
    def opener(pid):
        err = W.ComError("ActivateAudioInterfaceAsync", W.E_NOTIMPL)
        err.stage = "activate"
        raise err
    ev = Events()
    src = LB.LoopbackSource(1, Timeline(), clock(), ev, opener=opener)
    src.run()
    assert src.state == "failed" and src.unsupported
    assert ev.of("chrome_stream")[0]["error"] == "E_NOTIMPL"


class FakeSource:
    """Stands in for LoopbackSource in watcher tests."""

    made = []

    def __init__(self, pid, timeline, t0, status):
        self.pid, self.state, self.alive, self.stopped = pid, "starting", False, False
        self.hr, self.stage = None, None
        FakeSource.made.append(self)

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stopped, self.alive, self.state = True, False, "ended"

    def join(self, timeout=None):
        pass

    @property
    def unsupported(self):
        return self.state == "failed" and self.hr in LB.UNSUPPORTED

    def fail(self, hr):
        self.state, self.alive, self.hr, self.stage = "failed", False, hr, "activate"


def test_chrome_watcher_states_restart_and_retry(monkeypatch):
    FakeSource.made = []
    roots = []
    ev = Events()
    w = LB.ChromeWatcher(Timeline(), clock(), ev, threading.Event(), roots_fn=lambda: roots, source_factory=FakeSource)
    w.scan()
    assert ev.of("chrome")[-1]["state"] == "closed"                  # Chrome not running: recording goes on with the mic
    roots[:] = [100, 200]
    w.scan()
    assert [s.pid for s in FakeSource.made] == [100, 200]
    FakeSource.made[0].state = "running"
    w.scan()
    assert ev.of("chrome")[-1] == {"event": "chrome", "state": "attached", "pids": [100]}
    FakeSource.made[1].state = "running"
    w.scan()
    assert ev.of("chrome")[-1]["pids"] == [100, 200]
    n = len(ev)
    w.scan()
    assert len(ev) == n                                              # nothing changed: nothing reported
    roots[:] = [300]                                                 # Chrome restarted: new browser process
    w.scan()
    assert FakeSource.made[0].stopped and FakeSource.made[1].stopped and FakeSource.made[2].pid == 300
    FakeSource.made[2].fail(W.AUDCLNT_E_DEVICE_INVALIDATED)          # a transient failure: retried later
    monkeypatch.setattr(LB, "RETRY_AFTER", 0.0)
    w.scan()
    assert ev.of("chrome")[-1]["state"] == "error"
    assert len(FakeSource.made) == 4 and FakeSource.made[3].pid == 300
    FakeSource.made[3].fail(W.E_NOTIMPL)                             # Windows without process loopback
    w.scan()
    assert ev.of("chrome")[-1]["state"] == "unsupported" and ev.of("chrome")[-1]["error"] == "E_NOTIMPL"
    w.scan()
    assert len(FakeSource.made) == 4                                 # no more attempts


def test_chrome_watcher_thread_stops_its_sources():
    FakeSource.made = []
    stop = threading.Event()
    w = LB.ChromeWatcher(Timeline(), clock(), Events(), stop, roots_fn=lambda: [7], source_factory=FakeSource, interval=0.05)
    w.start()
    assert wait_for(lambda: FakeSource.made)
    stop.set()
    w.join(2)
    assert not w.is_alive() and FakeSource.made[0].stopped
