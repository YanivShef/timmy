"""Drive `python -m timmy.engine.worker` like the UI does: frames on stdin (paced), JSON lines from stdout."""
import json
import os
import subprocess
import sys
import threading
import time
import wave

import numpy as np

from tests.conftest import FIXTURES, WIN
from timmy.engine import protocol as P

SR = 16000
FRAME = 1600     # 100 ms, what the capture process writes


def fixture_audio():
    with wave.open(str(FIXTURES / "he_short.wav")) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def silence(sec):
    return np.zeros(int(sec * SR), np.float32)


def track(*parts):
    return np.concatenate([np.asarray(p, np.float32) for p in parts])


def run_worker(base, ch0, ch1, env_extra=None, speed=8.0, timeout=600, args=None):
    """Feed both channels in 100 ms frames, `speed` x real time, close stdin, wait for exit.
    Returns (events, returncode, stderr text, wall seconds)."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(WIN), **(env_extra or {})}
    cmd = [sys.executable, "-m", "timmy.engine.worker"] + (args if args is not None else [str(base)])
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=WIN, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    events, err = [], []

    def read_out():
        for line in p.stdout:
            o = P.parse_event(line.decode("utf-8"))
            if o is not None:
                o["_at"] = round(time.time() - t0, 3)
                events.append(o)

    def read_err():
        err.append(p.stderr.read().decode("utf-8", "replace"))

    th = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in th:
        t.start()
    try:
        n = max(len(ch0), len(ch1))
        start = time.time()
        for i in range(0, n, FRAME):
            for ch, a in ((0, ch0), (1, ch1)):
                seg = a[i:i + FRAME]
                if len(seg):
                    p.stdin.write(P.encode_frame(ch, seg))
            p.stdin.flush()
            due = start + (i + FRAME) / SR / speed
            d = due - time.time()
            if d > 0:
                time.sleep(d)
        p.stdin.close()
    except BrokenPipeError:
        pass
    rc = p.wait(timeout=timeout)
    for t in th:
        t.join(10)
    return events, rc, "".join(err), time.time() - t0


def types(events):
    return [e["type"] for e in events]


def stderr_events(err):
    out = []
    for line in err.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out
