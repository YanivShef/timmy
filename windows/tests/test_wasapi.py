"""wasapi.py on any OS: structure layouts, GUIDs, and every vtable call path against fake COM objects."""
import ctypes
import struct
import threading
from ctypes import c_void_p

import numpy as np
import pytest

from tests.fakecom import FakeAudioClient, FakeCaptureClient, FakeDevice, FakeEnumerator, FakeOperation
from timmy.audio import wasapi as W
from timmy.audio.resample import Format

X64 = ctypes.sizeof(c_void_p) == 8


def test_struct_layouts_match_the_windows_headers():
    assert ctypes.sizeof(W.GUID) == 16
    assert ctypes.sizeof(W.WAVEFORMATEX) == 18
    assert W.WAVEFORMATEX.cbSize.offset == 16
    assert ctypes.sizeof(W.AUDIOCLIENT_ACTIVATION_PARAMS) == 12
    assert ctypes.sizeof(W.PROPERTYKEY) == 20
    if X64:
        assert ctypes.sizeof(W.PROPVARIANT) == 24
        assert W.PROPVARIANT.cbSize.offset == 8 and W.PROPVARIANT.pBlobData.offset == 16
    if X64 and ctypes.sizeof(ctypes.c_wchar) == 2:            # WCHAR is UTF-16 only on Windows
        assert ctypes.sizeof(W.PROCESSENTRY32W) == 568
        assert W.PROCESSENTRY32W.th32ParentProcessID.offset == 32
        assert W.PROCESSENTRY32W.szExeFile.offset == 44


