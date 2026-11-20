"""Free-space checks, the same rules and numbers as the Mac app (Recorder.swift freeBytes / minStartBytes / minKeepBytes).

* less than 500 MB free on the recordings drive: a call does not start (an hour of call is about 250 MB while
  recording: two 16 kHz WAV channels, the mp3 is made at the end);
* less than 200 MB during a call (checked every 20 s by the UI): stop and save what there is ("נשמר, אין מקום").
The worker itself also reports {"type": "diskfull"} when a write fails, exactly like mac/live.py.
TIMMY_FAKE_FREE_MB simulates an almost full disk in tests (same variable as the Mac app).
"""
import os
import shutil

MB = 1_000_000                 # decimal, as on the Mac
MIN_START_BYTES = 500 * MB
MIN_KEEP_BYTES = 200 * MB
CHECK_EVERY_S = 20


def existing_parent(path) -> str:
    p = os.path.abspath(os.path.expanduser(str(path or "~")))
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def free_bytes(path) -> int:
    fake = os.environ.get("TIMMY_FAKE_FREE_MB")
    if fake:
        try:
            return int(float(fake) * MB)
        except ValueError:
            pass
    try:
        return int(shutil.disk_usage(existing_parent(path)).free)
    except OSError:
        return 2 ** 62           # unknown: never block a call because a check failed (Mac: Int64.max)


def can_start(path) -> bool:
    return free_bytes(path) >= MIN_START_BYTES


def must_stop(path) -> bool:
    return free_bytes(path) < MIN_KEEP_BYTES
