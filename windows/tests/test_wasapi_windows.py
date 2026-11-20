"""The real WASAPI paths on a Windows host (the CI runner, or a dev PC).

GitHub's windows-latest runners have no sound card, and the Windows Audio service may be stopped; every test
therefore asserts what must always hold (COM works, calls return HRESULTs instead of crashing, the capture process
keeps a correct timeline without devices) and skips the parts that need an endpoint, printing why. On a real PC
with speakers the last test proves Chrome-only capture end to end: a child process plays a tone, process loopback
on that child's PID records it.
"""
import io
import json
import os
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import pytest

from tests.conftest import WIN
from timmy.audio import wasapi as W

pytestmark = [pytest.mark.windows_only, pytest.mark.audio]
SR = 16000
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(WIN)}
# HRESULTs that only mean "this machine has no audio" (runner without sound card / audio service)
NO_AUDIO = {W.AUDCLNT_E_SERVICE_NOT_RUNNING, W.E_NOTFOUND, W.AUDCLNT_E_ENDPOINT_CREATE_FAILED, W.AUDCLNT_E_DEVICE_INVALIDATED}


def in_mta(fn):
    """Run fn() on a fresh MTA thread (like the capture process threads); re-raise its exception."""
    box = {}

    def run():
        need = W.com_init()
        try:
            box["v"] = fn()
        except BaseException as e:      # noqa: BLE001 - re-raised in the caller
            box["e"] = e
        finally:
            if need:
                W.com_uninit()
    t = threading.Thread(target=run)
    t.start()
    t.join(30)
    assert not t.is_alive(), "COM call hung"
    if "e" in box:
        raise box["e"]
    return box.get("v")


def audio_service_state():
    r = subprocess.run(["sc", "query", "Audiosrv"], capture_output=True, text=True)
    return "RUNNING" if "RUNNING" in r.stdout else (r.stdout.strip().splitlines() or ["unknown"])[-1]


def test_com_and_device_enumerator():
    def probe():
        e = W.DeviceEnumerator()
        try:
            return {"capture": e.endpoints(W.E_CAPTURE), "render": e.endpoints(W.E_RENDER),
                    "default_mic": e.default_id(W.E_CAPTURE), "default_out": e.default_id(W.E_RENDER)}
        finally:
            e.release()
    info = in_mta(probe)
    print("Audiosrv:", audio_service_state(), "endpoints:", json.dumps(info, ensure_ascii=False))
    assert isinstance(info["capture"], list) and isinstance(info["render"], list)
    if info["default_mic"]:
        assert info["default_mic"] in [i for i, _ in info["capture"]]


def test_process_loopback_activation_on_own_process():
    """ActivateAudioInterfaceAsync with the 12-byte blob and our agile handler: an IAudioClient, or a clean HRESULT."""
    def act():
        try:
            client = W.activate_process_loopback(os.getpid())
        except W.ComError as e:
            return ("error", e.hr)
        try:
            stream, tried = W.CaptureStream.open(lambda: W.activate_process_loopback(os.getpid()), W.LOOPBACK_FORMATS, loopback=True)
        except W.ComError as e:
            client.release()
            return ("init-error", e.hr)
        client.release()
        try:
            stream.start()
            packets = []
            end = time.monotonic() + 0.5
            while time.monotonic() < end:
                stream.wait(100)
                packets += stream.drain()
            return ("ok", str(stream.fmt), tried, len(packets))
        finally:
            stream.close()
    res = in_mta(act)
    print("process loopback on own pid:", res, "Audiosrv:", audio_service_state())
    if res[0] != "ok":
        hr = res[1]
        assert hr not in (W.E_ILLEGAL_METHOD_CALL, W.E_NOINTERFACE, W.E_POINTER), f"our activation call is wrong: {W.hr_name(hr)}"
        if hr in NO_AUDIO or hr == W.E_FAIL:
            pytest.skip(f"no audio on this machine: {W.hr_name(hr)} ({W.hr_hex(hr)})")
        pytest.fail(f"process loopback failed: {res[0]} {W.hr_name(hr)} ({W.hr_hex(hr)})")
    assert res[1] in [str(f) for f in W.LOOPBACK_FORMATS], res   # ideally the first: 16 kHz mono float via AUTOCONVERTPCM


def test_mic_source_reports_a_state():
    from timmy.audio.mic import MicSource
    from timmy.audio.padder import Timeline, clock
    ev, stop, tl = [], threading.Event(), Timeline()
    m = MicSource(tl, clock(), ev.append, stop)
    m.start()
    time.sleep(1.5)
    stop.set()
    m.join(5)
    print("mic events:", ev)
    assert not m.is_alive()
    states = [e["state"] for e in ev if e["event"] == "mic"]
    assert states and states[0] in ("open", "none", "denied", "error")
    if states[0] == "open":
        assert tl.end > 0.5 * SR


def test_capture_process_real_mode_keeps_the_timeline():
    """Real ChromeWatcher (on a child PID) + real MicSource, whatever the devices: 2 s in, 2 s of both channels out."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        r = subprocess.run([sys.executable, "-m", "timmy.audio.capture", "--pid", str(child.pid), "--seconds", "2", "--ignore-stdin"],
                           cwd=WIN, env=ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    finally:
        child.kill()
    err = r.stderr.decode("utf-8", "replace")
    print(err)
    assert r.returncode == 0, err
    ev = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert ev[-1]["event"] == "stopped"
    from timmy.engine import protocol as P
    frames = list(P.read_frames(io.BytesIO(r.stdout)))
    n0 = sum(len(a) for c, a in frames if c == 0)
    n1 = sum(len(a) for c, a in frames if c == 1)
    assert n0 == n1 and 2 * SR <= n0 < 2.6 * SR


def _tone_wav(path, seconds=6.0, freq=1000.0):
    t = np.arange(int(48000 * seconds)) / 48000
    x = np.round(0.5 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(x.tobytes())


def test_loopback_records_only_the_target_process(tmp_path):
    """A child plays a tone; process loopback on the child's PID must hear it (needs a speaker endpoint)."""
    def render_endpoints():
        e = W.DeviceEnumerator()
        try:
            return e.endpoints(W.E_RENDER)
        finally:
            e.release()
    if not in_mta(render_endpoints):
        pytest.skip("no speaker / render endpoint on this machine")
    wav = tmp_path / "tone.wav"
    _tone_wav(wav)
    player = subprocess.Popen([sys.executable, "-c",
                               "import sys, time, winsound; winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME | winsound.SND_ASYNC); time.sleep(8)",
                               str(wav)])
    try:
        time.sleep(0.5)
        r = subprocess.run([sys.executable, "-m", "timmy.audio.capture", "--pid", str(player.pid), "--no-mic", "--seconds", "3",
                            "--ignore-stdin"], cwd=WIN, env=ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    finally:
        player.kill()
    from timmy.engine import protocol as P
    ch0 = np.concatenate([a for c, a in P.read_frames(io.BytesIO(r.stdout)) if c == 0])
    print(r.stderr.decode("utf-8", "replace"))
    rms = float(np.sqrt(np.mean(ch0[SR // 2:] ** 2)))
    assert rms > 0.05, f"the tone the child played was not captured (rms {rms:.4f})"
    spec = np.abs(np.fft.rfft(ch0[SR // 2: SR // 2 + 16384] * np.hanning(16384)))
    assert abs(np.argmax(spec) * SR / 16384 - 1000) < 10
