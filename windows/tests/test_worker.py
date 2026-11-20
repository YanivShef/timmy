"""engine.worker: the mac/live.py port.

* parity: mac/live.py's own Chan / is_echo code (extracted with ast) and the port get the same audio and must emit
  the same events and lines (fast profile = the Mac behaviour);
* a guard: the hand-ported parts of mac/live.py are fingerprinted, so a Mac change fails here until it is ported;
* the cpu profile (queue + merge) loses nothing and keeps lines in time order;
* end to end through `python -m timmy.engine.worker` with a fake ASR backend and a fake claude: events, files,
  names, summary, key points, restyle, meetings copy, echo, no speech, UTF-8 output, cancel = kill the tree;
* the same with faster-whisper: the tiny model (plumbing) and the real ivrit.ai model (slow, the asr-real job).
"""
import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import types as _types

import numpy as np
import pytest

from tests import worker_feed as WF
from tests.conftest import FIXTURES, ROOT, WIN, claude_calls
from timmy.engine import asr as A
from timmy.engine import worker as W
from timmy.engine._mac import live_shared as L
from timmy.winsys import jobs

SR = 16000
MAC_LIVE = ROOT / "mac" / "live.py"


# ---------------------------------------------------------------- helpers

def noise_track(spec, seed=1, amp=0.08):
    """spec = [(start_s, dur_s)] of 'speech' (noise bursts); returns float32 at 16 kHz."""
    rng = np.random.default_rng(seed)
    end = max(s + d for s, d in spec) + 1.0
    a = np.zeros(int(end * SR), np.float32)
    for s, d in spec:
        i = int(s * SR)
        a[i:i + int(d * SR)] = (amp * rng.standard_normal(int(d * SR))).astype(np.float32)
    return a


VOCAB = ("תפוח", "רכבת", "מחשב", "גשם", "ספר", "חלון", "כביש", "דג", "שולחן", "עיפרון", "ים", "הר", "כוס", "מנורה",
         "צבע", "פרח", "קיר", "שמש", "ענן", "דלת", "גזר", "נמל", "קפה", "שעון", "מפה", "סוס", "בית", "עט", "ירח", "נהר")


class LenASR:
    """Deterministic backend: one segment per stretch of sound, with words drawn from a seeded generator (the n-th
    segment returned always has the same words), so different utterances never look alike to the text echo check."""
    name, device, compute_type, threads = "len", "cpu", "none", 1

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay
        self.n = 0

    def segments(self, a, partial=False, timestamps=False):
        import random
        self.calls.append((len(a), partial, timestamps))
        if self.delay:
            time.sleep(self.delay)
        out = []
        for s in A.FakeASR(text="x").segments(a, timestamps=True):
            self.n += 1
            out.append(dict(s, text=" " + " ".join(random.Random(self.n).sample(VOCAB, 6))))
        if not timestamps and len(out) > 1:
            out = [dict(out[0], end=out[-1]["end"], text=" ".join(x["text"].strip() for x in out))]
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def chunks_of(a, n=4000):          # 0.25 s per main-loop tick, like the Mac loop with a steady capture
    return [a[i:i + n] for i in range(0, len(a), n)]


def drive(step, ch0, ch1, clock):
    c0, c1 = chunks_of(ch0), chunks_of(ch1)
    n = max(len(c0), len(c1))
    for i in range(n + 1):
        clock.sleep(0.25)
        new = {0: [c0[i]] if i < len(c0) else [], 1: [c1[i]] if i < len(c1) else []}
        step(new, i == n)


def mac_namespace(asr, clock, emitted):
    """mac/live.py's Chan, is_echo and the pieces they use, executed from the Mac source."""
    src = MAC_LIVE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"Chan", "is_echo"}
    code = "\n".join("\n".join(src.splitlines()[n.lineno - 1:n.end_lineno]) for n in tree.body
                     if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in want)
    ns = {"np": np, "SR": SR, "FR": L.FR, "TH": L.TH, "speech_frames": L.speech_frames, "time": clock,
          "echo_score": L.echo_score, "final_ok": L.final_ok, "_norm": L._norm, "entries": [], "finals": [],
          "txt": io.StringIO(), "NAMES": {0: "צד שני", 1: "אני"}, "ts": L.ts, "emit": emitted.append,
          "difflib": __import__("difflib"),
          "tr": lambda a, partial=False: A.segments_info(asr.segments(a, partial=partial), partial=partial)}
    exec(code, ns)
    return ns


