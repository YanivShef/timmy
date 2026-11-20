"""The capture process end to end (`python -m timmy.audio.capture`) with fake sources: frames, timing, stop paths.

Runs on every OS (the fake sources go through the same resampler / timeline / writer as WASAPI streams).
On the Windows runner it also exercises `--list` against the real process list and audio endpoints.
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

from tests.conftest import FIXTURES, WIN
from timmy.engine import protocol as P

SR = 16000
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(WIN)}


def write_wav(path, rate, channels, mono, bits=16):
    x = np.repeat(np.asarray(mono, np.float64)[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(np.round(x * 32767).astype("<i2").tobytes())


def read_fixture():
    with wave.open(str(FIXTURES / "he_short.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def capture(*args, stdin=subprocess.DEVNULL, timeout=30):
    r = subprocess.run([sys.executable, "-m", "timmy.audio.capture", *args], cwd=WIN, env=ENV, stdin=stdin,
                       capture_output=True, timeout=timeout)
    events = [json.loads(line) for line in r.stderr.decode("utf-8").splitlines() if line.startswith("{")]
    frames = list(P.read_frames(io.BytesIO(r.stdout)))
    return r, events, frames


def channel(frames, ch):
    parts = [a for c, a in frames if c == ch]
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def offset_of(sig, ref, max_lag):
    """Lag (samples) at which `ref` best matches the start of `sig`, and the normalized correlation there."""
    n = min(len(ref), SR)

    def corr(lag):
        seg = sig[lag:lag + n]
        if len(seg) < n:
            return -1.0
        return float(np.dot(seg, ref[:n]) / (np.linalg.norm(seg) * np.linalg.norm(ref[:n]) + 1e-9))
    coarse = max(range(0, max_lag, 8), key=corr)
    best = max(range(max(0, coarse - 8), coarse + 9), key=corr)
    return best, corr(best)


def test_fake_sources_end_to_end(tmp_path, out_dir):
    t = np.arange(int(4 * 48000)) / 48000
    write_wav(tmp_path / "chrome48k.wav", 48000, 2, 0.4 * np.sin(2 * np.pi * 1000 * t))
    r, ev, frames = capture("--fake-chrome", str(tmp_path / "chrome48k.wav"), "--fake-mic", str(FIXTURES / "he_short.wav"),
                            "--fake-mic-gap", "1.2:0.6", "--seconds", "2.5", "--ignore-stdin")
    (out_dir / "capture_e2e_events.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    kinds = [e["event"] for e in ev]
    assert kinds[0] == "start" and "ready" in kinds and "levels" in kinds and kinds[-1] == "stopped"
    assert ev[-1]["reason"] == "seconds"
    assert all(f[1].dtype == np.float32 for f in frames)
    ch0, ch1 = channel(frames, 0), channel(frames, 1)
    assert len(ch0) == len(ch1), "both channels must end on the same sample"
    assert 2.5 * SR <= len(ch0) < 3.0 * SR
    # Chrome: 48 kHz stereo tone resampled; level and pitch preserved, starts (almost) at t=0
    body = ch0[int(0.3 * SR): int(2.2 * SR)]
    assert abs(np.sqrt(np.mean(body ** 2)) - 0.4 / np.sqrt(2)) < 0.02
    spec = np.abs(np.fft.rfft(body[:16384] * np.hanning(16384)))
    assert abs(np.argmax(spec) * SR / 16384 - 1000) < 5
    first = int(np.flatnonzero(np.abs(ch0) > 0.05)[0])
    assert first < 0.15 * SR
    # mic: the fixture, nearly unshifted; the 0.6 s "unplugged" gap is silence and the audio after it is on time
    ref = read_fixture()
    lag, corr = offset_of(ch1, ref, int(0.2 * SR))
    assert corr > 0.99 and lag < 0.15 * SR, (lag, corr)       # a few ms locally; slack for a busy CI runner
    assert not ch1[lag + int(1.22 * SR): lag + int(1.78 * SR)].any()
    after = ch1[lag + int(1.85 * SR): lag + int(2.3 * SR)]
    assert np.allclose(after, ref[int(1.85 * SR): int(2.3 * SR)], atol=1e-4)
    levels = [e for e in ev if e["event"] == "levels"]
    assert max(e["chrome"] for e in levels) > 0.5 and max(e["mic"] for e in levels) > 0.3
    assert ev[-1]["late_samples"] == {"0": 0, "1": 0}


def _run_live(args, stop_how):
    p = subprocess.Popen([sys.executable, "-m", "timmy.audio.capture", *args], cwd=WIN, env=ENV,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = bytearray(), []
    ready = threading.Event()

    def read_out():
        while True:
            d = p.stdout.read(4096)
            if not d:
                return
            out.extend(d)

    def read_err():
        for line in p.stderr:
            line = line.decode("utf-8")
            err.append(line)
            if '"ready"' in line:
                ready.set()
    ts = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in ts:
        t.start()
    assert ready.wait(20), "".join(err)
    time.sleep(1.0)
    t_stop = time.monotonic()
    if stop_how == "stop":
        p.stdin.write(b"stop\n")
        p.stdin.flush()
    else:
        p.stdin.close()
    rc = p.wait(10)
    took = time.monotonic() - t_stop
    for t in ts:
        t.join(5)
    events = [json.loads(e) for e in err if e.startswith("{")]
    return rc, took, events, list(P.read_frames(io.BytesIO(bytes(out))))


@pytest.mark.parametrize("how", ["stop", "eof"])
def test_stop_by_stdin(how):
    rc, took, ev, frames = _run_live(["--fake-mic", str(FIXTURES / "he_short.wav"), "--no-chrome"], how)
    assert rc == 0 and took < 5
    assert ev[-1]["event"] == "stopped" and ev[-1]["reason"] == "stop"
    ch1 = channel(frames, 1)
    assert 1.0 * SR <= len(ch1) < 4.0 * SR
    assert not channel(frames, 0).size


def test_exits_when_the_reader_goes_away():
    p = subprocess.Popen([sys.executable, "-m", "timmy.audio.capture", "--fake-mic", str(FIXTURES / "he_short.wav"),
                          "--ignore-stdin"], cwd=WIN, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    p.stdout.read(20000)
    p.stdout.close()
    assert p.wait(10) == 0


def test_mic_test_mode_has_only_the_mic_channel():
    r, ev, frames = capture("--mic-test", "--fake-mic", str(FIXTURES / "he_short.wav"), "--seconds", "1", "--ignore-stdin")
    assert r.returncode == 0
    assert {c for c, _ in frames} == {1}
    assert ev[0]["channels"] == [1]


def test_list_reports_environment():
    r = subprocess.run([sys.executable, "-m", "timmy.audio.capture", "--list"], cwd=WIN, env=ENV, capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout.decode("utf-8"))
    assert rep["process"] == "chrome.exe" and isinstance(rep["chrome_roots"], list)
    if sys.platform == "win32":
        print("capture --list:", rep)
        assert rep["loopback_supported"] is True
        assert "inputs" in rep and (rep["inputs"] is not None or "inputs_error" in rep)


def test_bad_arguments_exit_2():
    r = subprocess.run([sys.executable, "-m", "timmy.audio.capture", "--seconds", "x"], cwd=WIN, env=ENV, capture_output=True, timeout=30)
    assert r.returncode == 2
