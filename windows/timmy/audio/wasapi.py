"""Raw WASAPI / MMDevice COM through ctypes (no comtypes): exactly the calls Timmy needs, nothing else.

Why plain ctypes (decision at the end of build step 1, DESIGN.md 5.1): every COM call is one vtable slot with a
declared signature, so the code reads like Microsoft's ApplicationLoopback sample; the one COM object we implement
(the activation completion handler, which must be agile) is a 4-slot vtable of ctypes callbacks; there is no
apartment magic (comtypes' `sys.coinit_flags`, generated modules, reference-counting finalizers on the wrong
thread); and the call machinery is testable on any OS with fake COM objects built the same way (tests/test_wasapi.py).

Threading: every thread that touches these objects calls com_init() (MTA) first and com_uninit() at the end. This
module is only used in the capture process (timmy.audio.capture), never in the Qt UI process.

Vtable slots (IUnknown = 0 QueryInterface, 1 AddRef, 2 Release):
  IMMDeviceEnumerator   3 EnumAudioEndpoints, 4 GetDefaultAudioEndpoint, 5 GetDevice
  IMMDeviceCollection   3 GetCount, 4 Item
  IMMDevice             3 Activate, 4 OpenPropertyStore, 5 GetId, 6 GetState
  IPropertyStore        5 GetValue
  IAudioClient          3 Initialize, 4 GetBufferSize, 8 GetMixFormat, 10 Start, 11 Stop, 13 SetEventHandle, 14 GetService
  IAudioCaptureClient   3 GetBuffer, 4 ReleaseBuffer, 5 GetNextPacketSize
  IActivateAudioInterfaceAsyncOperation      3 GetActivateResult
  IActivateAudioInterfaceCompletionHandler   3 ActivateCompleted  (implemented here, plus IAgileObject)
"""
import ctypes
import functools
import sys
import threading
import uuid
from ctypes import POINTER, Structure, byref, c_int, c_int32, c_int64, c_size_t, c_ubyte, c_uint16, c_uint32, c_uint64, c_void_p, c_wchar_p

from timmy.audio.resample import Format

IS_WIN = sys.platform == "win32"
FUNCTYPE = ctypes.WINFUNCTYPE if IS_WIN else ctypes.CFUNCTYPE   # x64 Windows has one calling convention: same ABI
HRESULT = c_int32         # LONG is 32-bit on Windows; explicit so the fake-object tests behave the same on any OS

# ---- HRESULTs ----------------------------------------------------------------------------------------------------


def _s32(v):
    return v - (1 << 32) if v >= 0x80000000 else v


S_OK = 0
S_FALSE = 1
E_NOINTERFACE = _s32(0x80004002)
E_POINTER = _s32(0x80004003)
E_FAIL = _s32(0x80004005)
E_ACCESSDENIED = _s32(0x80070005)
E_INVALIDARG = _s32(0x80070057)
E_NOTFOUND = _s32(0x80070490)              # HRESULT_FROM_WIN32(ERROR_NOT_FOUND): no default device
E_NOTIMPL = _s32(0x80004001)
E_ILLEGAL_METHOD_CALL = _s32(0x8000000E)   # activation with a non-agile completion handler
RPC_E_CHANGED_MODE = _s32(0x80010106)
REGDB_E_CLASSNOTREG = _s32(0x80040154)
AUDCLNT_E_NOT_INITIALIZED = _s32(0x88890001)
AUDCLNT_E_UNSUPPORTED_FORMAT = _s32(0x88890008)
AUDCLNT_E_DEVICE_INVALIDATED = _s32(0x88890004)
AUDCLNT_E_DEVICE_IN_USE = _s32(0x8889000A)
AUDCLNT_E_SERVICE_NOT_RUNNING = _s32(0x88890010)
AUDCLNT_E_ENDPOINT_CREATE_FAILED = _s32(0x8889000F)
AUDCLNT_E_RESOURCES_INVALIDATED = _s32(0x88890026)
AUDCLNT_S_BUFFER_EMPTY = 0x08890001