def reset_echo():
    L.CH0_HIST["buf"] = np.zeros(0, np.float32)
    L.CH0_HIST["start"] = 0


@pytest.fixture
def cfg_default():
    L.configure({})
    yield {}
    L.configure({})


# ---------------------------------------------------------------- parity with mac/live.py

def call_audio():
    ch0 = noise_track([(0.5, 3.0), (5.0, 0.4), (7.0, 16.5), (25.0, 1.2), (27.0, 2.5)], seed=3)
    ch1 = noise_track([(4.0, 2.2), (12.0, 0.6), (13.0, 4.0), (20.0, 3.0)], seed=4, amp=0.1)
    n = max(len(ch0), len(ch1)) + SR
    ch0 = np.pad(ch0, (0, n - len(ch0)))
    ch1 = np.pad(ch1, (0, n - len(ch1)))
    # an echo of the other side on the mic (speakers, no headphones): 120 ms late, quieter
    d = int(0.12 * SR)
    ch1[d + 24 * SR:d + 30 * SR] += 0.5 * ch0[24 * SR:30 * SR]
    return ch0, ch1


def test_fast_profile_is_the_mac_code(tmp_path, cfg_default, monkeypatch):
    ch0, ch1 = call_audio()
    # 1) mac/live.py's own code
    clock = Clock()
    mac_events = []
    reset_echo()
    ns = mac_namespace(LenASR(), clock, mac_events)
    chans = {0: ns["Chan"](0), 1: ns["Chan"](1)}

    def mac_step(new, done):
        L.ch0_append(new[0])
        for c in (0, 1):
            chans[c].step(new[c], done)
    drive(mac_step, ch0, ch1, clock)

    # 2) the port
    clock2 = Clock()
    monkeypatch.setattr(W, "time", _types.SimpleNamespace(time=clock2.time, sleep=clock2.sleep, perf_counter=time.perf_counter))
    ev = W.Emitter(io.StringIO())
    s = W.Session(str(tmp_path / "2026-11-20 10-00-00 כרום"), {}, ev, asr=LenASR(), profile="fast")
    s.begin(bullets=False)
    drive(s.step_all, ch0, ch1, clock2)
    s.txt.close()

    assert ev.events == mac_events
    assert [(round(a, 3), b, c) for a, b, c in s.entries] == [(round(a, 3), b, c) for a, b, c in ns["entries"]]
    assert s.finals == ns["finals"]
    assert (tmp_path / "2026-11-20 10-00-00 כרום.txt").read_text(encoding="utf-8") == ns["txt"].getvalue()
    # the scenario really exercised the interesting paths
    kinds = {(e["type"], bool(e.get("text")), bool(e.get("thinking"))) for e in mac_events}
    assert ("partial", True, False) in kinds          # live text
    assert ("partial", False, True) in kinds          # thinking
    assert sum(1 for e in mac_events if e["type"] == "final") >= 6
    assert not any(e["type"] == "final" and e["speaker"] == "me" and e["t"] >= "00:00:24" for e in mac_events), \
        "the echo on the mic became a line"


def test_cpu_profile_merges_without_losing_lines(tmp_path, cfg_default):
    """A slow backend: utterances queue up, get merged (timestamps on), and every one still becomes its own line."""
    ch0, ch1 = call_audio()
    fast = W.Session(str(tmp_path / "a"), {}, W.Emitter(io.StringIO()), asr=LenASR(), profile="fast")
    fast.begin(bullets=False)
    clock = Clock()
    drive(fast.step_all, ch0, ch1, clock)
    fast.txt.close()

    slow_asr = LenASR(delay=0.05)
    ev = W.Emitter(io.StringIO())
    cpu = W.Session(str(tmp_path / "b"), {}, ev, asr=slow_asr, profile="cpu")
    cpu.begin(bullets=False)
    drive(cpu.step_all, ch0, ch1, Clock())          # feeding is instant, so the ASR thread falls behind
    with cpu.qcv:
        cpu.q_closed = True
        cpu.qcv.notify_all()
    cpu.asr_thread.join(30)
    cpu.txt.close()

    assert any(ts for _n, _p, ts in slow_asr.calls), "nothing was merged"
    assert not any(p for _n, p, _ts in slow_asr.calls), "the cpu profile must not run text partials"
    assert not any(e["type"] == "partial" and e.get("text") for e in ev.events)
    # the same lines at the same times (the words differ: the fake backend numbers its segments in call order)
    assert sorted((round(a, 2), b) for a, b, _ in cpu.entries) == sorted((round(a, 2), b) for a, b, _ in fast.entries)
    secs = [e[0] for e in cpu.entries if e[1] == 0]
    assert secs == sorted(secs)                               # per channel in time order


