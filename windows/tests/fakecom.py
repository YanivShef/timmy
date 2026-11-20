"""Fake WASAPI COM objects, built with the same PyComObject machinery Timmy uses for its activation handler.

They let every vtable call in timmy.audio.wasapi run for real (argument marshalling, out-pointers, 64-bit values,
callbacks) on any OS. Only the Windows behavior behind the calls is simulated.
"""
import ctypes
from ctypes import POINTER, c_int, c_int64, c_uint32, c_uint64, c_void_p, c_wchar_p

from timmy.audio import wasapi as W
from timmy.audio.resample import Format

H = W.HRESULT
ALIVE = []        # the fakes' vtables must outlive every raw address handed around (ComPtr holds only an address)


def com(iids, methods):
    o = W.PyComObject(iids, methods)
    ALIVE.append(o)
    return o


def task_wstr(text: str, keep: list) -> int:
    """A wide string the way COM hands one out. Timmy frees GetId strings with CoTaskMemFree and friendly names with
    PropVariantClear, which on Windows really free: a ctypes buffer there corrupted the heap (0xc0000374 on
    windows-latest), so on Windows the fake allocates with CoTaskMemAlloc like the real device does."""
    return task_copy(ctypes.create_unicode_buffer(text), keep)


def task_copy(buf, keep: list) -> int:
    """`buf` as COM task memory on Windows (the caller frees it with CoTaskMemFree), else the buffer itself."""
    if not W.IS_WIN:
        keep.append(buf)
        return ctypes.addressof(buf)
    ole = ctypes.WinDLL("ole32")
    ole.CoTaskMemAlloc.argtypes, ole.CoTaskMemAlloc.restype = [ctypes.c_size_t], c_void_p
    n = ctypes.sizeof(buf)
    addr = ole.CoTaskMemAlloc(n)
    ctypes.memmove(addr, buf, n)
    return addr


def _stub(slots):
    return [(H, (), (lambda this: W.E_NOTIMPL)) for _ in range(slots)]


class FakeCaptureClient:
    """IAudioCaptureClient serving a queue of packets: (bytes, frames, flags, qpc_100ns)."""

    def __init__(self, packets=(), error_after=None):
        self.packets = list(packets)
        self.released = []
        self.error_after = error_after       # after this many packets GetNextPacketSize fails (device unplugged)
        self.served = 0
        self._buf = None
        self.com = com([W.IID_IAudioCaptureClient], [
            (H, (POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint64), POINTER(c_uint64)), self.get_buffer),
            (H, (c_uint32,), self.release_buffer),
            (H, (POINTER(c_uint32),), self.next_size),
        ])

    def next_size(self, this, pn):
        if self.error_after is not None and self.served >= self.error_after:
            return W.AUDCLNT_E_DEVICE_INVALIDATED
        pn[0] = self.packets[0][1] if self.packets else 0
        return W.S_OK

    def get_buffer(self, this, ppdata, pframes, pflags, pdev, pqpc):
        if not self.packets:
            pframes[0] = 0
            return W.AUDCLNT_S_BUFFER_EMPTY
        data, frames, flags, qpc = self.packets.pop(0)
        self._buf = ctypes.create_string_buffer(data, max(1, len(data)))
        ppdata[0] = ctypes.addressof(self._buf) if data else None
        pframes[0], pflags[0], pdev[0], pqpc[0] = frames, flags, self.served * 1000, qpc
        self.served += 1
        return W.S_OK

    def release_buffer(self, this, n):
        self.released.append(n)
        return W.S_OK