HR_NAMES = {v: k for k, v in list(globals().items()) if k.startswith(("E_", "S_", "AUDCLNT_", "RPC_", "REGDB_")) and isinstance(v, int)}

# errors after which a stream must be reopened (the device went away or the audio engine restarted)
REOPEN_ERRORS = {AUDCLNT_E_DEVICE_INVALIDATED, AUDCLNT_E_RESOURCES_INVALIDATED, AUDCLNT_E_SERVICE_NOT_RUNNING}


def hr_hex(hr: int) -> str:
    return "0x%08X" % (int(hr) & 0xFFFFFFFF)


def hr_name(hr: int) -> str:
    return HR_NAMES.get(int(hr), hr_hex(hr))


class ComError(OSError):
    def __init__(self, what: str, hr: int):
        self.hr = int(hr)
        super().__init__(f"{what} failed: {hr_name(hr)} ({hr_hex(hr)})")


def check(hr: int, what: str) -> int:
    if hr is None:
        hr = 0
    if hr < 0:
        raise ComError(what, hr)
    return hr


# ---- GUIDs and structures ------------------------------------------------------------------------------------------


class GUID(Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_ubyte * 8)]

    @classmethod
    def from_str(cls, s: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(s.strip("{}")).bytes_le)

    def __str__(self):
        return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"

    def __eq__(self, other):
        return isinstance(other, GUID) and bytes(self) == bytes(other)

    def __hash__(self):
        return hash(bytes(self))


IID_IUnknown = GUID.from_str("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = GUID.from_str("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
CLSID_MMDeviceEnumerator = GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioClient = GUID.from_str("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID.from_str("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
IID_IActivateAudioInterfaceAsyncOperation = GUID.from_str("72A22D78-CDE4-431D-B8CC-843A71199B6D")
IID_IActivateAudioInterfaceCompletionHandler = GUID.from_str("41D949AB-9862-444A-80F6-C261334DA5EB")
KSDATAFORMAT_SUBTYPE_PCM = GUID.from_str("00000001-0000-0010-8000-00AA00389B71")
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID.from_str("00000003-0000-0010-8000-00AA00389B71")


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_uint32)]


PKEY_Device_FriendlyName = PROPERTYKEY(GUID.from_str("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", c_uint16), ("nChannels", c_uint16), ("nSamplesPerSec", c_uint32),
                ("nAvgBytesPerSec", c_uint32), ("nBlockAlign", c_uint16), ("wBitsPerSample", c_uint16), ("cbSize", c_uint16)]


WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


def waveformat(fmt: Format) -> WAVEFORMATEX:
    w = WAVEFORMATEX()
    w.wFormatTag = WAVE_FORMAT_IEEE_FLOAT if fmt.is_float else WAVE_FORMAT_PCM
    w.nChannels, w.nSamplesPerSec, w.wBitsPerSample = fmt.channels, fmt.rate, fmt.bits
    w.nBlockAlign = fmt.block_align
    w.nAvgBytesPerSec = fmt.rate * fmt.block_align
    w.cbSize = 0
    return w


def parse_waveformat(addr: int) -> Format:
    """WAVEFORMATEX / WAVEFORMATEXTENSIBLE at `addr` -> Format."""
    w = WAVEFORMATEX.from_address(addr)
    is_float = w.wFormatTag == WAVE_FORMAT_IEEE_FLOAT
    if w.wFormatTag == WAVE_FORMAT_EXTENSIBLE and w.cbSize >= 22:
        sub = GUID.from_address(addr + 24)              # after WAVEFORMATEX(18) + Samples(2) + dwChannelMask(4)
        is_float = sub == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
    return Format(int(w.nSamplesPerSec), int(w.nChannels), int(w.wBitsPerSample), bool(is_float))


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    # AUDIOCLIENT_ACTIVATION_TYPE ActivationType; union { AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS { DWORD TargetProcessId;
    # PROCESS_LOOPBACK_MODE ProcessLoopbackMode; } }  -> 12 bytes
    _fields_ = [("ActivationType", c_int), ("TargetProcessId", c_uint32), ("ProcessLoopbackMode", c_int)]


AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
VT_BLOB = 65
VT_LPWSTR = 31


class PROPVARIANT(Structure):
    # x64 layout: VARTYPE vt; WORD wReserved1..3; then a 16-byte union. VT_BLOB = {ULONG cbSize; BYTE *pBlobData},
    # VT_LPWSTR = {LPWSTR pwszVal}. 24 bytes total.
    _fields_ = [("vt", c_uint16), ("wReserved1", c_uint16), ("wReserved2", c_uint16), ("wReserved3", c_uint16),
                ("cbSize", c_uint32), ("pBlobData", c_void_p)]

    @property
    def pwszVal(self):
        return c_void_p.from_buffer(self, 8).value


# ---- calling and implementing COM ----------------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _proto(restype, argtypes):
    return FUNCTYPE(restype, c_void_p, *argtypes)


def vcall(this: int, index: int, restype, argtypes: tuple, *args):
    """Call vtable slot `index` of the COM object at address `this`."""
    if not this:
        raise ComError(f"vtable slot {index} on a null pointer", E_POINTER)
    vtbl = ctypes.cast(c_void_p(this), POINTER(POINTER(c_void_p))).contents
    fn = vtbl[index]
    return _proto(restype, tuple(argtypes))(fn)(this, *args)


class ComPtr:
    """An owned interface pointer (one reference). release() exactly once (or use `with`)."""

    __slots__ = ("addr", "name")

    def __init__(self, addr, name: str = "COM object"):
        self.addr = int(addr or 0)
        self.name = name

    def __bool__(self):
        return bool(self.addr)

    def call(self, index, argtypes=(), *args, restype=HRESULT):
        return vcall(self.addr, index, restype, tuple(argtypes), *args)

    def query(self, iid: GUID, name: str = "") -> "ComPtr":
        p = c_void_p()
        check(self.call(0, (POINTER(GUID), POINTER(c_void_p)), byref(iid), byref(p)), f"QueryInterface {name or iid}")
        return ComPtr(p.value, name or str(iid))

    def add_ref(self) -> int:
        return self.call(1, (), restype=c_uint32)

    def release(self) -> int:
        if not self.addr:
            return 0
        n = self.call(2, (), restype=c_uint32)
        self.addr = 0
        return n

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


_QI = FUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
_REF = FUNCTYPE(c_uint32, c_void_p)


class PyComObject:
    """A COM object implemented in Python: a pointer to a vtable of ctypes callbacks.

    `methods` are the slots after IUnknown: (restype, argtypes, function(this, *args)). The memory is owned by this
    Python object, so keep it alive for as long as anything may call it (see _KEEP below).
    """

    def __init__(self, iids, methods):
        self.refs = 1
        self._iids = {bytes(IID_IUnknown)} | {bytes(i) for i in iids}
        funcs = [_QI(self._query), _REF(self._add_ref), _REF(self._release)]
        for restype, argtypes, fn in methods:
            funcs.append(FUNCTYPE(restype, c_void_p, *argtypes)(self._guard(fn, restype)))
        self._funcs = funcs
        self._vtbl = (c_void_p * len(funcs))(*[ctypes.cast(f, c_void_p).value for f in funcs])
        self._obj = (c_void_p * 1)(ctypes.cast(self._vtbl, c_void_p).value)
        self.addr = ctypes.addressof(self._obj)

    @staticmethod
    def _guard(fn, restype):
        def call(*a):
            try:
                r = fn(*a)
                return 0 if r is None else r
            except Exception:                    # an exception must never cross into native code
                return E_FAIL if restype is HRESULT else 0
        return call

    def _query(self, this, riid, ppv):
        try:
            if not ppv:
                return E_POINTER
            if riid and bytes(riid.contents) in self._iids:
                ppv[0] = this
                self.refs += 1
                return S_OK
            ppv[0] = None
            return E_NOINTERFACE
        except Exception:
            return E_FAIL

    def _add_ref(self, this):
        self.refs += 1
        return self.refs

    def _release(self, this):
        self.refs = max(0, self.refs - 1)
        return self.refs