class SkippingASR(LenASR):
    """Like Whisper on windows-latest: in a merged call (timestamps on) it returns nothing for the last piece."""

    def segments(self, a, partial=False, timestamps=False):
        out = super().segments(a, partial, timestamps)
        return out[:-1] if timestamps and len(out) > 1 else out


def test_speech_runs_split_trim_and_skip_silence():
    a = noise_track([(1.0, 2.0), (6.0, 1.5)], seed=5)          # 2 s, 3 s of silence, 1.5 s
    runs = W.speech_runs(a, L.TH[0])
    assert len(runs) == 2
    (s0, e0), (s1, e1) = runs
    assert abs(s0 / SR - 0.8) < 0.11 and abs(e0 / SR - 3.3) < 0.11 and abs(s1 / SR - 5.8) < 0.11
    assert W.speech_runs(np.zeros(3 * SR, np.float32), L.TH[0]) == []
    short_gap = noise_track([(1.0, 1.0), (2.3, 1.0)], seed=6)   # 0.3 s pause: one utterance
    assert len(W.speech_runs(short_gap, L.TH[0])) == 1


def test_cpu_profile_backlog_and_skipped_pieces_lose_nothing(tmp_path, cfg_default):
    """The whole call arrives in one step (the model took longer to load than the call lasted), and the backend
    skips a piece of every merged call: still every utterance becomes its own line, at its own time."""
    ch0 = noise_track([(0.5, 3.0), (15.0, 3.0)], seed=7)
    ch1 = noise_track([(8.0, 2.5), (22.0, 2.5)], seed=8, amp=0.1)
    n = max(len(ch0), len(ch1))
    ch0, ch1 = np.pad(ch0, (0, n - len(ch0))), np.pad(ch1, (0, n - len(ch1)))
    reset_echo()
    asr = SkippingASR(delay=0.02)
    s = W.Session(str(tmp_path / "d"), {}, W.Emitter(io.StringIO()), asr=asr, profile="cpu")
    s.begin(bullets=False)
    s.step_all({0: [ch0], 1: [ch1]}, True)
    with s.qcv:
        s.q_closed = True
        s.qcv.notify_all()
    s.asr_thread.join(30)
    s.txt.close()
    assert any(ts for _n, _p, ts in asr.calls), "nothing was merged"
    got = sorted((int(sec), ch) for sec, ch, _ in s.entries)
    assert got == [(0, 0), (7, 1), (14, 0), (21, 1)], got


def test_cpu_profile_echo_check_happens_before_the_queue(tmp_path, cfg_default):
    ch0, ch1 = call_audio()
    s = W.Session(str(tmp_path / "c"), {}, W.Emitter(io.StringIO()), asr=LenASR(delay=0.02), profile="cpu")
    s.begin(bullets=False)
    drive(s.step_all, ch0, ch1, Clock())
    with s.qcv:
        s.q_closed = True
        s.qcv.notify_all()
    s.asr_thread.join(30)
    s.txt.close()
    assert not any(spk == 1 and sec >= 24 for sec, spk, _ in s.entries)


def mac_parts():
    src = MAC_LIVE.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in W.MAC_PORTED:
            out[n.name] = "\n".join(lines[n.lineno - 1:n.end_lineno])
        if isinstance(n, ast.If) and "--restyle" in ast.unparse(n.test):
            out["<restyle mode>"] = "\n".join(lines[n.lineno - 1:n.end_lineno])
        if isinstance(n, ast.While):
            out["<main loop>"] = "\n".join(lines[n.lineno - 1:n.end_lineno])
    start = next(i for i, ln in enumerate(lines) if ln.startswith("txt.close()"))
    out["<end of call>"] = "\n".join(lines[start:])
    return {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in out.items()}


def test_ported_mac_code_has_not_changed():
    """The hand-ported parts of mac/live.py are fingerprinted in worker.MAC_PORTED. When this fails, mac/live.py
    changed one of them: port the change into timmy/engine/worker.py (or asr.py), then update the hash."""
    got = mac_parts()
    assert set(got) == set(W.MAC_PORTED), sorted(set(got) ^ set(W.MAC_PORTED))
    changed = [k for k in got if got[k] != W.MAC_PORTED[k]]
    assert not changed, (f"mac/live.py changed {changed}: port the change to timmy/engine/worker.py, then update "
                         f"MAC_PORTED there with: {json.dumps({k: got[k] for k in changed})}")


