"""What the vendored mac/pdf.py (timmy/engine/_mac/pdf.py) calls instead of its macOS-only code. The calls are
patched in by tools/sync_mac.py (PATCHES["pdf.py"]); the Mac file itself is unchanged.

  find_claude(cfg)   Claude Code on Windows (engine.claude finder: native exe, npm claude.cmd, desktop app copy)
  run(argv, ...)     pdf.py's run(): no console window, the child and everything it starts in a Job Object,
                     a timeout kills the whole tree (Mac: start_new_session + killpg); `.cmd` shims through cmd.exe
  kill(p)            pdf.py's _kill_children() for one child (Mac: os.killpg)
  fcntl              a tiny flock() shim (msvcrt.locking on Windows, the real fcntl elsewhere)
  interpreters(kind) python / node / bash for the scripts of linked PDF skills, the Windows places included
  skill_path(interp) PATH for a skill script (Mac: /usr/bin:/bin:/opt/homebrew/bin...)
Everything works on any OS, so the vendored module can be tested on the Mac CI host as well.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import time

from timmy.engine import claude as C
from timmy.winsys import jobs

IS_WIN = sys.platform == "win32"


def app_dir() -> str:
    """%LOCALAPPDATA%\\Timmy (a private dev folder off Windows, never the Mac app's real one); TIMMY_APP_DIR wins in pdf.py."""
    from timmy import paths
    return str(paths.app_dir())


def find_claude(cfg: dict | None = None) -> str:
    return C.find_claude(cfg or {})


def _argv(argv):
    """A .cmd / .bat first element (npm's claude.cmd) runs through cmd.exe with checked fixed flags."""
    argv = [str(a) for a in argv]
    if IS_WIN and argv and os.path.splitext(argv[0])[1].lower() in (".cmd", ".bat"):
        return C.command(argv[0], argv[1:])
    return argv


class _Proc:
    """What pdf.py keeps in _CHILDREN: the Popen plus its job."""

    def __init__(self, p, job):
        self.p, self.job, self.pid = p, job, p.pid


def run(argv, input=None, timeout=60, cwd=None, env=None, children=None):
    """subprocess.run(capture_output=True, text=True) semantics; TimeoutExpired is raised after the tree is killed."""
    job = jobs.Job()
    try:
        p = job.popen(_argv(argv), stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, cwd=cwd, env=env)
        holder = _Proc(p, job)
        if children is not None:
            children.add(holder)
        try:
            out, err = p.communicate(None if input is None else input.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            job.kill()
            try:
                p.communicate(timeout=5)
            except Exception:
                pass
            raise
        finally:
            if children is not None:
                children.discard(holder)
        return subprocess.CompletedProcess(argv, p.returncode, (out or b"").decode("utf-8", "replace"),
                                           (err or b"").decode("utf-8", "replace"))
    finally:
        job.kill()
        job.close()


def kill(p) -> None:
    job = getattr(p, "job", None)
    if job is not None:
        job.kill()
        return
    try:
        import signal
        os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


class _Flock:
    LOCK_EX = 2
    LOCK_UN = 8

    @staticmethod
    def flock(f, op):
        if not IS_WIN:
            import fcntl as real
            real.flock(f, real.LOCK_EX if op == _Flock.LOCK_EX else real.LOCK_UN)
            return
        import msvcrt
        f.seek(0)
        if op == _Flock.LOCK_UN:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return
        deadline = time.time() + 120
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)


fcntl = _Flock()


def _existing(cands):
    seen, out = set(), []
    for x in cands:
        if x and os.path.isfile(x) and os.path.normcase(x) not in seen:
            seen.add(os.path.normcase(x))
            out.append(x)
    return out


def interpreters(kind: str) -> list[str]:
    if not IS_WIN:
        if kind == "python":
            c = [sys.executable, "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        elif kind == "node":
            c = [shutil.which("node") or "", "/opt/homebrew/bin/node", "/usr/local/bin/node"]
            c += sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")), reverse=True)
        else:
            c = ["/bin/bash"]
        return _existing(c)
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    if kind == "python":
        d = os.path.dirname(sys.executable)
        c = [os.path.join(d, "python.exe"), sys.executable, shutil.which("python") or "", shutil.which("py") or ""]
        c += sorted(glob.glob(os.path.join(local, "Programs", "Python", "Python3*", "python.exe")), reverse=True)
    elif kind == "node":
        c = [shutil.which("node") or "", os.path.join(pf, "nodejs", "node.exe"), os.path.join(pf86, "nodejs", "node.exe")]
        c += sorted(glob.glob(os.path.join(os.environ.get("APPDATA") or "", "nvm", "v*", "node.exe")), reverse=True)
    else:
        git_bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or ""
        c = [git_bash, os.path.join(pf, "Git", "bin", "bash.exe"), os.path.join(pf86, "Git", "bin", "bash.exe"),
             os.path.join(local, "Programs", "Git", "bin", "bash.exe")]
        # never C:\Windows\System32\bash.exe (WSL): it cannot read Windows paths the way the skill expects
        w = shutil.which("bash") or ""
        if w and "system32" not in w.lower():
            c.append(w)
    return _existing(c)


def skill_path(interp: str) -> str:
    """PATH for a skill script: its interpreter's folder first, then the system folders."""
    if not IS_WIN:
        return os.path.dirname(interp) + ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    parts = [os.path.dirname(interp), os.path.join(root, "System32"), root, os.path.join(root, "System32", "Wbem")]
    return os.pathsep.join(parts + [os.environ.get("PATH", "")])
