"""ui/recorder.py: the real capture process (fake WAV sources) piped into the real worker (fake ASR, fake claude),
driven like the island drives it: start, events on the card, stop and save, two-step cancel, disk checks."""
import json
import os
import time
import wave

import numpy as np
import pytest

from tests.conftest import FIXTURES, wait_until
from timmy.winsys import jobs

pytestmark = pytest.mark.ui
SR = 16000


def fixture(name):
    with wave.open(str(FIXTURES / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16)


def write_wav(path, total_s, items):
    out = np.zeros(int(total_s * SR), np.int16)
    for t, x in items:
        out[int(t * SR):int(t * SR) + len(x)] = x
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(out.tobytes())


@pytest.fixture
def call_env(tmp_path, app_dir, fake_claude, monkeypatch):
    rec = tmp_path / "טימי - הקלטות"
    (app_dir / "config.json").write_text(json.dumps({"output_dir": str(rec), "owner_name": "נועה"}, ensure_ascii=False), encoding="utf-8")
    ch0, ch1 = tmp_path / "chrome.wav", tmp_path / "mic.wav"
    write_wav(ch0, 12, [(0.5, fixture("he_short.wav"))])
    write_wav(ch1, 12, [(7.5, fixture("he_reply.wav"))])
    monkeypatch.setenv("TIMMY_CAPTURE_ARGS", json.dumps(["--fake-chrome", str(ch0), "--fake-mic", str(ch1)]))
    monkeypatch.setenv("TIMMY_ASR_BACKEND", "fake")
    monkeypatch.setenv("TIMMY_ASR_PROFILE", "fast")
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    monkeypatch.delenv("TIMMY_FAKE_FREE_MB", raising=False)
    return rec


def new_recorder():
    from timmy.ui.recorder import Recorder
    return Recorder()


def test_full_call_saved(qapp, call_env, app_dir):
    r = new_recorder()
    events = []
    r.changed.connect(lambda: events.append((r.phase, len(r.lines), r.loading)))
    assert r.start() is True
    assert r.phase == "recording" and r.loading and os.path.basename(r.base).endswith(" כרום")
    assert wait_until(qapp, lambda: not r.loading, 60), "worker never became ready"
    assert wait_until(qapp, lambda: len(r.lines) >= 2, 60), r.lines
    assert [ln["speaker"] for ln in r.lines[:2]] == ["other", "me"]
    assert r.level > 0 or any(True for _ in events)
    assert r.elapsed >= 7
    r.stop()
    assert r.phase == "finishing"
    assert wait_until(qapp, lambda: r.phase == "message", 90), (r.phase, r.message)
    assert r.message == "נשמר ✓"
    names = sorted(os.listdir(call_env))
    md = [n for n in names if n.endswith(".md")]
    assert len(md) == 1 and not any(n.endswith(".wav") for n in names), names
    assert any(n.endswith(" - תמלול.txt") for n in names)
    log = (app_dir / "dry-open.log").read_text(encoding="utf-8")
    assert "reveal: " + str(call_env / md[0]) in log
    assert r.done_file == str(call_env / md[0])
    assert wait_until(qapp, lambda: r.phase == "idle", 5)


def test_cancel_two_steps_kills_and_deletes(qapp, call_env):
    r = new_recorder()
    assert r.start()
    assert wait_until(qapp, lambda: not r.loading, 60)
    cap, wrk, base = r._capture, r._worker, r.base
    r.cancel_tapped()
    assert r.cancel_armed and r.phase == "recording"
    r.cancel_tapped()                                   # a fast double click does not delete
    assert r.phase == "recording"
    wait_until(qapp, lambda: False, 0.6)
    r.cancel_tapped()
    assert r.phase == "message" and r.message == "בוטל"
    assert wait_until(qapp, lambda: cap.poll() is not None and wrk.poll() is not None, 10)
    assert not jobs.pid_alive(cap.pid) and not jobs.pid_alive(wrk.pid)
    wait_until(qapp, lambda: False, 2.5)                # the second cleanup pass
    left = [n for n in os.listdir(call_env) if n.startswith(os.path.basename(base))] if call_env.exists() else []
    assert left == []
    # a new call right away works and never reuses the cancelled base
    assert r.start() and r.base != base
    r.cancel()


def test_cancel_arm_expires(qapp, call_env):
    r = new_recorder()
    assert r.start()
    r.arm_cancel(seconds=0.3)
    assert r.cancel_armed
    assert wait_until(qapp, lambda: not r.cancel_armed, 3)
    r.stop()                                            # stop disarms too and saves (no speech yet: nothing kept)
    assert wait_until(qapp, lambda: r.phase in ("message", "idle"), 90)
    assert r.message in ("לא נקלט דיבור", "נשמר ✓")


def test_no_space_does_not_start(qapp, call_env, monkeypatch):
    monkeypatch.setenv("TIMMY_FAKE_FREE_MB", "100")
    r = new_recorder()
    asked = []
    r.need_folder.connect(lambda: asked.append(1))
    assert r.start() is False
    assert r.phase == "message" and r.message == "אין מקום" and asked
    assert not call_env.exists() or not os.listdir(call_env)


def test_disk_runs_out_mid_call(qapp, call_env, monkeypatch):
    r = new_recorder()
    assert r.start()
    assert wait_until(qapp, lambda: len(r.lines) >= 1, 60)
    monkeypatch.setenv("TIMMY_FAKE_FREE_MB", "150")
    r._start = time.monotonic() - 20                    # the 20 s check is due
    r._on_tick()
    assert r.phase == "finishing" and r.stopped_for_disk
    assert wait_until(qapp, lambda: r.phase == "message", 90)
    assert r.message == "נשמר, אין מקום"


def test_status_events_become_notices(qapp, call_env):
    r = new_recorder()
    r.phase = "recording"
    g = r._gen
    r._on_status(g, {"event": "mic", "state": "open", "name": "Realtek"})
    assert r.notice is None                             # the first mic at start is not news
    r._on_status(g, {"event": "mic", "state": "switched", "name": "אוזניות"})
    assert r.notice == "המיקרופון עבר ל: אוזניות"
    r._on_status(g, {"event": "mic", "state": "lost"})
    assert r.notice == "אין מיקרופון, ממשיך להקליט את הצד השני"
    r._on_status(g, {"event": "chrome", "state": "closed"})
    assert r.notice.startswith("כרום סגור")
    r._on_status(g, {"event": "levels", "chrome": 0.8, "mic": 0.1})
    assert r.level == pytest.approx(0.8)
    r._on_status(g - 1, {"event": "mic", "state": "switched", "name": "x"})     # an old call's process: ignored
    assert "x" not in (r.notice or "")


def test_worker_events_drive_the_card(qapp):
    r = new_recorder()
    r.phase = "recording"
    g = r._gen
    r._on_event(g, {"type": "ready"})
    r._on_event(g, {"type": "partial", "speaker": "other", "text": "שלום"})
    assert r.partial == "שלום" and not r.thinking
    r._on_event(g, {"type": "partial", "speaker": "me", "thinking": True})
    assert r.partial == "שלום"                          # mic noise never replaces the other side's live text
    r._on_event(g, {"type": "final", "speaker": "other", "t": "00:00:01", "text": "שלום לך"})
    assert r.partial == "" and r.lines[-1]["text"] == "שלום לך"
    r._on_event(g, {"type": "partial", "speaker": "me", "thinking": True})
    assert r.thinking and r.partial_speaker == "me"
    r._on_event(g, {"type": "bullets", "items": ["א", "ב"]})
    assert r.bullets == ["א", "ב"]
    r._on_event(g + 5, {"type": "final", "speaker": "me", "t": "0", "text": "זר"})
    assert r.lines[-1]["text"] == "שלום לך"


def test_delete_recording_files_only_touches_its_own(tmp_path):
    from timmy.ui.recorder import delete_recording_files
    base = tmp_path / "2026-11-21 10-00-00 כרום"
    for n in (".ch0.wav", ".ch1.wav", ".txt", " נקודות.md"):
        (tmp_path / (base.name + n)).write_text("x")
    (tmp_path / "2026-11-21 10-00-00-2 כרום.txt").write_text("keep")
    (tmp_path / "other.md").write_text("keep")
    assert delete_recording_files(str(base)) == []
    assert sorted(os.listdir(tmp_path)) == ["2026-11-21 10-00-00-2 כרום.txt", "other.md"]
    assert delete_recording_files(str(tmp_path / "short")) == []     # a too-short base never deletes anything