# ---------------------------------------------------------------- pure helpers

def test_parse_summary():
    out = "עם: \"דנה כהן\"\nכותרת: **שיחת היכרות**\nסיכום: פתיחה.\nעוד שורה.\n\nמשימות:\n- לשלוח הצעה (אני)\n- אין\n- ...\n-"
    person, title, summary, tasks = W.parse_summary(out)
    assert (person, title) == ("דנה כהן", "שיחת היכרות")
    assert summary == "פתיחה.\n\nעוד שורה."
    assert tasks == ["לשלוח הצעה (אני)"]
    assert W.parse_summary("עם: לא ידוע\nכותרת: x")[0] == ""
    assert W.parse_summary("") == ("", "", "", [])


def test_parse_bullets():
    assert W.parse_bullets("הנה:\n- אחת\n• שתיים\n3) שלוש\n* ארבע\n-  \nטקסט") == ["אחת", "שתיים", "שלוש", "ארבע"]


def test_load_people_and_link(tmp_path):
    clients = tmp_path / "לקוחות"
    clients.mkdir()
    (clients / "דנה כהן - מעצבת.md").write_text("x", encoding="utf-8")
    (clients / "#תבנית.md").write_text("x", encoding="utf-8")
    (clients / "יוסי לוי").mkdir()
    pf = tmp_path / "people.txt"
    pf.write_text("רון\n\nמיכל\n", encoding="utf-8")
    src = tmp_path / "crm.json"
    src.write_text('{"name": "אורי"}, {"name": "רון"}', encoding="utf-8")
    cfg = {"people": ["שירה", " "], "people_file": str(pf), "clients_dir": str(clients),
           "people_sources": [{"path": str(src), "regex": r'"name": "([^"]+)"'}, {"path": "/nope", "regex": "x"}]}
    people, notes = W.load_people(cfg)
    assert people[:4] == ["שירה", "רון", "מיכל", "אורי"]
    assert "דנה כהן" in people and "יוסי לוי" in people and "#תבנית" not in people
    assert W.note_link("דנה כהן", notes, str(clients)) == "[[דנה כהן - מעצבת]]"
    assert W.note_link("גלעד", notes, str(clients)) == "[[גלעד]]"
    assert W.note_link("גלעד", notes, "") == "גלעד"
    assert W.note_link("", notes, str(clients)) == ""


def test_markdown_format_is_the_macs():
    md = W.build_markdown("כותרת", "כותרת", "2026-11-20 14-03-07", 125.9, "[[דנה]]", "סיכום.", ["נקודה"], ["משימה"],
                          os.path.join("x", "2026-11-20 14-03 דנה - כותרת"))
    assert md.split("\n") == [
        "# כותרת", "", "**תאריך:** 2026-11-20 14:03 · **אורך:** 2:05 דק׳ · **עם:** [[דנה]]", "",
        "## סיכום", "", "סיכום.", "", "## נקודות עיקריות", "", "- נקודה", "", "## משימות", "", "- [ ] משימה", "",
        "תמלול מלא: 2026-11-20 14-03 דנה - כותרת - תמלול.txt", ""]
    md2 = W.build_markdown("", "שיחה בכרום", "2026-11-20 14-03-07", 59, "", "", [], [], "2026-11-20 14-03 שיחה בכרום")
    assert md2.split("\n")[:3] == ["# שיחה בכרום", "", "**תאריך:** 2026-11-20 14:03 · **אורך:** 0:59 דק׳"]


# ---------------------------------------------------------------- end to end (subprocess)