def test_guid_roundtrip_and_known_values():
    g = W.GUID.from_str("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    assert str(g) == "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
    assert g.Data1 == 0x1CB9AD4C and g.Data2 == 0xDBFA and g.Data3 == 0x4C32 and bytes(g.Data4) == bytes.fromhex("B178C2F568A703B2")
    assert g == W.IID_IAudioClient and hash(g) == hash(W.IID_IAudioClient)
    assert W.IID_IAgileObject != W.IID_IUnknown


def test_hresult_helpers():
    assert W.hr_hex(W.AUDCLNT_E_DEVICE_INVALIDATED) == "0x88890004"
    assert W.hr_name(W.AUDCLNT_E_DEVICE_INVALIDATED) == "AUDCLNT_E_DEVICE_INVALIDATED"
    assert W.hr_name(W.E_NOTFOUND) == "E_NOTFOUND"
    assert W.check(W.AUDCLNT_S_BUFFER_EMPTY, "x") == W.AUDCLNT_S_BUFFER_EMPTY      # success codes pass
    with pytest.raises(W.ComError) as e:
        W.check(W.E_ACCESSDENIED, "Activate")
    assert e.value.hr == W.E_ACCESSDENIED and "E_ACCESSDENIED" in str(e.value)


def test_waveformat_build_and_parse():
    w = W.waveformat(Format(48000, 2, 16, False))
    assert (w.wFormatTag, w.nChannels, w.nSamplesPerSec, w.nBlockAlign, w.nAvgBytesPerSec, w.cbSize) == (1, 2, 48000, 4, 192000, 0)
    assert W.parse_waveformat(ctypes.addressof(w)) == Format(48000, 2, 16, False)
    f = W.waveformat(Format(16000, 1, 32, True))
    assert f.wFormatTag == 3 and W.parse_waveformat(ctypes.addressof(f)).is_float
    # WAVEFORMATEXTENSIBLE float, as GetMixFormat returns it
    raw = struct.pack("<HHIIHHH", 0xFFFE, 2, 48000, 384000, 8, 32, 22) + struct.pack("<HI", 32, 3) + bytes(W.KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)
    buf = ctypes.create_string_buffer(raw, len(raw))
    assert W.parse_waveformat(ctypes.addressof(buf)) == Format(48000, 2, 32, True)


def test_py_com_object_query_interface_through_the_vtable():
    obj = W.PyComObject([W.IID_IAgileObject], [])
    p = W.ComPtr(obj.addr)
    with p.query(W.IID_IAgileObject) as agile:
        assert agile.addr == obj.addr and obj.refs == 2
    assert obj.refs == 1
    with pytest.raises(W.ComError) as e:
        p.query(W.IID_IAudioClient)
    assert e.value.hr == W.E_NOINTERFACE
    assert p.add_ref() == 2 and p.release() == 1 and not p


def test_activation_handler_completes_through_the_vtable():
    """Windows calls ActivateCompleted(op) on its own thread; the handler asks op for the result and signals."""
    h = W.ActivationHandler()
    client = FakeAudioClient()
    op = FakeOperation(W.S_OK, client.com.addr)
    # the handler must answer QueryInterface for the completion-handler IID and IAgileObject (else E_ILLEGAL_METHOD_CALL)
    for iid in (W.IID_IActivateAudioInterfaceCompletionHandler, W.IID_IAgileObject, W.IID_IUnknown):
        W.ComPtr(h.com.addr).query(iid).release()
    t = threading.Thread(target=lambda: W.vcall(h.com.addr, 3, W.HRESULT, (c_void_p,), op.com.addr))
    t.start()
    t.join(5)
    assert h.done.is_set() and h.hr == 0 and h.unk == client.com.addr


def test_activation_handler_failure_result():
    h = W.ActivationHandler()
    op = FakeOperation(W.E_ACCESSDENIED, None)
    assert W.vcall(h.com.addr, 3, W.HRESULT, (c_void_p,), op.com.addr) == 0
    assert h.hr == W.E_ACCESSDENIED and h.unk == 0


def test_open_tries_formats_in_order_with_fresh_clients():
    made = []

    def make():
        c = FakeAudioClient(accept={Format(48000, 2, 16, False)})
        made.append(c)
        return W.ComPtr(c.com.addr, "IAudioClient")

    stream, tried = W.CaptureStream.open(make, W.LOOPBACK_FORMATS, loopback=True)
    assert stream.fmt == Format(48000, 2, 16, False)
    assert [t[0] for t in tried] == [str(W.LOOPBACK_FORMATS[0]), str(W.LOOPBACK_FORMATS[1])]
    assert tried[0][1] == "0x88890008" and tried[1][1] == "0x00000000"
    assert len(made) == 2 and made[0].com.refs == 0          # the rejected client was released
    init = made[1].inits[0]
    assert init["mode"] == W.AUDCLNT_SHAREMODE_SHARED and init["period"] == 0 and init["buffer"] == W.BUFFER_HNS
    want = W.AUDCLNT_STREAMFLAGS_LOOPBACK | W.AUDCLNT_STREAMFLAGS_EVENTCALLBACK | W.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM \
        | W.AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
    assert init["flags"] == want and init["align"] == 4 and init["avg"] == 192000 and init["cb"] == 0
    stream.close()
    assert made[1].stopped


def test_open_stops_early_on_device_errors():
    def make():
        return W.ComPtr(FakeAudioClient(init_hr=W.AUDCLNT_E_DEVICE_INVALIDATED).com.addr)
    with pytest.raises(W.ComError) as e:
        W.CaptureStream.open(make, W.MIC_FORMATS, loopback=False)
    assert e.value.hr == W.AUDCLNT_E_DEVICE_INVALIDATED


def test_start_and_drain_packets():
    sig = (np.arange(480 * 2, dtype=np.int16)).tobytes()
    cap = FakeCaptureClient([
        (sig, 480, 0, 0),
        (b"", 480, W.AUDCLNT_BUFFERFLAGS_SILENT, 0),
        (sig, 480, W.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, 0),
    ])
    client = FakeAudioClient(accept={Format(48000, 2, 16, False)}, capture=cap)
    s = W.CaptureStream(W.ComPtr(client.com.addr), Format(48000, 2, 16, False), clock=lambda: 50.0)
    s.start()
    assert client.started and client.event != "unset"
    pk = s.drain()
    assert [p.frames for p in pk] == [480, 480, 480]
    assert pk[0].data == sig and not pk[0].silent
    assert pk[1].silent and pk[1].data == b""
    assert pk[2].flags & W.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY
    assert pk[0].t == pytest.approx(50.0 - 0.01)              # no device timestamp: now - duration
    assert cap.released == [480, 480, 480]
    assert s.drain() == []
    s.close()
    assert client.stopped and client.com.refs == 0 and cap.com.refs == 1      # only the fake's own reference left


def test_drain_uses_sane_device_timestamps_and_raises_on_unplug():
    cap = FakeCaptureClient([(b"\0" * 64, 16, 0, int(49.5 * 1e7)), (b"\0" * 64, 16, 0, int(10 * 1e7))], error_after=2)
    s = W.CaptureStream(W.ComPtr(FakeAudioClient(capture=cap).com.addr), Format(16000, 1, 32, True), clock=lambda: 50.0)
    s.start()
    with pytest.raises(W.ComError) as e:
        s.drain()
    assert e.value.hr == W.AUDCLNT_E_DEVICE_INVALIDATED
    cap2 = FakeCaptureClient([(b"\0" * 64, 16, 0, int(49.5 * 1e7)), (b"\0" * 64, 16, 0, int(10 * 1e7))])
    s2 = W.CaptureStream(W.ComPtr(FakeAudioClient(capture=cap2).com.addr), Format(16000, 1, 32, True), clock=lambda: 50.0)
    s2.start()
    a, b = s2.drain()
    assert a.t == pytest.approx(49.5)                           # the device's QPC timestamp
    assert b.t == pytest.approx(50.0 - 0.001)                   # 40 s old timestamp is not plausible: fallback


def test_device_enumerator_calls():
    mic = FakeDevice("{0.0.1.00000000}.{aaaa}", "מיקרופון (USB)", lambda: FakeAudioClient(mix=Format(48000, 2, 32, True)))
    other = FakeDevice("{0.0.1.00000000}.{bbbb}", "Headset", lambda: FakeAudioClient())
    fe = FakeEnumerator([mic, other], default=mic.id)
    e = fe.as_enumerator()
    assert e.default_id() == mic.id
    assert fe.calls[-1] == ("default", W.E_CAPTURE, W.ROLE_CONSOLE)
    assert e.endpoints() == [(mic.id, "מיקרופון (USB)"), (other.id, "Headset")]
    with e.device(other.id) as d:
        assert W.device_id(d) == other.id and W.friendly_name(d) == "Headset"
        with W.activate_audio_client(d) as c:
            assert c and W.get_mix_format(c) is None
    with e.device(mic.id) as d, W.activate_audio_client(d) as c:
        assert W.get_mix_format(c) == Format(48000, 2, 32, True)
    fe.default = None
    assert e.default_id() is None and e.default() is None
    with pytest.raises(W.ComError):
        e.device("missing")


def test_activate_denied_raises():
    dev = FakeDevice("x", "x", FakeAudioClient, activate_hr=W.E_ACCESSDENIED)
    with pytest.raises(W.ComError) as e:
        W.activate_audio_client(W.ComPtr(dev.com.addr))
    assert e.value.hr == W.E_ACCESSDENIED


def test_null_pointer_call_is_an_error_not_a_crash():
    with pytest.raises(W.ComError):
        W.vcall(0, 2, W.HRESULT, ())
    assert W.ComPtr(0).release() == 0
