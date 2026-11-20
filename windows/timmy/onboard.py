"""Onboarding checks ("הרשאות והגדרה"), no Qt: the Windows twin of Recorder.swift `Onboarding`.

Steps (ui/onboarding.py shows them one by one, the state is re-checked every second while the window is open):
  0 welcome
  1 Windows version (19041+, else "update Windows") and the microphone: the privacy switch
    (HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone, value "Deny" at the top level or under NonPackaged =
    desktop apps may not use the mic) and a 4 s test capture through the capture process (`--mic-test`, levels)
  2 Chrome (Timmy records only Chrome's audio)
  3 Claude Code: installed (engine/claude.py finder), `--version` runs, `claude auth status --json` -> loggedIn,
    login with `claude auth login --claudeai` in the background (it opens the browser by itself; the first https URL
    it prints is shown as a fallback link; a code can be pasted into its stdin), Git for Windows (Claude Code on
    Windows needs Git Bash: `git --version`, CLAUDE_CODE_GIT_BASH_PATH or the usual install folders)
  4 the recordings folder + free space, launch at login, hide in screen share
  5 done (Windows 11 hides new tray icons under the ^ arrow: the text says where Timmy is)
TIMMY_DEMO_CLAUDE / TIMMY_DEMO_MIC fix a state for screenshots (the Mac demo switches).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from timmy import config as tconfig
from timmy import paths
from timmy.engine import claude as C
from timmy.engine import procrun
from timmy.winsys import disk, startup, winver

IS_WIN = sys.platform == "win32"
STEPS = 6
MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
GIT_URL = "https://git-scm.com/download/win"
CLAUDE_URL = "https://claude.com/download"
CHROME_URL = "https://www.google.com/chrome/"


# ---- microphone ----------------------------------------------------------------------------------------------------
def mic_consent() -> str:
    """"allow" | "deny" (the privacy switch blocks desktop apps) | "unknown" (no key / not Windows)."""
    if os.environ.get("TIMMY_DEMO_MIC"):
        return "deny" if os.environ["TIMMY_DEMO_MIC"] == "denied" else "allow"
    if not IS_WIN:
        return "unknown"
    import winreg
    vals = []
    for sub in ("", "\\NonPackaged"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_KEY + sub) as k:
                vals.append(str(winreg.QueryValueEx(k, "Value")[0]))
        except OSError:
            vals.append("")
    if any(v.lower() == "deny" for v in vals):
        return "deny"
    return "allow" if any(v.lower() == "allow" for v in vals) else "unknown"


def child_python() -> str:
    exe = sys.executable
    d, n = os.path.split(exe)
    if n.lower() == "pythonw.exe" and os.path.exists(os.path.join(d, "python.exe")):
        return os.path.join(d, "python.exe")
    return exe


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def mic_test(seconds: float = 4.0, extra_args=()) -> dict:
    """A short capture of the default microphone. {"state": "ok" | "silent" | "none" | "denied" | "error", "peak", "name"}."""
    if os.environ.get("TIMMY_DEMO_MIC"):
        st = os.environ["TIMMY_DEMO_MIC"]
        return {"state": st if st in ("ok", "silent", "none", "denied") else "ok", "peak": 0.4 if st == "ok" else 0.0, "name": "מיקרופון לדוגמה"}
    cmd = [child_python(), "-m", "timmy.audio.capture", "--mic-test", "--ignore-stdin", "--seconds", str(seconds)] + list(extra_args)
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=seconds + 20,
                           env=child_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        err = p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "error", "peak": 0.0, "name": ""}
    peak, state, name = 0.0, "", ""
    for ln in err.splitlines():
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("event") == "levels" and isinstance(o.get("mic"), (int, float)):
            peak = max(peak, float(o["mic"]))
        elif o.get("event") == "mic":
            s = o.get("state")
            if s in ("open", "switched"):
                name = str(o.get("name") or name)
                state = state or "open"
            elif s in ("denied", "none", "error") and state != "open":
                state = s
    if state in ("denied", "none", "error"):
        return {"state": state, "peak": peak, "name": name}
    return {"state": "ok" if peak > 0.002 else "silent", "peak": peak, "name": name}


# ---- Chrome --------------------------------------------------------------------------------------------------------
def chrome_paths() -> list[str]:
    out = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        b = os.environ.get(env)
        if b:
            out.append(os.path.join(b, "Google", "Chrome", "Application", "chrome.exe"))
    return out


def chrome_installed() -> bool:
    if os.environ.get("TIMMY_DEMO_CHROME"):
        return os.environ["TIMMY_DEMO_CHROME"] != "0"
    if not IS_WIN:
        return os.path.exists("/Applications/Google Chrome.app")
    return any(os.path.exists(p) for p in chrome_paths())


# ---- Git for Windows -----------------------------------------------------------------------------------------------
def git_bash() -> str:
    """Git Bash for Claude Code on Windows, or "". Off Windows: "/bin/bash" (Claude Code does not need Git for Windows)."""
    if os.environ.get("TIMMY_DEMO_GIT"):
        return "" if os.environ["TIMMY_DEMO_GIT"] == "0" else "bash.exe"
    if not IS_WIN:
        return "/bin/bash"
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and os.path.exists(env):
        return env
    for b in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs")):
        if b:
            p = os.path.join(b, "Git", "bin", "bash.exe")
            if os.path.exists(p):
                return p
    g = shutil.which("git")
    if g:
        p = os.path.join(os.path.dirname(os.path.dirname(g)), "bin", "bash.exe")
        if os.path.exists(p):
            return p
    return ""


# ---- Claude Code ---------------------------------------------------------------------------------------------------
BROKEN = "מצאתי את Claude Code, אבל הוא לא נפתח. אפשר לקבל אותו מחדש דרך אפליקציית Claude, ואז ללחוץ \"בדוק שוב\"."


def problem_text(k: str) -> str:
    return {"network": "אתה מחובר ל-Claude, אבל כרגע אין אליו חיבור. בדוק שיש אינטרנט ולחץ \"בדוק שוב\".",
            "timeout": "אתה מחובר ל-Claude, אבל הוא לא ענה בזמן. אולי האינטרנט איטי או ש-Claude עמוס. נסה \"בדוק שוב\" בעוד דקה.",
            "limit": "אתה מחובר ל-Claude, אבל הגעת למכסת השימוש של המנוי. הסיכומים יחזרו לעבוד כשהמכסה תתחדש."}.get(
        k, "אתה מחובר ל-Claude, אבל הוא לא הצליח לענות כרגע. נסה \"בדוק שוב\" בעוד דקה.")


def login_failure_text(k: str) -> str:
    return {"outdated": "הגרסה של Claude Code שבמחשב ישנה מדי, ולא יודעת להתחבר מכאן. צריך גרסה חדשה, וזה לוקח דקה.",
            "network": "אין חיבור לאינטרנט, אז ההתחברות לא הושלמה. בדוק את החיבור ונסה שוב.",
            "badcode": "הקוד לא התקבל. נסה שוב, והפעם העתק את כל הקוד בכפתור Copy Code שבדף.",
            "timeout": "עבר הרבה זמן ולא הגיע אישור מהדפדפן, אז עצרתי. אפשר לנסות שוב."}.get(
        k, "ההתחברות לא הושלמה. אפשר לנסות שוב, וללחוץ על כפתור האישור (Authorize) בדף שנפתח בדפדפן.")


def login_failure_kind(out: str, quick: bool) -> str:
    t = out.lower()
    if quick and ("unknown command" in t or "unknown option" in t or "error: unknown" in t or ("usage:" in t and "http" not in t)):
        return "outdated"
    if any(w in t for w in ("enotfound", "econnrefused", "etimedout", "getaddrinfo", "network", "fetch failed", "unable to connect")):
        return "network"
    if any(w in t for w in ("wasn't accepted", "was not accepted", "invalid code", "invalid_grant", "invalid authorization code")):
        return "badcode"
    return "other"


def version_key(s: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", s or "")
    return tuple(int(x or 0) for x in m.groups()) if m else ()


def _cmd(path: str, args: list[str]):
    try:
        return C.command(path, args)
    except ValueError:
        return None


def auth_status(path: str, timeout: float | None = None):
    """True / False from `claude auth status --json`, None when it did not answer (old version, stuck)."""
    t = timeout or float(os.environ.get("TIMMY_CLAUDE_STATUS_TIMEOUT") or 10)
    cmd = _cmd(path, ["auth", "status", "--json"])
    if cmd is None:
        return None
    r = procrun.run_process(cmd, None, timeout=t)
    o = procrun.last_json_object(r.out, "loggedIn")
    if not r.launched or r.timed_out or o is None or not isinstance(o.get("loggedIn"), bool):
        return None
    return o["loggedIn"]


def health_check(path: str) -> tuple[str, str]:
    """A tiny question to Claude, only to see that it really answers. ("ok" | "auth" | "failed", message)."""
    t = float(os.environ.get("TIMMY_CLAUDE_HEALTH_TIMEOUT") or 30)
    cmd = _cmd(path, ["-p", "--model", "haiku", "--strict-mcp-config", "--no-session-persistence"])
    if cmd is None:
        return "failed", problem_text("other")
    r = procrun.run_process(cmd, "ענה במילה אחת בלבד: מחובר", timeout=t, merge_stderr=True)
    if r.ok:
        return "ok", ""
    if r.timed_out:
        return "failed", problem_text("timeout")
    if not r.launched:
        return "failed", "לא הצלחתי להפעיל את Claude Code. כדאי להתקין אותו מחדש וללחוץ \"בדוק שוב\"."
    t2 = r.out.lower()
    if any(w in t2 for w in ("/login", "invalid api key", "oauth token", "not logged in", "authentication_error", "please log in",
                             "token has expired", "revoked", "error: 401", "status 401")):
        return "auth", ""
    if any(w in t2 for w in ("usage limit", "rate limit", "limit reached", "rate_limit", "quota", "error: 429", "overloaded")):
        return "failed", problem_text("limit")
    if any(w in t2 for w in ("enotfound", "econnrefused", "etimedout", "econnreset", "getaddrinfo", "network", "fetch failed",
                             "unable to connect", "connection error", "offline")):
        return "failed", problem_text("network")
    return "failed", problem_text("other")


def detect_claude(cfg: dict | None = None) -> tuple[str, str | None, str]:
    """(state, path, note): "ok" | "not_installed" | "not_logged_in" | "problem" | "unknown". Several installs: a logged-in
    one wins, then the newest. The choice is saved as claude_path (like the Mac)."""
    cfg = cfg if cfg is not None else tconfig.load()
    cands = []
    for c in C.candidates(cfg):
        if C.runnable(c) and os.path.realpath(c) not in [os.path.realpath(x) for x in cands]:
            cands.append(c)
    if not cands:
        return "not_installed", None, ""
    items = []
    lock = threading.Lock()

    def probe(i, p):
        cmd = _cmd(p, ["--version"])
        v = procrun.run_process(cmd, None, timeout=15) if cmd is not None else procrun.ProcResult()
        a = auth_status(p)
        with lock:
            items.append((i, p, version_key(v.out), (v.launched and not v.timed_out and v.status == 0) or a is not None, a))
    th = [threading.Thread(target=probe, args=(i, p), daemon=True) for i, p in enumerate(cands)]
    for t in th:
        t.start()
    for t in th:
        t.join(40)
    order = sorted(items, key=lambda x: (tuple(-n for n in x[2]) if x[2] else (1,), x[0]))
    runs = [x for x in order if x[3]]
    if not runs:
        return "problem", cands[0], BROKEN

    def remember(p):
        if str(cfg.get("claude_path") or "") != p:
            tconfig.save({"claude_path": p})
    best = next((x for x in runs if x[4] is True), None)
    if best:
        remember(best[1])
        return "ok", best[1], ""
    best = next((x for x in runs if x[4] is False), None)
    if best:
        remember(best[1])
        return "not_logged_in", best[1], ""
    remember(runs[0][1])
    st, msg = health_check(runs[0][1])
    return ({"ok": "ok", "auth": "not_logged_in"}.get(st, "problem")), runs[0][1], msg


def first_url(s: str) -> str:
    m = re.search(r"(https://[^\s\"'<>]+)", s or "")
    return m.group(1) if m else ""


class ClaudeLogin:
    """`claude auth login --claudeai` in the background, never a terminal. It opens the browser itself; we collect its
    output for the fallback link, accept a pasted code on stdin, and finish when it exits or `auth status` says yes."""

    def __init__(self, path: str, on_update=None):
        self.path = path
        self.on_update = on_update or (lambda: None)
        self.url = ""
        self.out = ""
        self.done = False
        self.ok = False
        self.error = ""
        self.started = time.time()
        self.proc = None
        self.job = None

    def start(self, timeout: float = 600):
        from timmy.winsys import jobs
        cmd = _cmd(self.path, ["auth", "login", "--claudeai"])
        if cmd is None:
            self.done, self.error = True, login_failure_text("other")
            return self
        try:
            self.job = jobs.Job()
            self.proc = self.job.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=paths.app_dir()
                                       if os.path.isdir(paths.app_dir()) else None)
        except OSError:
            self.done, self.error = True, login_failure_text("other")
            return self
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._watch, args=(timeout,), daemon=True).start()
        return self

    def _read(self):
        for raw in iter(lambda: self.proc.stdout.readline(), b""):
            self.out += raw.decode("utf-8", "replace")
            if not self.url:
                self.url = first_url(self.out)
            self.on_update()

    def _watch(self, timeout):
        while self.proc.poll() is None:
            if time.time() - self.started > timeout:
                self.job.kill()
                self.finish(False, login_failure_text("timeout"))
                return
            time.sleep(0.5)
        quick = time.time() - self.started < 8
        logged = auth_status(self.path) is True
        if self.proc.returncode == 0 or logged:
            self.finish(True, "")
        else:
            self.finish(False, login_failure_text(login_failure_kind(self.out, quick)))

    def send_code(self, code: str):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.write((code.strip() + "\n").encode())
                self.proc.stdin.flush()
            except OSError:
                pass

    def cancel(self):
        if self.job is not None:
            self.job.kill()
        self.finish(False, "")

    def finish(self, ok: bool, error: str):
        if self.done:
            return
        self.done, self.ok, self.error = True, ok, error
        if self.job is not None:
            self.job.close()
        self.on_update()


# ---- folder / state ------------------------------------------------------------------------------------------------
def recordings_dir() -> str:
    from timmy.ui import calls as Cl
    return Cl.recordings_dir()


def free_gb(path: str) -> float:
    return round(disk.free_bytes(path) / 1e9, 1)


def set_folder(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    tconfig.save({"output_dir": path})
    return True


def step_saved() -> int:
    try:
        return int(tconfig.load().get("onboarding_step") or 0)
    except (TypeError, ValueError):
        return 0


def save_step(n: int) -> None:
    tconfig.save({"onboarding_step": int(n)})


def windows_ok() -> tuple[bool, str]:
    b = winver.current_build()
    if b is None:
        return True, winver.describe(None)
    return b >= winver.MIN_BUILD, winver.describe(b)


def launch_at_login() -> bool:
    return startup.is_enabled()


def set_launch_at_login(on: bool) -> bool:
    ok = startup.set_enabled(on)
    if ok:
        tconfig.save({"launch_at_login": bool(on)})
    return ok