def fixture(name):
    import wave
    with wave.open(str(FIXTURES / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def place(total, items):
    out = np.zeros(int(total * SR), np.float32)
    for t, x in items:
        out[int(t * SR):int(t * SR) + len(x)] += x
    return out


def two_speakers():
    a, r = fixture("he_short.wav"), fixture("he_reply.wav")
    return place(14, [(0.5, a)]), place(14, [(8.0, r)])


@pytest.fixture
def rec(tmp_path, app_dir, fake_claude):
    d = tmp_path / "טימי - הקלטות"
    d.mkdir()
    return d


def env_for(fake_claude, **kw):
    path, logf = fake_claude
    e = {"TIMMY_CLAUDE_SEARCH": str(path), "FAKE_CLAUDE_LOG": str(logf), "TIMMY_ASR_BACKEND": "fake"}
    e.update({k: str(v) for k, v in kw.items()})
    return e


def write_cfg(app_dir, **cfg):
    (app_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def ls(d):
    return sorted(p.name for p in d.iterdir())


@pytest.mark.parametrize("profile", ["fast", "cpu"])
def test_full_call(rec, app_dir, fake_claude, profile):
    write_cfg(app_dir, owner_name="נועה", summary_model="opus", bullets_model="sonnet", meetings_dir="")
    ch0, ch1 = two_speakers()
    base = rec / "2026-11-20 14-03-07 כרום"
    env = env_for(fake_claude, TIMMY_ASR_PROFILE=profile)
    events, rc, err, _ = WF.run_worker(base, ch0, ch1, env, speed=8)
    assert rc == 0, err
    t = WF.types(events)
    assert t[:2] == ["loading", "ready"]
    assert t[-1] == "done" and "saving" in t
    assert t.index("saving") > max(i for i, x in enumerate(t) if x == "final")
    finals = [e for e in events if e["type"] == "final"]
    assert [(e["speaker"], e["t"]) for e in finals] == [("other", "00:00:00"), ("me", "00:00:07")]
    if profile == "cpu":
        assert not any(e["type"] == "partial" and e.get("text") for e in events)
    asr_log = next(e for e in WF.stderr_events(err) if e.get("event") == "asr")
    assert asr_log["profile"] == profile and asr_log["claude"] is True

    stem = "2026-11-20 14-03 קביעת פגישה על הצעת מחיר"
    assert ls(rec) == sorted([stem + ".md", stem + ".mp3", stem + " - תמלול.txt"])
    done = events[-1]
    assert done["txt"] == str(rec / (stem + ".md")) and done["title"] == "קביעת פגישה על הצעת מחיר"
    md = (rec / (stem + ".md")).read_text(encoding="utf-8")
    assert md.startswith("# קביעת פגישה על הצעת מחיר\n\n**תאריך:** 2026-11-20 14:03 · **אורך:** 0:14 דק׳\n")
    assert "## סיכום" in md and "## נקודות עיקריות\n\n- הצד השני רוצה" in md and "- [ ] לשלוח זימון לפגישה (אני)" in md
    assert md.endswith(f"תמלול מלא: {stem} - תמלול.txt\n")
    assert "\r" not in md
    tr = (rec / (stem + " - תמלול.txt")).read_bytes().decode("utf-8")
    assert tr == ("[00:00:00] צד שני: שלום, אני רוצה לקבוע פגישה ביום שלישי\n"
                  "[00:00:07] נועה: בסדר גמור, אני אשלח לך את ההצעה עד מחר\n")
    assert (rec / (stem + ".mp3")).stat().st_size > 20_000

    calls = claude_calls(fake_claude[1])
    kinds = [c["kind"] for c in calls]
    assert kinds == ["bullets", "summary"]
    assert calls[0]["argv"] == ["-p", "--model", "sonnet"] and calls[1]["argv"] == ["-p", "--model", "opus"]
    assert "נועה: בסדר גמור" in calls[1]["prompt"] and "צד שני: שלום" in calls[1]["prompt"]
    assert "כותרות הטאבים הפעילים בכרום בזמן ההקלטה: (אין)" in calls[1]["prompt"]


def test_person_meetings_copy_and_live_points_file(rec, app_dir, fake_claude, tmp_path):
    clients, meetings = tmp_path / "clients", tmp_path / "meetings"
    clients.mkdir()
    meetings.mkdir()
    (clients / "דנה כהן.md").write_text("x", encoding="utf-8")
    write_cfg(app_dir, clients_dir=str(clients), meetings_dir=str(meetings))
    ch0, ch1 = two_speakers()
    base = rec / "2026-11-20 09-15-00 כרום"
    events, rc, err, _ = WF.run_worker(base, ch0, ch1, env_for(fake_claude, FAKE_CLAUDE_PERSON="דנה כהן"), speed=8)
    assert rc == 0, err
    stem = "2026-11-20 09-15 דנה כהן - קביעת פגישה על הצעת מחיר"
    assert (rec / (stem + ".md")).exists()
    md = (rec / (stem + ".md")).read_text(encoding="utf-8")
    assert "· **עם:** [[דנה כהן]]" in md
    assert (meetings / (stem + ".md")).read_text(encoding="utf-8") == md
    tr = (rec / (stem + " - תמלול.txt")).read_text(encoding="utf-8")
    assert tr.splitlines()[0].startswith("[00:00:00] דנה: ")       # "צד שני" -> the first name
    assert "people" not in md
    summary_prompt = next(c for c in claude_calls(fake_claude[1]) if c["kind"] == "summary")["prompt"]
    assert "דנה כהן" in summary_prompt.split("רשימת הלקוחות והאנשים המוכרים", 1)[1]


def test_custom_instructions_restyle_the_summary(rec, app_dir, fake_claude):
    write_cfg(app_dir, custom_instructions=["תמיד להוסיף טבלת משימות"])
    ch0, ch1 = two_speakers()
    events, rc, err, _ = WF.run_worker(rec / "2026-11-20 11-00-00 כרום", ch0, ch1, env_for(fake_claude), speed=8)
    assert rc == 0, err
    assert WF.types(events).count("saving") == 2
    md = next(rec.glob("*.md")).read_text(encoding="utf-8")
    assert "**נכתב מחדש לפי ההוראות**" in md
    calls = claude_calls(fake_claude[1])
    assert [c["kind"] for c in calls] == ["bullets", "summary", "restyle"]
    assert "תמיד להוסיף טבלת משימות" in calls[0]["prompt"]          # custom_block in the key points prompt


def test_no_speech(rec, app_dir, fake_claude):
    base = rec / "2026-11-20 12-00-00 כרום"
    events, rc, err, _ = WF.run_worker(base, WF.silence(3), WF.silence(3), env_for(fake_claude), speed=8)
    assert rc == 0, err
    assert WF.types(events) == ["loading", "ready", "saving", "done"]
    assert events[-1]["txt"] == str(base) + ".txt"
    assert ls(rec) == [base.name + ".txt"]                    # the app deletes it and shows "לא נקלט דיבור"
    assert claude_calls(fake_claude[1]) == []


def test_missing_model_is_an_error_not_no_speech(rec, app_dir, fake_claude, tmp_path):
    """QA: offline with no model, the worker used to die like a call without speech. Now: {"type": "error"} + exit 3."""
    from timmy.engine import worker as Wk
    base = rec / "2026-11-20 12-30-00 כרום"
    env = env_for(fake_claude, TIMMY_ASR_BACKEND="", TIMMY_ASR_MODEL="timmy-test/no-such-model", HF_HUB_OFFLINE="1",
                  HF_HOME=str(tmp_path / "hf"))
    events, rc, err, _ = WF.run_worker(base, WF.silence(2), WF.silence(2), env, speed=8)
    assert rc == Wk.EXIT_MODEL, err
    assert WF.types(events) == ["loading", "error"] and events[-1]["kind"] == "model_missing", events
    assert events[-1]["detail"]
    assert claude_calls(fake_claude[1]) == []


def test_mic_echo_of_the_other_side_is_not_a_line(rec, app_dir, fake_claude):
    # (not in the first half second: the sound check needs 0.5 s of Chrome history before the mic line, as on the Mac)
    ch0 = place(12, [(2.0, fixture("he_short.wav"))])
    d = int(0.15 * SR)
    ch1 = np.zeros_like(ch0)
    ch1[d:] = 0.4 * ch0[:-d]                                  # speakers -> mic
    events, rc, err, _ = WF.run_worker(rec / "2026-11-20 13-00-00 כרום", ch0, ch1, env_for(fake_claude), speed=8)
    assert rc == 0, err
    assert [e["speaker"] for e in events if e["type"] == "final"] == ["other"]


def test_without_claude_only_the_transcript(rec, app_dir, fake_claude, tmp_path):
    ch0, ch1 = two_speakers()
    env = env_for(fake_claude, TIMMY_CLAUDE_SEARCH=str(tmp_path / "missing" / "claude.exe"))
    events, rc, err, _ = WF.run_worker(rec / "2026-11-20 15-30-00 כרום", ch0, ch1, env, speed=8)
    assert rc == 0, err
    assert "bullets" not in WF.types(events)
    assert events[-1]["title"] == ""
    assert ls(rec) == sorted(["2026-11-20 15-30 שיחה בכרום.md", "2026-11-20 15-30 שיחה בכרום.mp3",
                              "2026-11-20 15-30 שיחה בכרום - תמלול.txt"])


def test_second_call_with_the_same_name_does_not_overwrite(rec, app_dir, fake_claude):
    ch0, ch1 = two_speakers()
    for sec in ("00", "40"):
        _, rc, err, _ = WF.run_worker(rec / f"2026-11-20 16-00-{sec} כרום", ch0, ch1, env_for(fake_claude), speed=8)
        assert rc == 0, err
    names = ls(rec)
    assert "2026-11-20 16-00 קביעת פגישה על הצעת מחיר.md" in names
    assert "2026-11-20 16-00 קביעת פגישה על הצעת מחיר (2).md" in names
    assert len(names) == 6


def test_output_is_utf8_whatever_the_code_page(rec, app_dir, fake_claude):
    """Windows pipes default to the ANSI code page (cp1252 cannot encode Hebrew): the worker forces UTF-8."""
    ch0, ch1 = two_speakers()
    env = env_for(fake_claude, PYTHONUTF8="0", PYTHONIOENCODING="cp1252")
    events, rc, err, _ = WF.run_worker(rec / "2026-11-20 17-00-00 כרום", ch0, ch1, env, speed=8)
    assert rc == 0, err
    assert any(e["type"] == "final" and "שלום" in e["text"] for e in events)


def test_restyle_mode(app_dir, fake_claude, tmp_path):
    meetings = tmp_path / "meetings"
    meetings.mkdir()
    write_cfg(app_dir, custom_instructions=["קצר יותר"], meetings_dir=str(meetings))
    d = tmp_path / "rec"
    d.mkdir()
    stem = "2026-11-20 14-03 דנה - שיחה"
    md = ("# שיחה\n\n**תאריך:** 2026-11-20 14:03 · **אורך:** 1:00 דק׳\n\n## סיכום\n\nטקסט.\n\n![צילום](images/a.png)\n\n"
          f"תמלול מלא: {stem} - תמלול.txt\n")
    (d / (stem + ".md")).write_text(md, encoding="utf-8")
    (d / (stem + " - תמלול.txt")).write_text("[00:00:01] צד שני: שלום\n", encoding="utf-8")
    (meetings / (stem + ".md")).write_text(md, encoding="utf-8")
    env = {**os.environ, **env_for(fake_claude), "PYTHONPATH": str(WIN)}
    r = subprocess.run([sys.executable, "-m", "timmy.engine.worker", "--restyle", str(d / (stem + ".md"))], cwd=WIN, env=env,
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    ev = [json.loads(x) for x in r.stdout.decode("utf-8").splitlines()]
    assert ev == [{"type": "restyled", "txt": str(d / (stem + ".md")), "changed": True}]
    new = (d / (stem + ".md")).read_text(encoding="utf-8")
    assert "**נכתב מחדש לפי ההוראות**" in new and "![צילום](images/a.png)" in new
    vault = (meetings / (stem + ".md")).read_text(encoding="utf-8")
    assert "**נכתב מחדש" in vault and "![צילום]" not in vault
    (call,) = claude_calls(fake_claude[1])
    assert call["kind"] == "restyle" and "[00:00:01] צד שני: שלום" in call["prompt"]

    write_cfg(app_dir)                                         # no custom instructions: nothing to do
    r = subprocess.run([sys.executable, "-m", "timmy.engine.worker", "--restyle", str(d / (stem + ".md"))], cwd=WIN, env=env,
                       capture_output=True, timeout=120)
    assert json.loads(r.stdout.decode("utf-8")) == {"type": "restyled", "txt": str(d / (stem + ".md")), "changed": False}


def test_restyle_rejects_a_broken_answer(app_dir, fake_claude, monkeypatch, tmp_path):
    L.configure({"custom_instructions": ["x"]})
    try:
        md = "# כותרת\n\n**תאריך:** 2026-11-20 14:03\n\nטקסט ארוך מספיק כדי שקיצור יבלוט מאוד מאוד.\n\nתמלול מלא: a - תמלול.txt\n"
        monkeypatch.setattr(W.C, "run", lambda *a, **k: "כותרת בלי סולמית")
        assert W.restyle(md, "", "claude") == md
        monkeypatch.setattr(W.C, "run", lambda *a, **k: "# כותרת\n\nקצר")
        assert W.restyle(md, "", "claude") == md
        good = md.replace("טקסט", "טקסט חדש")
        monkeypatch.setattr(W.C, "run", lambda *a, **k: "```markdown\n" + good + "```")
        assert W.restyle(md, "", "claude") == good.rstrip("\n") + "\n"
    finally:
        L.configure({})


def test_cancel_kills_worker_and_claude(rec, app_dir, fake_claude):
    """The UI's cancel: kill the Job the worker runs in; the claude it started (summary, hanging) dies too."""
    ch0, ch1 = two_speakers()
    env = {**os.environ, **env_for(fake_claude, FAKE_CLAUDE_SLEEP=120), "PYTHONPATH": str(WIN), "PYTHONUTF8": "1"}
    with jobs.Job() as job:
        p = job.popen([sys.executable, "-m", "timmy.engine.worker", str(rec / "2026-11-20 18-00-00 כרום")], cwd=WIN, env=env,
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        from timmy.engine import protocol as P
        for i in range(0, len(ch0), 1600):
            p.stdin.write(P.encode_frame(0, ch0[i:i + 1600]) + P.encode_frame(1, ch1[i:i + 1600]))
        p.stdin.close()
        end = time.time() + 60
        while time.time() < end and not claude_calls(fake_claude[1]):
            time.sleep(0.2)
        calls = claude_calls(fake_claude[1])
        assert calls, "the worker never called claude"
        job.kill()
        p.wait(15)
    deadline = time.time() + 10
    while time.time() < deadline and any(jobs.pid_alive(c["pid"]) for c in calls):
        time.sleep(0.1)
    assert not any(jobs.pid_alive(c["pid"]) for c in calls)


# ---------------------------------------------------------------- with faster-whisper

def _have_fw():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.asr
@pytest.mark.skipif(not _have_fw(), reason="faster-whisper not installed (extra: asr)")
def test_worker_with_the_tiny_model(rec, app_dir, fake_claude):
    """Real faster-whisper plumbing in the worker process (the tiny model barely knows Hebrew: only the event flow,
    the files and the timing are checked)."""
    ch0, ch1 = two_speakers()
    env = env_for(fake_claude, TIMMY_ASR_BACKEND="", TIMMY_ASR_MODEL=A.TINY_MODEL)
    events, rc, err, wall = WF.run_worker(rec / "2026-11-20 19-00-00 כרום", ch0, ch1, env, speed=2, timeout=600)
    assert rc == 0, err
    t = WF.types(events)
    assert t[:2] == ["loading", "ready"] and t[-1] == "done" and "saving" in t
    asr_log = next(e for e in WF.stderr_events(err) if e.get("event") == "asr")
    assert asr_log["backend"] == "faster-whisper" and asr_log["device"] in ("cpu", "cuda")
    assert not list(rec.glob("*.wav"))


@pytest.mark.asr
@pytest.mark.slow
@pytest.mark.skipif(not _have_fw(), reason="faster-whisper not installed (extra: asr)")
def test_worker_with_the_real_model(rec, app_dir, fake_claude):
    """The whole call with the ivrit.ai model on CPU, in real time, in the cpu profile (what most laptops get)."""
    from tests.test_asr import _cached
    if not (_cached(A.DEFAULT_MODEL) or os.environ.get("TIMMY_TEST_REAL_MODEL")):
        pytest.skip("real model not available (set TIMMY_TEST_REAL_MODEL=1 to download 1.6 GB)")
    a, r = fixture("he_short.wav"), fixture("he_reply.wav")
    ch0 = place(26, [(0.5, a), (15.0, a)])
    ch1 = place(26, [(8.0, r), (22.0, r)])
    env = env_for(fake_claude, TIMMY_ASR_BACKEND="", TIMMY_ASR_PROFILE="cpu")
    events, rc, err, wall = WF.run_worker(rec / "2026-11-20 20-00-00 כרום", ch0, ch1, env, speed=1, timeout=900)
    log = next((e for e in WF.stderr_events(err) if e.get("event") == "asr"), None)
    # written before the checks, so a failing CI run still uploads what the worker did (events, timings, stderr)
    (WIN / "out").mkdir(exist_ok=True)
    (WIN / "out" / "worker-real-model.json").write_text(json.dumps({"asr": log, "wall_s": round(wall, 1), "rc": rc, "events": events,
                                                                   "stderr": err[-20000:]}, ensure_ascii=False, indent=1),
                                                        encoding="utf-8")
    assert rc == 0, err
    finals = [(e["speaker"], e["t"], e["text"]) for e in events if e["type"] == "final"]
    # live lines of the two sides may arrive out of order in this profile (the saved transcript is sorted by time)
    assert sorted((t, s) for s, t, _ in finals) == [("00:00:00", "other"), ("00:00:07", "me"), ("00:00:14", "other"),
                                                    ("00:00:21", "me")]
    for s, _, text in finals:
        for w in (("פגישה", "שלישי", "הצעת המחיר") if s == "other" else ("בסדר גמור", "ההצעה", "מחר")):
            assert w in text, text
    assert log is not None