# Handlers handed to Windows stay referenced here until Windows released them (refs == 0 after we dropped ours);
# pruned lazily, never from inside one of their own callbacks.
_KEEP: list = []
_KEEP_LOCK = threading.Lock()


def _keep(obj):
    with _KEEP_LOCK:
        _KEEP[:] = [o for o in _KEEP if o.refs > 0]
        _KEEP.append(obj)


class ActivationHandler:
    """IActivateAudioInterfaceCompletionHandler + IAgileObject. ActivateCompleted stores the result and signals."""

    def __init__(self):
        self.done = threading.Event()
        self.hr = None
        self.unk = 0
        self.com = PyComObject([IID_IActivateAudioInterfaceCompletionHandler, IID_IAgileObject],
                               [(HRESULT, (c_void_p,), self._completed)])

    def _completed(self, this, op):
        try:
            hr, unk = HRESULT(E_FAIL), c_void_p()
            r = vcall(op, 3, HRESULT, (POINTER(HRESULT), POINTER(c_void_p)), byref(hr), byref(unk))
            self.hr = r if r < 0 else hr.value
            self.unk = unk.value or 0
        except Exception:
            self.hr = E_FAIL
        finally:
            self.done.set()
        return S_OK


# ---- Win32 basics (Windows only, loaded lazily so the module imports everywhere) -------------------------------------


@functools.lru_cache(maxsize=None)
def _dll(name):
    return ctypes.WinDLL(name)


def ole32():
    d = _dll("ole32")
    d.CoInitializeEx.argtypes, d.CoInitializeEx.restype = [c_void_p, c_uint32], HRESULT
    d.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_uint32, POINTER(GUID), POINTER(c_void_p)]
    d.CoCreateInstance.restype = HRESULT
    d.CoTaskMemFree.argtypes, d.CoTaskMemFree.restype = [c_void_p], None
    d.PropVariantClear.argtypes, d.PropVariantClear.restype = [POINTER(PROPVARIANT)], HRESULT
    return d


def kernel32():
    k = _dll("kernel32")
    k.CreateEventW.argtypes, k.CreateEventW.restype = [c_void_p, c_int, c_int, c_wchar_p], c_void_p
    k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [c_void_p, c_uint32], c_uint32
    k.CloseHandle.argtypes, k.CloseHandle.restype = [c_void_p], c_int
    return k


COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 0x17
STGM_READ = 0
E_CAPTURE, E_RENDER, E_ALL = 1, 0, 2           # EDataFlow
ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS = 0, 1, 2
DEVICE_STATE_ACTIVE = 0x1


# Off Windows (dev hosts, the fake-object tests) the few Win32 calls below are inert stand-ins.


def com_init():
    """Join the MTA on this thread. Returns True if this call must be balanced with com_uninit()."""
    if not IS_WIN:
        return False
    hr = ole32().CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr == RPC_E_CHANGED_MODE:      # the thread is already STA (should not happen in the capture process): use it
        return False
    check(hr, "CoInitializeEx")
    return True


def com_uninit():
    if IS_WIN:
        _dll("ole32").CoUninitialize()


def co_task_mem_free(addr):
    if addr and IS_WIN:
        ole32().CoTaskMemFree(c_void_p(addr))


def prop_variant_clear(pv):
    if IS_WIN:
        ole32().PropVariantClear(byref(pv))


class Event:
    """Auto-reset Win32 event (the WASAPI event handle)."""

    def __init__(self):
        if not IS_WIN:
            self.handle, self._ev = None, threading.Event()
            return
        self.handle = kernel32().CreateEventW(None, 0, 0, None)
        if not self.handle:
            raise ctypes.WinError()

    def wait(self, ms: int) -> bool:
        if not IS_WIN:
            return self._ev.wait(ms / 1000.0)
        return kernel32().WaitForSingleObject(self.handle, int(ms)) == 0

    def close(self):
        if self.handle and IS_WIN:
            kernel32().CloseHandle(self.handle)
        self.handle = None


