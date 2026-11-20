"""The recording controller: starts / stops / cancels a call and turns the worker's events into card state.
Port of Recorder.swift `Recorder` (phases, flash messages, two-step cancel, disk checks, mic notices) on top of the
Windows processes (DESIGN.md 2):

    capture  python -m timmy.audio.capture          stdout = frames ---+   stderr = status JSON (levels, mic, chrome)
    worker   python -m timmy.engine.worker <base>   stdin  <-----------+   stdout = events JSON (ready/final/...)

Both run in one Job Object (winsys.jobs.Job): Stop = "stop" to the capture's stdin (it flushes and exits, the worker
sees EOF, summarizes, renames and exits); Cancel = Job.kill() (the worker's claude / ffmpeg children die too) and
every "<base>*" file is deleted, twice (now and 2 s later, as on the Mac).

No widgets here: the island (ui/island.py) only reads this object's state and calls toggle / stop / cancel_tapped /
set_hide_live. Thread safety: reader threads only emit Qt signals; all state changes happen on the GUI thread.
Test hook: TIMMY_CAPTURE_ARGS = JSON list of extra capture arguments (for example --fake-chrome a.wav).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from timmy import config as tconfig
from timmy import paths
from timmy.engine import protocol as P
from timmy.ui import calls as C
from timmy.ui import shell
from timmy.winsys import disk, jobs

FLASH_S = 2.5
ARM_S = 4.0
CONFIRM_AFTER_S = 0.5
NOTICE_S = 6.0


def child_python() -> str:
    """python.exe next to pythonw.exe: children get pipes, CREATE_NO_WINDOW keeps them invisible."""
    exe = sys.executable
    d, n = os.path.split(exe)
    if n.lower() == "pythonw.exe" and os.path.exists(os.path.join(d, "python.exe")):
        return os.path.join(d, "python.exe")
    return exe


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # the children import timmy from where this process found it (an editable install or the app folder)
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def delete_recording_files(base: str) -> list[str]:
    """Every file of a recording: "<base>.ch0.wav", ".txt", " נקודות.md", ".mp3" and anything starting with the base
    name (Mac deleteRecordingFiles). Returns what could not be deleted (Windows: a file still open)."""
    left = []
    d, name = os.path.split(base)
    if not base or len(name) <= 10:
        return left
    try:
        items = os.listdir(d)
    except OSError:
        return left
    for f in items:
        if f == name or f.startswith(name + ".") or f.startswith(name + " "):
            p = os.path.join(d, f)
            try:
                if os.path.isdir(p):
                    import shutil
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            except OSError:
                left.append(f)
    return left


class _Pipe(QObject):
    """Cross-thread bridge: reader threads emit, the controller (GUI thread) receives (queued connection)."""
    event = Signal(int, object)          # gen, worker event dict
    status = Signal(int, object)         # gen, capture status dict
    exited = Signal(int, str, int)       # gen, base, worker return code


class Recorder(QObject):
    changed = Signal()                   # anything on the card changed: the island repaints / relayouts
    line_added = Signal(object)          # {"t", "text", "speaker"}
    need_folder = Signal()               # not enough space: the UI opens the folder choice (Mac timmy.diskFull)
    saved = Signal(str)                  # final .md path of a finished call
    calls_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase = "idle"              # idle | recording | finishing | message
        self.message = ""
        self.elapsed = 0
        self.lines: list[dict] = []
        self.partial = ""
        self.partial_speaker = "other"
        self.thinking = False
        self.loading = False
        self.level = 0.0
        self.bullets: list[str] = []
        self.hide_live = bool(tconfig.load().get("hide_live_transcript") or False)
        self.cancel_armed = False
        self.notice: str | None = None
        self.base = ""
        self.done_file: str | None = None
        self.stopped_for_disk = False
        self.last_error = ""

        self._gen = 0
        self._job = None
        self._capture = None
        self._worker = None
        self._cancelling = False
        self._cancelled = set()
        self._armed_at = 0.0
        self._start = 0.0
        self._mic_seen = False
        self._chrome_seen = False

        self._pipe = _Pipe()
        self._pipe.event.connect(self._on_event)
        self._pipe.status.connect(self._on_status)
        self._pipe.exited.connect(self._on_exit)
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._on_tick)
        self._flash_timer = QTimer(self, singleShot=True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._disarm_timer = QTimer(self, singleShot=True)
        self._disarm_timer.timeout.connect(self.disarm_cancel)
        self._notice_timer = QTimer(self, singleShot=True)
        self._notice_timer.timeout.connect(self._clear_notice)

    # ---- state helpers --------------------------------------------------------------------------------------------
    @property
    def is_card(self) -> bool:
        return self.phase in ("recording", "finishing")

    def _changed(self):
        self.changed.emit()

    def flash(self, text: str):
        self.phase = "message"
        self.message = text
        self._flash_timer.start(int(FLASH_S * 1000))
        self._changed()

    def _end_flash(self):
        if self.phase == "message":
            self.phase = "idle"
            self.message = ""
            self._changed()

    def show_notice(self, text: str, seconds: float = NOTICE_S):
        self.notice = text
        self._notice_timer.start(int(seconds * 1000))
        self._changed()

    def _clear_notice(self):
        self.notice = None
        self._changed()

    def set_hide_live(self, on: bool):
        self.hide_live = bool(on)
        tconfig.save({"hide_live_transcript": self.hide_live})
        self._changed()

    def toggle(self):
        if self._cancelling:
            return
        if self.phase in ("idle", "message"):
            self.start()
        elif self.phase == "recording":
            self.stop()

    # ---- start ----------------------------------------------------------------------------------------------------
    def recordings_dir(self) -> str:
        return C.recordings_dir()

    def fresh_base(self, stamp: str) -> str:
        d = self.recordings_dir()
        b, n = os.path.join(d, f"{stamp} כרום"), 2
        while b in self._cancelled or os.path.exists(b + ".txt") or os.path.exists(b + ".ch0.wav"):
            b = os.path.join(d, f"{stamp}-{n} כרום")
            n += 1
        return b

    def capture_cmd(self) -> list[str]:
        cmd = [child_python(), "-m", "timmy.audio.capture"]
        extra = os.environ.get("TIMMY_CAPTURE_ARGS")
        if extra:
            try:
                cmd += [str(x) for x in json.loads(extra)]
            except ValueError:
                pass
        return cmd

    def worker_cmd(self, base: str) -> list[str]:
        return [child_python(), "-m", "timmy.engine.worker", base]

    def start(self) -> bool:
        if self.phase in ("recording", "finishing") or self._cancelling:
            return False
        rec = self.recordings_dir()
        if not disk.can_start(rec):
            self.flash("אין מקום")
            self.need_folder.emit()
            return False
        self.stopped_for_disk = False
        try:
            os.makedirs(rec, exist_ok=True)
        except OSError as e:
            self.last_error = str(e)
            self.flash("אין גישה לתיקייה")
            self.need_folder.emit()
            return False
        self.base = self.fresh_base(dt.datetime.now().strftime("%Y-%m-%d %H-%M-%S"))
        self.lines, self.partial, self.thinking, self.bullets, self.done_file = [], "", False, [], None
        self.loading, self.level, self.notice, self.cancel_armed = True, 0.0, None, False
        self._mic_seen = self._chrome_seen = False
        self._gen += 1
        gen, base = self._gen, self.base
        env = child_env()
        log = None
        try:
            self._job = jobs.Job()
            cwd = str(paths.app_dir()) if paths.app_dir().is_dir() else None
            self._capture = self._job.popen(self.capture_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, env=env, cwd=cwd)
            logs = paths.app_dir() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log = open(logs / "worker.log", "ab")
            log.write(f"\n=== {dt.datetime.now().isoformat(timespec='seconds')} {os.path.basename(base)}\n".encode("utf-8"))
            log.flush()
            self._worker = self._job.popen(self.worker_cmd(base), stdin=self._capture.stdout, stdout=subprocess.PIPE,
                                           stderr=log, env=env, cwd=cwd)
        except OSError as e:
            self.last_error = f"{type(e).__name__}: {e}"
            if self._job:
                self._job.kill()
                self._job.close()
            self._job = self._capture = self._worker = None
            self.loading = False
            self.flash("שגיאה")
            return False
        finally:
            if log:
                log.close()
        try:
            self._capture.stdout.close()     # the worker owns the read end now; EOF reaches it when capture exits
        except OSError:
            pass
        threading.Thread(target=self._read_worker, args=(gen, self._worker), name="worker-out", daemon=True).start()
        threading.Thread(target=self._read_capture, args=(gen, self._capture), name="capture-err", daemon=True).start()
        threading.Thread(target=self._wait_worker, args=(gen, base, self._worker), name="worker-wait", daemon=True).start()
        self._start = time.monotonic()
        self.elapsed = 0
        self.phase = "recording"
        self._tick.start()
        self._changed()
        return True

    # ---- reader threads (never touch state) -----------------------------------------------------------------------
    def _read_worker(self, gen, proc):
        for raw in iter(proc.stdout.readline, b""):
            ev = P.parse_event(raw.decode("utf-8", "replace"))
            if ev is not None:
                self._pipe.event.emit(gen, ev)

    def _read_capture(self, gen, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("{"):
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and o.get("event"):
                    self._pipe.status.emit(gen, o)

    def _wait_worker(self, gen, base, proc):
        rc = proc.wait()
        self._pipe.exited.emit(gen, base, rc if rc is not None else -1)

    # ---- events (GUI thread) --------------------------------------------------------------------------------------
    def _on_event(self, gen, o):
        if gen != self._gen:
            return
        t = o.get("type")
        if t == "ready":
            self.loading = False
        elif t == "partial":
            sp = o.get("speaker") or "other"
            text = o.get("text") or ""
            if text:
                self.partial, self.partial_speaker, self.thinking = text, sp, False
            elif o.get("thinking"):
                # noise on the mic must not replace what the other side is saying
                if not self.partial or self.partial_speaker == sp:
                    self.partial, self.partial_speaker, self.thinking = "", sp, True
            elif sp == self.partial_speaker:
                self.partial, self.thinking = "", False
        elif t == "final":
            sp = o.get("speaker") or "other"
            line = {"t": o.get("t") or "", "text": o.get("text") or "", "speaker": sp}
            self.lines.append(line)
            if sp == self.partial_speaker:
                self.partial, self.thinking = "", False
            self.line_added.emit(line)
        elif t == "bullets":
            items = o.get("items")
            if isinstance(items, list):
                self.bullets = [str(x) for x in items]
        elif t == "done":
            self.done_file = o.get("txt")
        elif t == "diskfull":
            if self.phase == "recording":
                self.stopped_for_disk = True
                self.stop()
        else:
            return
        self._changed()

    def _on_status(self, gen, o):
        if gen != self._gen or self.phase != "recording":
            return
        ev, st = o.get("event"), o.get("state")
        if ev == "levels":
            lv = float(o.get("chrome") or 0.0)
            self.level = max(lv, self.level * 0.6)
            self._changed()
        elif ev == "mic":
            name = str(o.get("name") or "").strip() or "מיקרופון המחשב"
            if st == "switched" or (st == "open" and self._mic_seen):
                self.show_notice("המיקרופון עבר ל: " + name)
            elif st in ("lost", "none"):
                self.show_notice("אין מיקרופון, ממשיך להקליט את הצד השני")
            elif st == "denied":
                self.show_notice("אין גישה למיקרופון: צריך לאשר בהגדרות הפרטיות", 10)
            if st in ("open", "switched", "lost", "none", "denied"):
                self._mic_seen = True
        elif ev == "chrome":
            if st == "closed":
                self.show_notice("כרום סגור. ממשיך להקליט את המיקרופון")
            elif st == "attached" and self._chrome_seen:
                self.show_notice("כרום מחובר, מקליט את השיחה")
            elif st == "unsupported" and sys.platform == "win32":
                self.show_notice("הקול של כרום לא נקלט (צריך ווינדוס 10 גרסה 2004 ומעלה)", 10)
            if st in ("closed", "attached"):
                self._chrome_seen = True

    def _on_tick(self):
        if self.phase != "recording":
            return
        self.elapsed = int(time.monotonic() - self._start)
        # every 20 s: out of space -> stop and keep what there is (Mac minKeepBytes)
        if self.elapsed > 0 and self.elapsed % disk.CHECK_EVERY_S == 0 and getattr(self, "_last_disk", -1) != self.elapsed:
            self._last_disk = self.elapsed
            if disk.must_stop(self.recordings_dir()):
                self.stopped_for_disk = True
                self.stop()
                return
        self._changed()

    # ---- stop -----------------------------------------------------------------------------------------------------
    def stop(self):
        if self._cancelling or self.phase != "recording":
            return
        self.disarm_cancel()
        self._tick.stop()
        self.notice = None
        self.phase = "finishing"
        cap = self._capture
        if cap and cap.stdin:
            try:
                cap.stdin.write(b"stop\n")
                cap.stdin.flush()
            except OSError:
                pass
            try:
                cap.stdin.close()
            except OSError:
                pass
        self._changed()

    def _on_exit(self, gen, base, rc):
        if base in self._cancelled:
            delete_recording_files(base)
            return
        if gen != self._gen:
            return
        job = self._job
        self._job = self._capture = self._worker = None
        if job:
            job.kill()            # anything the worker left behind (a hung claude) goes with it
            job.close()
        self._tick.stop()
        if self.phase == "recording":    # the worker ended by itself (crash): what it saved is what there is
            self.phase = "finishing"
        txt = self.done_file or (base + ".txt")
        self.loading = False
        if not self.lines:
            try:
                if txt.endswith(".txt") and os.path.exists(txt):
                    os.remove(txt)
            except OSError:
                pass
            self.flash("לא נקלט דיבור")
        else:
            if os.path.exists(txt):
                shell.reveal(txt)
                self.saved.emit(txt)
            self.flash("נשמר, אין מקום" if self.stopped_for_disk else "נשמר ✓")
        self.calls_changed.emit()

    # ---- cancel (two steps) ---------------------------------------------------------------------------------------
    def cancel_tapped(self):
        if self.phase != "recording" or self._cancelling:
            return
        if self.cancel_armed:
            # a fast double click (habit) does not delete: the confirmation counts only after half a second
            if time.monotonic() - self._armed_at >= CONFIRM_AFTER_S:
                self.cancel()
        else:
            self.arm_cancel()

    def arm_cancel(self, seconds: float = ARM_S):
        if self.phase != "recording":
            return
        self.cancel_armed = True
        self._armed_at = time.monotonic()
        self._disarm_timer.start(int(seconds * 1000))
        self._changed()

    def disarm_cancel(self):
        self._disarm_timer.stop()
        if self.cancel_armed:
            self.cancel_armed = False
            self._changed()

    def cancel(self):
        """Throw the recording away: kill capture + worker + everything they started, delete every file."""
        if self.phase != "recording" or self._cancelling:
            return
        self._cancelling = True
        self.disarm_cancel()
        self._tick.stop()
        b = self.base
        self._cancelled.add(b)
        self._gen += 1                   # output still on its way from the old processes is ignored
        job, procs = self._job, (self._capture, self._worker)
        self._job = self._capture = self._worker = None
        if job:
            job.kill()
        for p in procs:
            if p is not None:
                try:
                    p.wait(5)
                except Exception:
                    pass
        if job:
            job.close()
        delete_recording_files(b)
        self.lines, self.partial, self.thinking, self.bullets = [], "", False, []
        self.level, self.loading, self.notice, self.done_file = 0.0, False, None, None
        self._cancelling = False
        self.flash("בוטל")
        QTimer.singleShot(2000, lambda: delete_recording_files(b))

    def shutdown(self):
        """Timmy quits while recording: stop and let the worker save (the Mac app also saves on quit)."""
        if self.phase == "recording":
            self.stop()
        w = self._worker
        if w is not None:
            try:
                w.wait(60)
            except Exception:
                pass

    # ---- demo state for screenshots (the Mac --demo-card) ---------------------------------------------------------
    def demo(self, finishing=False, thinking=False, compact=None, armed=False, notice=None):
        self.lines = [
            {"t": "00:00:04", "text": "אז מי שהיה בסדנה תרשמו לי כן.", "speaker": "other"},
            {"t": "00:00:08", "text": "שזה לא אתה אלא דמותי, ניקח את זה כמחמאה, כן כן מהסדנה, איזה כיף.", "speaker": "other"},
            {"t": "00:00:14", "text": "יש כאן מישהו שלא היה בסדנה? זה מעניין אותי, מישהו שלא היה ובכל זאת הגיע.", "speaker": "other"},
            {"t": "00:00:20", "text": "אני לא הייתי, אבל ראיתי את ההקלטה.", "speaker": "me"},
        ]
        self.partial = "" if thinking else "אני לא הייתי, אוקיי, אז בואו נתחיל"
        self.partial_speaker = "me" if thinking else "other"
        self.thinking = thinking
        self.bullets = ["הסדנה הקודמת עברה טוב, רוב המשתתפים חזרו", "יש כמה משתתפים חדשים שלא היו בסדנה",
                        "המטרה היום: לבנות סוכן ראשון ב-Claude Code"]
        self.loading = False
        self.elapsed = 83
        self.level = 0.6
        self.phase = "finishing" if finishing else "recording"
        if compact is not None:
            self.hide_live = compact
        self.cancel_armed = armed
        self.notice = notice
        self._changed()