class FakeAudioClient:
    """IAudioClient: accepts only the formats in `accept`; records Initialize arguments."""

    def __init__(self, accept=(), mix: Format | None = None, capture: FakeCaptureClient | None = None, init_hr=None):
        self.accept = set(accept)
        self.mix = mix
        self.capture = capture or FakeCaptureClient()
        self.init_hr = init_hr
        self.inits = []
        self.started = self.stopped = False
        self.event = "unset"
        self._mixkeep = []
        self.com = com([W.IID_IAudioClient], [
            (H, (c_int, c_uint32, c_int64, c_int64, c_void_p, c_void_p), self.initialize),     # 3
            *_stub(4),                                                                        # 4..7
            (H, (POINTER(c_void_p),), self.get_mix_format),                                   # 8
            *_stub(1),                                                                        # 9
            (H, (), self.start),                                                              # 10
            (H, (), self.stop),                                                               # 11
            *_stub(1),                                                                        # 12
            (H, (c_void_p,), self.set_event),                                                 # 13
            (H, (POINTER(W.GUID), POINTER(c_void_p)), self.get_service),                     # 14
        ])

    def initialize(self, this, mode, flags, buf, period, pfmt, guid):
        fmt = W.parse_waveformat(pfmt)
        w = W.WAVEFORMATEX.from_address(pfmt)
        self.inits.append({"mode": mode, "flags": flags, "buffer": buf, "period": period, "format": fmt,
                           "avg": w.nAvgBytesPerSec, "align": w.nBlockAlign, "cb": w.cbSize})
        if self.init_hr is not None:
            return self.init_hr
        return W.S_OK if fmt in self.accept else W.AUDCLNT_E_UNSUPPORTED_FORMAT

    def get_mix_format(self, this, pp):
        if self.mix is None:
            return W.E_NOTIMPL
        # WAVEFORMATEXTENSIBLE: 18 + 22 bytes, SubFormat at offset 24
        buf = ctypes.create_string_buffer(40)
        w = W.WAVEFORMATEX.from_buffer(buf)
        w.wFormatTag, w.nChannels, w.nSamplesPerSec = W.WAVE_FORMAT_EXTENSIBLE, self.mix.channels, self.mix.rate
        w.wBitsPerSample, w.nBlockAlign, w.cbSize = self.mix.bits, self.mix.block_align, 22
        sub = W.KSDATAFORMAT_SUBTYPE_IEEE_FLOAT if self.mix.is_float else W.KSDATAFORMAT_SUBTYPE_PCM
        ctypes.memmove(ctypes.addressof(buf) + 24, bytes(sub), 16)
        pp[0] = task_copy(buf, self._mixkeep)
        return W.S_OK

    def start(self, this):
        self.started = True
        return W.S_OK

    def stop(self, this):
        self.stopped = True
        return W.S_OK

    def set_event(self, this, h):
        self.event = h
        return W.S_OK

    def get_service(self, this, riid, pp):
        if bytes(riid.contents) != bytes(W.IID_IAudioCaptureClient):
            pp[0] = None
            return W.E_NOINTERFACE
        self.capture.com.refs += 1
        pp[0] = self.capture.com.addr
        return W.S_OK


class FakeDevice:
    def __init__(self, dev_id, name, client_factory, activate_hr=None):
        self.id, self.name = dev_id, name
        self.client_factory = client_factory
        self.activate_hr = activate_hr
        self.clients = []
        self._keep = []
        self.com = com([], [
            (H, (POINTER(W.GUID), c_uint32, c_void_p, POINTER(c_void_p)), self.activate),   # 3
            (H, (c_uint32, POINTER(c_void_p)), self.open_store),                            # 4
            (H, (POINTER(c_void_p),), self.get_id),                                         # 5
        ])

    def activate(self, this, riid, ctx, params, pp):
        if self.activate_hr is not None:
            pp[0] = None
            return self.activate_hr
        c = self.client_factory()
        self.clients.append(c)
        pp[0] = c.com.addr
        return W.S_OK

    def get_id(self, this, pp):
        pp[0] = task_wstr(self.id, self._keep)
        return W.S_OK

    def open_store(self, this, mode, pp):
        store = FakePropertyStore(self.name)
        self._keep.append(store)
        pp[0] = store.com.addr
        return W.S_OK