# ---- devices -------------------------------------------------------------------------------------------------------


class DeviceEnumerator:
    def __init__(self):
        p = c_void_p()
        check(ole32().CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, byref(IID_IMMDeviceEnumerator), byref(p)),
              "CoCreateInstance(MMDeviceEnumerator)")
        self.ptr = ComPtr(p.value, "IMMDeviceEnumerator")

    def default(self, flow=E_CAPTURE, role=ROLE_CONSOLE):
        """The default endpoint as a ComPtr(IMMDevice), or None when there is none (E_NOTFOUND)."""
        p = c_void_p()
        hr = self.ptr.call(4, (c_int, c_int, POINTER(c_void_p)), flow, role, byref(p))
        if hr == E_NOTFOUND or (hr >= 0 and not p.value):
            return None
        check(hr, "GetDefaultAudioEndpoint")
        return ComPtr(p.value, "IMMDevice")

    def default_id(self, flow=E_CAPTURE, role=ROLE_CONSOLE):
        dev = self.default(flow, role)
        if dev is None:
            return None
        with dev:
            return device_id(dev)

    def device(self, dev_id: str) -> ComPtr:
        p = c_void_p()
        check(self.ptr.call(5, (c_wchar_p, POINTER(c_void_p)), dev_id, byref(p)), "IMMDeviceEnumerator.GetDevice")
        return ComPtr(p.value, "IMMDevice")

    def endpoints(self, flow=E_CAPTURE, state=DEVICE_STATE_ACTIVE):
        """[(id, friendly name)] of the active endpoints."""
        p = c_void_p()
        check(self.ptr.call(3, (c_int, c_uint32, POINTER(c_void_p)), flow, state, byref(p)), "EnumAudioEndpoints")
        out = []
        with ComPtr(p.value, "IMMDeviceCollection") as coll:
            n = c_uint32()
            check(coll.call(3, (POINTER(c_uint32),), byref(n)), "IMMDeviceCollection.GetCount")
            for i in range(n.value):
                d = c_void_p()
                if coll.call(4, (c_uint32, POINTER(c_void_p)), i, byref(d)) < 0:
                    continue
                with ComPtr(d.value, "IMMDevice") as dev:
                    out.append((device_id(dev), friendly_name(dev)))
        return out

    def release(self):
        self.ptr.release()


def device_id(dev: ComPtr) -> str:
    p = c_void_p()
    check(dev.call(5, (POINTER(c_void_p),), byref(p)), "IMMDevice.GetId")
    try:
        return ctypes.wstring_at(p.value) if p.value else ""
    finally:
        co_task_mem_free(p.value)


def friendly_name(dev: ComPtr) -> str:
    try:
        ps = c_void_p()
        check(dev.call(4, (c_uint32, POINTER(c_void_p)), STGM_READ, byref(ps)), "OpenPropertyStore")
        with ComPtr(ps.value, "IPropertyStore") as store:
            v = PROPVARIANT()
            check(store.call(5, (POINTER(PROPERTYKEY), POINTER(PROPVARIANT)), byref(PKEY_Device_FriendlyName), byref(v)), "GetValue")
            try:
                return ctypes.wstring_at(v.pwszVal) if v.vt == VT_LPWSTR and v.pwszVal else ""
            finally:
                prop_variant_clear(v)
    except OSError:
        return ""


def activate_audio_client(dev: ComPtr) -> ComPtr:
    p = c_void_p()
    check(dev.call(3, (POINTER(GUID), c_uint32, c_void_p, POINTER(c_void_p)), byref(IID_IAudioClient), CLSCTX_ALL, None, byref(p)),
          "IMMDevice.Activate(IAudioClient)")
    return ComPtr(p.value, "IAudioClient")


