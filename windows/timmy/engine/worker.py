"""The recording worker: a port of mac/live.py on faster-whisper. Same command line, events and files.

    python -m timmy.engine.worker "<base path>"          one call: frames on stdin, JSON lines on stdout
    python -m timmy.engine.worker --restyle "<summary.md>" rewrite a summary with the custom instructions

stdin  = frames [1 byte channel 0 = Chrome, 1 = mic][uint32 LE n][n float32 16 kHz mono] until EOF (protocol.py)
stdout = {"type": "loading" | "ready" | "partial" | "final" | "bullets" | "saving" | "done" | "diskfull" | "restyled"}
files  = <base>.ch0.wav / .ch1.wav while recording, <base>.txt ("[hh:mm:ss] <owner>: ..." / "צד שני: ..."),
         <base> נקודות.md (live key points); at the end "<stamp> [<person> - ]<title>.md", ".mp3", " - תמלול.txt"
stderr = diagnostics only ({"event": "asr", ...}, tracebacks); the UI never parses it.

What is the same as the Mac, line by line: voice-activity segmentation (Chan.step), hallucination filters,
echo handling (sound envelope while recording, text overlap at the end), key points every 30 s after the first
15 s, the summary / title / person / tasks prompt and its parsing, file names, the meetings-folder copy and
--restyle. Prompts and filters are not copied by hand: they come from mac/live.py through tools/sync_mac.py
(timmy.engine._mac.live_shared).

What differs (DESIGN.md 6.3): on a CPU that needs seconds per Whisper call the "cpu" profile skips text partials
(the card shows the thinking state) and transcribes finished utterances on a separate thread, several at a time
(up to 28 s per call), so it never falls behind; lines arrive 5 to 15 s late. The "fast" profile (CUDA, or a fast
machine) is the Mac behaviour unchanged. Windows specifics: UTF-8 files and stdout regardless of the code page,
children (claude, ffmpeg) without console windows and killed with their whole tree, safe Windows file names.
"""
import json
import os
import re
import struct
import sys
import threading
import time
import traceback
import wave

import numpy as np

from timmy import config as tconfig
from timmy.engine import asr as A
from timmy.engine import chrome_tabs as tabs_mod
from timmy.engine import claude as C
from timmy.engine import files as F
from timmy.engine._mac import live_shared as L
from timmy.winsys import jobs

SR = 16000
_IMG = L._IMG

# The parts of mac/live.py that are ported by hand (everything else comes through tools/sync_mac.py), with the
# sha256 of their Mac source at the time of the port. tests/test_worker.py fails when mac/live.py changes one of
# them, so a Mac fix is never silently missing here: port it, then update the hash.
MAC_PORTED = {
    "restyle": "b7876f3d0933b0daa8eba6ea8e7dd399524f0b933b63c6100da1f6727fb9037e",
    "<restyle mode>": "7edb1a6d1533be59d1e5ac68110917d8bb16c5d6e3dbe266a2cb950d20bd8fb0",
    "reader": "900d6e5b40574cc6042b594c02993a3872d004cae407020b63ee6c3948f2651f",
    "tr": "cd64bd3dd49aab4d6094e04410476d5f33f7151221bf1297b02f3f95514f6722",
    "update_bullets": "f277d1eeb7c0b848dc3545b96c6099fb2646d763b9a7ebe93611236225e1bfea",
    "is_echo": "9e60976c42700b1185b7c7fdbc495cdd924f620fc827eb9e22979c530a4a60ff",
    "Chan": "b6bdb9d84446613cfe03b8e2711c84ca5774792e5201e9221eccb88ac893ce3d",
    "<main loop>": "03dc150af4b3e0e9e6d3547f56c424f55ebe5429f45ff91a07e1f6edd3884887",
    "<end of call>": "c7d0d39e4752a9b313fd42cb59a245329ccb7f64a2a5aef43f093142e99aaec4",
}


def _open_text(path, mode="w"):
    # UTF-8 and "\n" on every OS, like the Mac files (Windows would otherwise use the ANSI code page and CRLF)
    return open(path, mode, encoding="utf-8", newline="\n")


