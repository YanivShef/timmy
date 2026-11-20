"""The owner's microphone: WASAPI shared capture of the default input, following the default (DESIGN.md 5.2).

The Mac app follows the system default input mid-call (headset plugged in, AirPods connected) and pads the gap with
silence. Same here: every 0.5 s the default capture endpoint id is compared; when it changed, or the stream reports
AUDCLNT_E_DEVICE_INVALIDATED (unplugged), the stream is closed and the new default is opened. The Feed re-anchors
the new stream by its timestamps, so the timeline (padder.py) fills the gap with silence.

Status events: {"event": "mic", "state": "open" | "switched" | "lost" | "none" | "error" | "denied", ...}
"""
import threading

from timmy.audio import wasapi as W
from timmy.audio.padder import Feed, clock
from timmy.audio.resample import Converter

POLL = 0.5
RETRY = 1.0
RETRY_DENIED = 3.0


def open_mic(enum: "W.DeviceEnumerator", dev_id: str):
    """(CaptureStream, friendly name, tried formats) for endpoint `dev_id`.

    Formats: 16 kHz mono float first (the engine converts, no resampling here), then the device's own mix format
    (always accepted in shared mode), then common fallbacks.
    """
    dev = enum.device(dev_id)
    try:
        name = W.friendly_name(dev)
        with W.activate_audio_client(dev) as probe:          # E_ACCESSDENIED here = microphone privacy switch is off
            mix = W.get_mix_format(probe)
        formats = [W.MIC_FORMATS[0]] + ([mix] if mix else []) + [f for f in W.MIC_FORMATS[1:] if f != mix]
        stream, tried = W.CaptureStream.open(lambda: W.activate_audio_client(dev), formats, loopback=False)
        return stream, name, tried
    finally:
        dev.release()


class MicSource(threading.Thread):
    def __init__(self, timeline, t0, status, stop, role=W.ROLE_CONSOLE, poll=POLL, enum_factory=None, opener=open_mic):
        super().__init__(name="mic", daemon=True)
        self.status, self.stop_event, self.role, self.poll = status, stop, role, poll
        self.enum_factory = enum_factory or W.DeviceEnumerator
        self.opener = opener
        self.feed = Feed(timeline, t0)
        self.device_id = None
        self.device_name = ""
        self.opened = 0              # how many streams were opened (1 = no switch yet)
        self._last = None

    def _say(self, state, **kw):
        key = (state, kw.get("id"), kw.get("hr"))
        if key != self._last:
            self._last = key
            self.status({"event": "mic", "state": state, **kw})

    def run(self):
        need_uninit = False
        enum = stream = None
        try:
            need_uninit = W.com_init()
            enum = self.enum_factory()
            retry_at = next_poll = 0.0
            while not self.stop_event.is_set():
                now = clock()
                if stream is None:
                    if now < retry_at:
                        self.stop_event.wait(0.1)
                        continue
                    try:
                        dev_id = enum.default_id(W.E_CAPTURE, self.role)
                    except OSError:
                        dev_id = None
                    if not dev_id:
                        self._say("none")
                        retry_at = now + RETRY
                        continue
                    try:
                        stream, name, tried = self.opener(enum, dev_id)
                        stream.start()
                    except W.ComError as e:
                        if stream is not None:
                            stream.close()
                            stream = None
                        denied = e.hr == W.E_ACCESSDENIED
                        self._say("denied" if denied else "error", id=dev_id, hr=W.hr_hex(e.hr), error=W.hr_name(e.hr))
                        retry_at = now + (RETRY_DENIED if denied else RETRY)
                        continue
                    self.feed.set_converter(Converter(stream.fmt))
                    self.opened += 1
                    state = "open" if self.opened == 1 else "switched"
                    self.device_id, self.device_name = dev_id, name
                    self._say(state, id=dev_id, name=name, format=stream.fmt.as_dict(), tried=tried)
                    next_poll = now + self.poll
                    continue
                stream.wait(100)
                try:
                    for pk in stream.drain():
                        self.feed.push(pk.t, pk.data, pk.frames, pk.silent)
                except W.ComError as e:
                    self._say("lost", id=self.device_id, hr=W.hr_hex(e.hr), error=W.hr_name(e.hr))
                    stream.close()
                    stream = None
                    self.feed.end_stream()
                    retry_at = clock() + 0.2
                    continue
                if clock() >= next_poll:
                    next_poll = clock() + self.poll
                    try:
                        new_id = enum.default_id(W.E_CAPTURE, self.role)
                    except OSError:
                        new_id = self.device_id
                    if new_id != self.device_id:        # the user picked another default input: follow it
                        stream.close()
                        stream = None
                        self.feed.end_stream()
        except Exception as e:
            self.status({"event": "mic", "state": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            if stream is not None:
                stream.close()
            try:
                self.feed.end_stream()
            except Exception:
                pass
            if enum is not None:
                enum.release()
            if need_uninit:
                W.com_uninit()


def list_inputs():
    """{"default": id or None, "devices": [{"id", "name"}]} (onboarding / --list). Call on a thread with COM."""
    need = W.com_init()
    try:
        enum = W.DeviceEnumerator()
        try:
            default = enum.default_id(W.E_CAPTURE, W.ROLE_CONSOLE)
            return {"default": default, "devices": [{"id": i, "name": n} for i, n in enum.endpoints(W.E_CAPTURE)]}
        finally:
            enum.release()
    finally:
        if need:
            W.com_uninit()