def activate_process_loopback(pid: int, include_tree: bool = True, timeout: float = 5.0) -> ComPtr:
    """IAudioClient that captures what process `pid` (and, by default, its child processes) plays. Windows 10 2004+."""
    params = AUDIOCLIENT_ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, int(pid),
                                           PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE if include_tree
                                           else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE)
    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.cbSize = ctypes.sizeof(params)
    pv.pBlobData = ctypes.cast(ctypes.pointer(params), c_void_p).value
    fn = _dll("Mmdevapi").ActivateAudioInterfaceAsync
    fn.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT), c_void_p, POINTER(c_void_p)]
    fn.restype = HRESULT
    handler = ActivationHandler()
    _keep(handler.com)
    op = c_void_p()
    try:
        check(fn(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IID_IAudioClient), byref(pv), handler.com.addr, byref(op)),
              "ActivateAudioInterfaceAsync")
        if not handler.done.wait(timeout):
            raise ComError("process loopback activation (timeout)", E_FAIL)
    finally:
        handler.com.refs -= 1                          # our own reference; Windows releases its own
        if op.value:
            ComPtr(op.value, "IActivateAudioInterfaceAsyncOperation").release()
    check(handler.hr if handler.hr is not None else E_FAIL, "process loopback activation")
    if not handler.unk:
        raise ComError("process loopback activation (no interface)", E_NOINTERFACE)
    with ComPtr(handler.unk, "IUnknown") as unk:
        return unk.query(IID_IAudioClient, "IAudioClient")


# ---- capture streams -----------------------------------------------------------------------------------------------

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4
BUFFER_HNS = 10_000_000          # 1 s shared-mode buffer: plenty of room for a Python thread that is late

# What we ask for, in order (DESIGN.md 5.1). AUTOCONVERTPCM lets the engine convert to 16 kHz mono float itself.
LOOPBACK_FORMATS = (Format(16000, 1, 32, True), Format(48000, 2, 16, False), Format(44100, 2, 16, False))
MIC_FORMATS = (Format(16000, 1, 32, True), Format(48000, 2, 32, True), Format(48000, 1, 16, False), Format(44100, 2, 16, False))


def get_mix_format(client: ComPtr):
    p = c_void_p()
    if client.call(8, (POINTER(c_void_p),), byref(p)) < 0 or not p.value:
        return None
    try:
        return parse_waveformat(p.value)
    finally:
        co_task_mem_free(p.value)


def initialize(client: ComPtr, fmt: Format, loopback: bool) -> int:
    flags = AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
    if loopback:
        flags |= AUDCLNT_STREAMFLAGS_LOOPBACK
    w = waveformat(fmt)
    return client.call(3, (c_int, c_uint32, c_int64, c_int64, POINTER(WAVEFORMATEX), c_void_p),
                       AUDCLNT_SHAREMODE_SHARED, flags, BUFFER_HNS, 0, byref(w), None)


class Packet:
    __slots__ = ("t", "data", "frames", "flags")

    def __init__(self, t, data, frames, flags):
        self.t, self.data, self.frames, self.flags = t, data, frames, flags

    @property
    def silent(self):
        return bool(self.flags & AUDCLNT_BUFFERFLAGS_SILENT)