class Emitter:
    """JSON lines on stdout, one lock so lines from the reader / ASR / key-point threads never interleave."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.lock = threading.Lock()
        self.events = []          # kept for in-process tests

    def __call__(self, o):
        with self.lock:
            self.events.append(o)
            try:
                self.stream.write(json.dumps(o, ensure_ascii=False) + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass              # the UI went away: keep going, the files are what matters


def log(**kw):
    try:
        sys.stderr.write(json.dumps(kw, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def setup_stdio():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- restyle ("שפר את טימי")

def restyle(md_text, transcript_text, claude):
    """mac/live.py restyle(): apply the custom instructions to a summary. On any failure: the original."""
    if not L.CUSTOM or not claude:
        return md_text
    out = C.run(claude, L.RESTYLE_PROMPT.format(owner=L.OWNER, custom="\n".join("- " + c for c in L.CUSTOM),
                                                md=md_text, transcript=transcript_text[-60000:]),
                model=L.SUMMARY_MODEL, timeout=240).strip()
    if not out:
        return md_text
    if out.startswith("```"):
        out = out.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    date_line = next((ln for ln in md_text.splitlines() if ln.startswith("**תאריך:**")), None)
    tr_line = next((ln for ln in md_text.splitlines() if ln.startswith("תמלול מלא:")), None)
    ok = (out.startswith("# ")
          and (date_line is None or date_line in out.splitlines())
          and (tr_line is None or tr_line in out.splitlines())
          and len(out) >= 0.4 * len(md_text.strip()))
    return L.keep_images(md_text, out) + "\n" if ok else md_text


def restyle_main(md_path, cfg, emit):
    claude = C.find_claude(cfg)
    tr_path = md_path[:-3] + " - תמלול.txt"
    with open(md_path, encoding="utf-8") as f:
        md_text = f.read()
    tr_text = ""
    if os.path.exists(tr_path):
        with open(tr_path, encoding="utf-8") as f:
            tr_text = f.read()
    new_text = restyle(md_text, tr_text, claude)
    if new_text != md_text:
        with _open_text(md_path) as f:
            f.write(new_text)
        _md = os.path.expanduser(cfg.get("meetings_dir") or "")
        _copy = os.path.join(_md, os.path.basename(md_path)) if _md else ""
        if _copy and os.path.exists(_copy) and not os.environ.get("LIVE_NO_VAULT"):
            # the meetings-folder copy has no images folder next to it: without the image lines (Mac)
            _vault = re.sub(r"\n{3,}", "\n\n", "\n".join(ln for ln in new_text.split("\n") if not _IMG.match(ln)))
            with _open_text(_copy) as f:
                f.write(_vault)
        emit({"type": "restyled", "txt": md_path, "changed": True})
    else:
        emit({"type": "restyled", "txt": md_path, "changed": False})


# ---------------------------------------------------------------- summary helpers (mac/live.py end of call)

def parse_summary(out):
    """-> (person, title, summary, tasks) from the SUMMARY_PROMPT answer."""
    title, summary, tasks, person = "", "", [], ""
    sec = None
    body = []
    for ln in (out or "").splitlines():
        st = ln.strip()
        if st.startswith("עם:"):
            person = st.split(":", 1)[1].strip().strip('"*')
            continue
        if st.startswith("כותרת:"):
            title = st.split(":", 1)[1].strip().strip('"*')
            continue
        if st.startswith("סיכום:"):
            sec = "s"
            rest = st.split(":", 1)[1].strip()
            body += [rest] if rest else []
            continue
        if st.startswith("משימות:"):
            sec = "t"
            continue
        if sec == "s" and st:
            body.append(st)
        if sec == "t" and st.startswith("- ") and st[2:].strip() not in ("", "אין", "..."):
            tasks.append(st[2:].strip())
    summary = "\n\n".join(body)
    if person in ("", "לא ידוע", "..."):
        person = ""
    return person, title, summary, tasks


def parse_bullets(out):
    items = []
    for ln in (out or "").splitlines():
        m = re.match(r"^\s*(?:[-•*]|\d+[.)])\s+(.+)$", ln)   # also "1." / "•" when the model drifts from the format
        if m and m.group(1).strip():
            items.append(m.group(1).strip())
    return items


def load_people(cfg):
    """(people, notes): known names for the summary prompt (config people, people_file, people_sources, and the
    note names in clients_dir), exactly the Mac rules."""
    clients_dir = os.path.expanduser(cfg.get("clients_dir") or "")
    people = [p.strip() for p in cfg.get("people", []) or [] if isinstance(p, str) and p.strip()]
    try:
        if cfg.get("people_file"):
            with open(os.path.expanduser(cfg["people_file"]), encoding="utf-8") as f:
                people += [ln.strip() for ln in f if ln.strip()]
    except Exception:
        pass
    for src in cfg.get("people_sources", []) or []:
        try:
            with open(os.path.expanduser(src["path"]), encoding="utf-8") as f:
                people += [m.strip() for m in re.findall(src["regex"], f.read()) if m.strip()]
        except Exception:
            pass
    notes = []
    try:
        for f in (os.listdir(clients_dir) if clients_dir else []):
            stem = f[:-3] if f.endswith(".md") else f
            if f.endswith(".md") or os.path.isdir(os.path.join(clients_dir, f)):
                if not re.match(r"^[^\w֐-׿]", stem):
                    notes.append(stem)
    except Exception:
        pass
    people = list(dict.fromkeys(people + [n.split(" - ")[0] for n in notes]))
    return people, notes


def note_link(person, notes, clients_dir):
    if not person:
        return ""
    match = next((n for n in notes if n == person), None) or next(
        (n for n in notes if n.startswith(person) or person.startswith(n.split(" - ")[0])), None)
    return f"[[{match or person}]]" if clients_dir else person


def build_markdown(title, safe, stamp, seconds, link, summary, bullets, tasks, new_base):
    Ls = int(seconds)
    md = [f"# {title or safe}", "",
          f"**תאריך:** {stamp[:10]} {stamp[11:16].replace('-', ':')} · **אורך:** {Ls // 60}:{Ls % 60:02d} דק׳" + (f" · **עם:** {link}" if link else ""), ""]
    if summary:
        md += ["## סיכום", "", summary, ""]
    if bullets:
        md += ["## נקודות עיקריות", ""] + ["- " + b for b in bullets] + [""]
    if tasks:
        md += ["## משימות", ""] + ["- [ ] " + t for t in tasks] + [""]
    md += [f"תמלול מלא: {os.path.basename(new_base)} - תמלול.txt", ""]
    return "\n".join(md)


# ---------------------------------------------------------------- one call

class Chan:
    """mac/live.py Chan: voice activity on one channel -> utterances -> lines."""

    def __init__(self, s, ch):
        self.s = s
        self.ch = ch
        self.pending = np.zeros(0, np.float32)
        self.start = 0
        self.last_partial = 0.0
        self.speaking = False
        self.waiting = 0            # cpu profile: utterances queued or being transcribed

    @property
    def sp(self):
        return "me" if self.ch == 1 else "other"

    def finalize(self, a, at):
        self.speaking = False
        if self.ch == 1 and L.echo_score(a, at) > 0.6:
            self.s.emit({"type": "partial", "text": "", "speaker": "me"})
            return
        if self.s.profile == "cpu":
            self.s.enqueue(self.ch, np.array(a, np.float32), at)
            return
        t, _, info = self.s.tr(a)
        self.line(t, info, at)

    def line(self, t, info, at):
        s = self.s
        sec = at / SR
        if t and not L.final_ok(t, info):
            t = ""
        if t and len(t.split()) <= 3 and info["min_lp"] < -0.65:   # only a "weak" repeat is a hallucination
            key = " ".join(L._norm(w) for w in t.split())
            with s.elock:
                recent = list(s.entries[-6:])
            if any(spk == self.ch and sec - t0 < 30 and " ".join(L._norm(w) for w in tx.split()) == key for (t0, spk, tx) in recent):
                t = ""
        if t and not (self.ch == 1 and s.is_echo(t, sec)):
            s.add_line(sec, self.ch, t)
        elif s.profile == "cpu" and (self.speaking or self.waiting > 1):
            s.emit({"type": "partial", "text": "", "thinking": True, "speaker": self.sp})
        else:
            s.emit({"type": "partial", "text": "", "speaker": self.sp})

    def step(self, new, done):
        if new:
            self.pending = np.concatenate([self.pending] + new)
        rms = L.speech_frames(self.pending)
        sp = rms > L.TH[self.ch]
        if not sp.any():
            if len(self.pending) > SR:
                drop = len(self.pending) - int(0.3 * SR)
                self.pending = self.pending[drop:]
                self.start += drop
            return
        cut = max(0, int(np.argmax(sp)) * L.FR - int(0.2 * SR))
        if cut:
            self.pending = self.pending[cut:]
            self.start += cut
            rms = L.speech_frames(self.pending)
            sp = rms > L.TH[self.ch]
        dur = len(self.pending) / SR
        if not self.speaking:
            self.speaking = True
            self.s.emit({"type": "partial", "text": "", "thinking": True, "speaker": self.sp})
        tail_silent = len(sp) >= 6 and not sp[-6:].any()
        if done and dur >= 14:
            # the call ended with a long backlog (typically: stopped while the model was still loading). The Mac
            # sends it to Whisper as one long utterance; here it is first cut into the usual <= 14 s pieces, so
            # every piece keeps its own timestamp and stays inside one 30 s window
            while len(self.pending) >= 14 * SR:
                rms = L.speech_frames(self.pending)
                lo, hi = 9 * SR // L.FR, 14 * SR // L.FR
                split = (lo + int(np.argmin(rms[lo:hi]))) * L.FR
                self.finalize(self.pending[:split], self.start)
                self.start += split
                self.pending = self.pending[split:]
        if done or (dur >= 2 and tail_silent):
            self.finalize(self.pending, self.start)
            self.start += len(self.pending)
            self.pending = np.zeros(0, np.float32)
        elif dur >= 14:
            lo, hi = 9 * SR // L.FR, 14 * SR // L.FR
            split = (lo + int(np.argmin(rms[lo:hi]))) * L.FR
            self.finalize(self.pending[:split], self.start)
            self.start += split
            self.pending = self.pending[split:]
        elif self.s.profile == "fast" and time.time() - self.last_partial >= 1.2 and dur >= 0.8:
            if self.ch == 1 and L.echo_score(self.pending, self.start) > 0.6:
                self.last_partial = time.time()
                return
            t, ok, _ = self.s.tr(self.pending, partial=True)
            if ok:
                self.s.emit({"type": "partial", "text": t, "speaker": self.sp})
            else:
                self.s.emit({"type": "partial", "text": "", "thinking": True, "speaker": self.sp})
            self.last_partial = time.time()


class Session:
    def __init__(self, base, cfg, emit, asr=None, claude="", tabs="", profile="fast", tick=0.25,
                 bullets_first=15.0, bullets_every=30.0):
        self.base = base
        self.cfg = cfg
        self.emit = emit
        self.asr = asr
        self.claude = claude
        self.tabs = tabs
        self.profile = profile
        self.tick = tick
        self.bullets_first, self.bullets_every = bullets_first, bullets_every
        self.disk_full = False
        self.lock = threading.Lock()
        self.chunks = {0: [], 1: []}
        self.eof = False
        self.total = {0: 0, 1: 0}
        self.got = {0: False, 1: False}
        self.entries = []          # (start_sec, speaker, text)
        self.finals = []           # "speaker: text" in arrival order, for the prompts
        self.elock = threading.Lock()
        self.bullets = []
        self.blk = threading.Lock()
        self.bullets_done_upto = 0
        self.txt = None
        self.chans = {0: Chan(self, 0), 1: Chan(self, 1)}
        # cpu profile: queue of finished utterances per channel
        self.queue = {0: [], 1: []}
        self.qcv = threading.Condition()
        self.q_closed = False
        self.asr_thread = None
        self.reader_thread = None
        self.stopped = threading.Event()
        # one call per process, like the Mac: the echo detector's Chrome history starts empty
        L.CH0_HIST["buf"] = np.zeros(0, np.float32)
        L.CH0_HIST["start"] = 0

    # ---- input
    def start_reader(self, stream):
        ws = {}
        for ch in (0, 1):
            w = wave.open(f"{self.base}.ch{ch}.wav", "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            ws[ch] = w
        self.reader_thread = threading.Thread(target=self._reader, args=(stream, ws), daemon=True, name="reader")
        self.reader_thread.start()

    def _reader(self, f, ws):
        def readn(n):
            b = b""
            while len(b) < n:
                try:
                    d = f.read(n - len(b))
                except (OSError, ValueError):
                    return None
                if not d:
                    return None
                b += d
            return b

        try:
            while True:
                h = readn(5)
                if h is None:
                    break
                ch, n = h[0], struct.unpack("<I", h[1:])[0]
                d = readn(n * 4)
                if d is None:
                    break
                if ch not in (0, 1):
                    continue
                a = np.frombuffer(d, dtype="<f4").astype(np.float32)
                try:
                    if not self.disk_full:
                        ws[ch].writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
                except OSError:
                    # disk full: stop writing audio, keep transcribing, ask the app to stop
                    self.disk_full = True
                    self.emit({"type": "diskfull"})
                with self.lock:
                    self.chunks[ch].append(a)
                    self.total[ch] += len(a)
                    self.got[ch] = True
        finally:
            for w in ws.values():
                try:
                    w.close()
                except OSError:
                    pass
            with self.lock:
                self.eof = True

    # ---- ASR
    def tr(self, a, partial=False):
        try:
            segs = self.asr.segments(a, partial=partial)
        except Exception:
            log(event="asr_error", error=traceback.format_exc(limit=3))
            return "", False, {"changed": False, "min_lp": 0.0, "max_temp": 0.0}
        return A.segments_info(segs, partial=partial)

    def enqueue(self, ch, a, at):
        with self.qcv:
            self.queue[ch].append((a, at))
            self.chans[ch].waiting += 1
            self.qcv.notify()

    def start_asr_thread(self):
        self.asr_thread = threading.Thread(target=self._asr_loop, daemon=True, name="asr")
        self.asr_thread.start()

    def _asr_loop(self):
        while True:
            with self.qcv:
                while not (self.queue[0] or self.queue[1]) and not self.q_closed:
                    self.qcv.wait()
                if not (self.queue[0] or self.queue[1]):
                    return
                # the channel whose oldest utterance is the earliest: lines come out roughly in time order, so
                # the text echo check (mic line vs the other side's line at the same time) still works
                c = min((c for c in (0, 1) if self.queue[c]), key=lambda c: self.queue[c][0][1])
                n = A.merge_budget([len(a) for a, _ in self.queue[c]])
                items = self.queue[c][:n]
                del self.queue[c][:n]
            self._transcribe_items(c, items)

    def _transcribe_items(self, c, items):
        try:
            if len(items) == 1:
                groups = [self.asr.segments(items[0][0])]
            else:
                audio, spans = A.merge([a for a, _ in items])
                groups = A.split(self.asr.segments(audio, timestamps=True), spans)
        except Exception:
            log(event="asr_error", error=traceback.format_exc(limit=3))
            groups = [[] for _ in items]
        for (_a, at), g in zip(items, groups):
            t, _, info = A.segments_info(g)
            try:
                self.chans[c].line(t, info, at)
            finally:
                with self.qcv:
                    self.chans[c].waiting -= 1

    # ---- lines
    def is_echo(self, text, at):
        # without headphones the mic hears the other side from the speakers: a mic line like what the other side
        # said at the same time is an echo (mac/live.py is_echo)
        import difflib
        with self.elock:
            recent = list(self.entries[-12:])
        for (t0, spk, tx) in recent:
            if spk == 0 and abs(t0 - at) < 12 and difflib.SequenceMatcher(None, text, tx).ratio() > 0.55:
                return True
        return False

    def add_line(self, sec, ch, t):
        with self.elock:
            self.entries.append((sec, ch, t))
            self.finals.append(f"{L.NAMES[ch]}: {t}")
            if self.txt:
                self.txt.write(f"[{L.ts(sec)}] {L.NAMES[ch]}: {t}\n")
                self.txt.flush()
        self.emit({"type": "final", "text": t, "t": L.ts(sec), "speaker": "me" if ch == 1 else "other"})

    # ---- key points
    def update_bullets(self):
        if not self.claude:
            return
        with self.blk:
            with self.elock:
                n = len(self.finals)
                transcript = "\n".join(self.finals)[-9000:]
            if n == self.bullets_done_upto:
                return
            prompt = L.PROMPT.format(owner=L.OWNER, owner_line=L.OWNER_LINE, custom=L.custom_block("נקודות"),
                                     bullets="\n".join("- " + b for b in self.bullets) or "(אין עדיין)", transcript=transcript)
            items = parse_bullets(C.run(self.claude, prompt, model=L.BULLETS_MODEL, timeout=90))
            if not items:
                return
            self.bullets[:] = items
            self.bullets_done_upto = n
            try:
                with _open_text(self.base + " נקודות.md") as f:
                    f.write("# נקודות עיקריות\n\n" + "\n".join("- " + b for b in items) + "\n")
            except OSError:
                pass
            self.emit({"type": "bullets", "items": items})

    def _bullets_loop(self):
        if self.stopped.wait(self.bullets_first):
            return
        while True:
            self.update_bullets()
            if self.stopped.wait(self.bullets_every):
                return

    # ---- the call
    def run(self):
        """mac/live.py main loop: every 0.25 s take what the reader got and step both channels; EOF ends it."""
        self.begin()
        while True:
            time.sleep(self.tick)
            with self.lock:
                new = {c: self.chunks[c][:] for c in (0, 1)}
                for c in (0, 1):
                    self.chunks[c].clear()
                done = self.eof and not self.chunks[0] and not self.chunks[1]
            self.step_all(new, done)
            if done:
                break
        return self.end()

    def begin(self, bullets=True):
        if bullets:
            threading.Thread(target=self._bullets_loop, daemon=True, name="bullets").start()
        self.txt = _open_text(self.base + ".txt", "a")
        if self.profile == "cpu":
            self.start_asr_thread()

    def step_all(self, new, done):
        L.ch0_append(new[0])
        for c in (0, 1):
            self.chans[c].step(new[c], done)

    def end(self):
        """After the last step: wait for queued transcriptions, then save (summary, names, mp3)."""
        if self.asr_thread:
            with self.qcv:
                self.q_closed = True
                self.qcv.notify_all()
            self.asr_thread.join()
        self.stopped.set()
        with self.elock:
            self.txt.close()
            self.txt = None
        return self.finish()

    def write_transcript(self, path, other_name):
        with _open_text(path) as f:
            for (sec, spk, t) in self.entries:
                f.write(f"[{L.ts(sec)}] {L.NAMES[1] if spk == 1 else other_name}: {t}\n")

    def finish(self):
        base, cfg, emit = self.base, self.cfg, self.emit
        emit({"type": "saving"})
        self.update_bullets()
        entries = self.entries
        entries.sort(key=lambda e: e[0])

        # an echo that got through: a mic line mostly contained in what the other side said within 15 s is dropped
        def _echo_line(e):
            sec, spk, t = e
            if spk != 1:
                return False
            near = " ".join(x[2] for x in entries if x[1] == 0 and abs(x[0] - sec) < 15)
            return L.text_overlap(t, near) > 0.6

        entries[:] = [e for e in entries if not _echo_line(e)]
        self.write_transcript(base + ".txt", L.NAMES[0])
        # audio: both channels mixed into one mp3
        ins = [f"{base}.ch{c}.wav" for c in (0, 1) if self.got[c]]
        if ins:
            F.mix_mp3(F.ffmpeg_path(cfg), ins, base + ".mp3")
        if os.path.exists(base + ".mp3"):
            for c in (0, 1):
                if os.path.exists(f"{base}.ch{c}.wav") and not os.environ.get("LIVE_KEEP_WAV"):
                    os.remove(f"{base}.ch{c}.wav")
        if not entries:
            for ext in (".mp3", ".ch0.wav", ".ch1.wav", " נקודות.md"):
                try:
                    os.remove(base + ext)
                except OSError:
                    pass
            emit({"type": "done", "txt": base + ".txt"})
            return base + ".txt"

        # summary + title at the end, files renamed by the title
        clients_dir = os.path.expanduser(cfg.get("clients_dir") or "")
        meetings_dir = os.path.expanduser(cfg.get("meetings_dir") or "")
        people, notes = load_people(cfg)
        transcript = "\n".join(f"{L.NAMES[sp]}: {t}" for _, sp, t in entries)
        out = C.run(self.claude, L.SUMMARY_PROMPT.format(owner=L.OWNER, owner_line=L.OWNER_LINE, transcript=transcript,
                                                         tabs=self.tabs or "(אין)", people="\n".join(people) or "(אין)"),
                    model=L.SUMMARY_MODEL, timeout=180)
        person, title, summary, tasks = parse_summary(out)
        link = note_link(person, notes, clients_dir)
        safe = F.safe_title(title)
        stamp = F.stamp_of(base)
        new = F.unique_base(F.final_base(base, person, title), base)
        # in the final transcript "צד שני" becomes the person's first name, if known
        self.write_transcript(base + ".txt", person.split()[0] if person else L.NAMES[0])
        F.move(base + ".txt", new + " - תמלול.txt")
        F.move(base + ".mp3", new + ".mp3")
        if os.path.exists(base + " נקודות.md"):
            os.remove(base + " נקודות.md")
        seconds = max(self.total.values()) / SR
        md_text = build_markdown(title, safe, stamp, seconds, link, summary, self.bullets, tasks, new)
        if L.CUSTOM:   # "שפר את טימי": the summary is rewritten by the personal instructions
            emit({"type": "saving"})
            md_text = restyle(md_text, transcript, self.claude)
        with _open_text(new + ".md") as f:
            f.write(md_text)
        if person and meetings_dir and os.path.isdir(meetings_dir) and not os.environ.get("LIVE_NO_VAULT"):
            try:
                with _open_text(os.path.join(meetings_dir, os.path.basename(new) + ".md")) as f:
                    f.write(md_text)
            except OSError:
                pass
        emit({"type": "done", "txt": new + ".md", "title": title})
        return new + ".md"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_stdio()
    if not argv:
        print("usage: python -m timmy.engine.worker <base> | --restyle <summary.md>", file=sys.stderr)
        return 2
    cfg = tconfig.load()
    L.configure(cfg)
    emit = Emitter()
    if argv[0] == "--restyle":
        if len(argv) < 2:
            return 2
        restyle_main(argv[1], cfg, emit)
        return 0
    base = argv[0]
    s = Session(base, cfg, emit, claude=C.find_claude(cfg))
    s.start_reader(sys.stdin.buffer)
    s.tabs = tabs_mod.chrome_tabs()
    emit({"type": "loading"})
    s.asr = A.make_asr(cfg)
    t0 = time.perf_counter()
    s.tr(np.zeros(SR, np.float32))
    warm = time.perf_counter() - t0
    s.profile = A.pick_profile(cfg, getattr(s.asr, "device", "cpu"), warm)
    if getattr(s.asr, "device", "cpu") == "cpu" and s.profile == "cpu":
        jobs.lower_priority()
    log(event="asr", backend=s.asr.name, device=getattr(s.asr, "device", ""), compute_type=getattr(s.asr, "compute_type", ""),
        threads=getattr(s.asr, "threads", 0), warmup_s=round(warm, 3), profile=s.profile, claude=bool(s.claude))
    emit({"type": "ready"})
    s.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