class FakePropertyStore:
    def __init__(self, name):
        self.name = name
        self._keep = []
        self.com = com([], [*_stub(2), (H, (POINTER(W.PROPERTYKEY), POINTER(W.PROPVARIANT)), self.get_value)])

    def get_value(self, this, key, pv):
        k = key.contents
        if not (k.fmtid == W.PKEY_Device_FriendlyName.fmtid and k.pid == 14):
            return W.E_INVALIDARG
        v = pv.contents
        v.vt = W.VT_LPWSTR
        c_void_p.from_buffer(v, 8).value = task_wstr(self.name, self._keep)
        return W.S_OK


class FakeCollection:
    def __init__(self, devices):
        self.devices = devices
        self.com = com([], [(H, (POINTER(c_uint32),), self.count), (H, (c_uint32, POINTER(c_void_p)), self.item)])

    def count(self, this, pn):
        pn[0] = len(self.devices)
        return W.S_OK

    def item(self, this, i, pp):
        pp[0] = self.devices[i].com.addr
        return W.S_OK


class FakeEnumerator:
    """IMMDeviceEnumerator over a dict of FakeDevice; `default` is the id of the default capture device (or None)."""

    def __init__(self, devices, default=None):
        self.devices = {d.id: d for d in devices}
        self.default = default
        self.calls = []
        self._keep = []
        self.com = com([W.IID_IMMDeviceEnumerator], [
            (H, (c_int, c_uint32, POINTER(c_void_p)), self.enum),                 # 3
            (H, (c_int, c_int, POINTER(c_void_p)), self.get_default),              # 4
            (H, (c_wchar_p, POINTER(c_void_p)), self.get_device),                  # 5
        ])

    def enum(self, this, flow, state, pp):
        coll = FakeCollection(list(self.devices.values()))
        self._keep.append(coll)
        pp[0] = coll.com.addr
        return W.S_OK

    def get_default(self, this, flow, role, pp):
        self.calls.append(("default", flow, role))
        if self.default is None:
            pp[0] = None
            return W.E_NOTFOUND
        pp[0] = self.devices[self.default].com.addr
        return W.S_OK

    def get_device(self, this, dev_id, pp):
        if dev_id not in self.devices:
            pp[0] = None
            return W.E_NOTFOUND
        pp[0] = self.devices[dev_id].com.addr
        return W.S_OK

    def as_enumerator(self):
        e = object.__new__(W.DeviceEnumerator)
        e.ptr = W.ComPtr(self.com.addr, "IMMDeviceEnumerator")
        return e


class FakeOperation:
    """IActivateAudioInterfaceAsyncOperation returning (hr, interface)."""

    def __init__(self, hr, unk):
        self.hr, self.unk = hr, unk
        self.com = com([W.IID_IActivateAudioInterfaceAsyncOperation],
                                 [(H, (POINTER(H), POINTER(c_void_p)), self.result)])

    def result(self, this, phr, pp):
        phr[0] = self.hr
        pp[0] = self.unk
        return W.S_OK


class LiveCaptureClient(FakeCaptureClient):
    """IAudioCaptureClient that produces a constant-value signal in real time (10 ms packets, no device timestamps)."""

    def __init__(self, fmt: Format, value: float, clock):
        super().__init__()
        self.fmt, self.value, self.clock = fmt, value, clock
        self.start = None
        self.produced = 0
        self.dead = False
        self.chunk = fmt.rate // 100

    def _packet(self):
        import numpy as np
        x = np.full((self.chunk, self.fmt.channels), self.value)
        if self.fmt.is_float:
            return x.astype("<f4").tobytes()
        return np.round(x * 32767).astype("<i2").tobytes()

    def next_size(self, this, pn):
        if self.dead:
            return W.AUDCLNT_E_DEVICE_INVALIDATED
        now = self.clock()
        if self.start is None:
            self.start = now
        ready = int((now - self.start) * self.fmt.rate) - self.produced
        pn[0] = self.chunk if ready >= self.chunk else 0
        return W.S_OK

    def get_buffer(self, this, ppdata, pframes, pflags, pdev, pqpc):
        self.packets = [(self._packet(), self.chunk, 0, 0)]
        self.produced += self.chunk
        return super().get_buffer(this, ppdata, pframes, pflags, pdev, pqpc)