class CaptureStream:
    """An initialized IAudioClient + its IAudioCaptureClient. Owns both references."""

    def __init__(self, client: ComPtr, fmt: Format, clock=None):
        from timmy.audio.padder import clock as default_clock
        self.client, self.fmt = client, fmt
        self.clock = clock or default_clock
        self.capture = None
        self.event = None
        self.packets = 0

    @classmethod
    def open(cls, make_client, formats, loopback: bool):
        """Try each format on a fresh client from make_client(); returns (stream, tried) or raises the last ComError."""
        tried, last = [], None
        for fmt in dict.fromkeys(formats):
            client = make_client()
            hr = initialize(client, fmt, loopback)
            tried.append((str(fmt), hr_hex(hr)))
            if hr >= 0:
                return cls(client, fmt), tried
            client.release()
            last = ComError(f"IAudioClient.Initialize({fmt})", hr)
            if hr in REOPEN_ERRORS or hr == E_ACCESSDENIED:
                break
        raise last or ComError("IAudioClient.Initialize", E_FAIL)

    def start(self):
        self.event = Event()
        check(self.client.call(13, (c_void_p,), self.event.handle), "IAudioClient.SetEventHandle")
        p = c_void_p()
        check(self.client.call(14, (POINTER(GUID), POINTER(c_void_p)), byref(IID_IAudioCaptureClient), byref(p)),
              "IAudioClient.GetService(IAudioCaptureClient)")
        self.capture = ComPtr(p.value, "IAudioCaptureClient")
        check(self.client.call(10), "IAudioClient.Start")

    def wait(self, ms: int = 100) -> bool:
        return self.event.wait(ms) if self.event else False

    def drain(self, limit: int = 256):
        """Read every packet that is ready. Raises ComError (e.g. AUDCLNT_E_DEVICE_INVALIDATED)."""
        out = []
        cap = self.capture
        n = c_uint32()
        data, frames, flags = c_void_p(), c_uint32(), c_uint32()
        devpos, qpc = c_uint64(), c_uint64()
        for _ in range(limit):
            check(cap.call(5, (POINTER(c_uint32),), byref(n)), "IAudioCaptureClient.GetNextPacketSize")
            if n.value == 0:
                break
            hr = check(cap.call(3, (POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint64), POINTER(c_uint64)),
                                byref(data), byref(frames), byref(flags), byref(devpos), byref(qpc)), "IAudioCaptureClient.GetBuffer")
            if hr == AUDCLNT_S_BUFFER_EMPTY or frames.value == 0:
                cap.call(4, (c_uint32,), 0)
                break
            f, fl = frames.value, flags.value
            silent = bool(fl & AUDCLNT_BUFFERFLAGS_SILENT) or not data.value
            raw = b"" if silent else ctypes.string_at(data.value, f * self.fmt.block_align)
            check(cap.call(4, (c_uint32,), f), "IAudioCaptureClient.ReleaseBuffer")
            out.append(Packet(self._time(qpc.value, f, fl), raw, f, fl | (AUDCLNT_BUFFERFLAGS_SILENT if silent else 0)))
            self.packets += 1
        return out

    def _time(self, qpc_100ns: int, frames: int, flags: int) -> float:
        """clock() time of the packet's first frame: the device timestamp when it is sane, else now - duration."""
        now = self.clock()
        if qpc_100ns and not flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR:
            t = qpc_100ns / 1e7
            if now - 5.0 < t <= now + 0.05:
                return t
        return now - frames / float(self.fmt.rate)

    def close(self):
        try:
            if self.client:
                self.client.call(11)                       # Stop
        except OSError:
            pass
        for p in (self.capture, self.client):
            try:
                if p:
                    p.release()
            except OSError:
                pass
        if self.event:
            self.event.close()
        self.capture = None
        self.event = None


# ---- process list (Toolhelp32) -------------------------------------------------------------------------------------


class PROCESSENTRY32W(Structure):
    _fields_ = [("dwSize", c_uint32), ("cntUsage", c_uint32), ("th32ProcessID", c_uint32), ("th32DefaultHeapID", c_size_t),
                ("th32ModuleID", c_uint32), ("cntThreads", c_uint32), ("th32ParentProcessID", c_uint32),
                ("pcPriClassBase", c_int32), ("dwFlags", c_uint32), ("szExeFile", ctypes.c_wchar * 260)]


def toolhelp_processes():
    """[(pid, ppid, exe name)] of every process (Windows)."""
    k = _dll("kernel32")
    k.CreateToolhelp32Snapshot.argtypes, k.CreateToolhelp32Snapshot.restype = [c_uint32, c_uint32], c_void_p
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = k.Process32NextW.restype = c_int
    k.CloseHandle.argtypes, k.CloseHandle.restype = [c_void_p], c_int
    snap = k.CreateToolhelp32Snapshot(0x2, 0)               # TH32CS_SNAPPROCESS
    if not snap or snap == c_void_p(-1).value:
        raise ctypes.WinError()
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
            ok = k.Process32NextW(snap, byref(e))
    finally:
        k.CloseHandle(snap)
    return out
