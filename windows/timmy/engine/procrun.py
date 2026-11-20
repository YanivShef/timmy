"""Running a child process from the UI side (chats, onboarding, WhatsApp drafts): the Mac `runProcess` / `runClaude`.

* every child runs in its own Job Object (winsys.jobs): a timeout or "בטל" kills it and everything it started
  (Claude Code -> node -> its tools), never only the top process;
* no console window (CREATE_NO_WINDOW), cwd = the temp folder, prompts always on stdin;
* CancelToken = the Mac CancelToken: the running process is killed and the next steps do not start;
* ProcResult.ok has the Mac meaning: launched, not timed out, not cancelled, exit 0 and some output.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from timmy.winsys import jobs


class CancelToken:
    def __init__(self):
        self._ev = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def cancel(self) -> None:
        self._ev.set()


@dataclass
class ProcResult:
    status: int = -1
    out: str = ""
    timed_out: bool = False
    launched: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.launched and not self.timed_out and not self.cancelled and self.status == 0 and bool(self.out.strip())


def run_process(cmd, input_text: str | None = None, timeout: float = 60, token: CancelToken | None = None, cwd=None, env=None,
                merge_stderr: bool = False) -> ProcResult:
    r = ProcResult()
    if token is not None and token.cancelled:
        r.cancelled = True
        return r
    try:
        job = jobs.Job()
    except OSError:
        return r
    try:
        try:
            p = job.popen(cmd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
                          cwd=cwd if cwd is not None else tempfile.gettempdir(), env=env)
        except (OSError, ValueError):
            return r
        r.launched = True
        buf = []
        reader = threading.Thread(target=lambda: buf.append(p.stdout.read()), daemon=True)
        reader.start()
        if input_text is not None:
            def write():
                try:
                    p.stdin.write(input_text.encode("utf-8"))
                except OSError:
                    pass
                finally:
                    try:
                        p.stdin.close()
                    except OSError:
                        pass
            threading.Thread(target=write, daemon=True).start()
        deadline = time.time() + timeout
        while p.poll() is None:
            if token is not None and token.cancelled:
                r.cancelled = True
                break
            if time.time() >= deadline:
                r.timed_out = True
                break
            time.sleep(0.05)
        if p.poll() is None:
            job.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        job.kill()          # grandchildren that hold the pipe open must not keep us waiting
        reader.join(timeout=3)
        r.out = (buf[0] if buf else b"").decode("utf-8", "replace")
        r.status = p.returncode if p.returncode is not None else -1
        return r
    finally:
        job.close()


def run_claude(args: list[str], input_text: str | None = None, timeout: float = 120, token: CancelToken | None = None,
               cfg: dict | None = None, merge_stderr: bool = False) -> ProcResult:
    """`claude <args>` with the prompt on stdin (Mac runClaude). Claude missing: a ProcResult that never launched."""
    from timmy import config as tconfig
    from timmy.engine import claude as C
    path = C.find_claude(cfg if cfg is not None else tconfig.load())
    if not path:
        return ProcResult()
    try:
        cmd = C.command(path, list(args))
    except ValueError:
        return ProcResult()
    return run_process(cmd, input_text, timeout=timeout, token=token, merge_stderr=merge_stderr)


def last_json_object(s: str, key: str):
    """The last {...} in s that parses as a JSON object containing `key` (Claude may write text around it)."""
    s = s or ""
    dec = json.JSONDecoder()
    i = s.rfind("{")
    while i >= 0:
        try:
            o, _ = dec.raw_decode(s[i:])
            if isinstance(o, dict) and key in o:
                return o
        except ValueError:
            pass
        i = s.rfind("{", 0, i)
    return None


def split_sections(text: str) -> dict:
    """"===NAME===\\n...\\n===SKILL===\\n..." -> {"NAME": ..., "SKILL": ...} (Mac splitSections, code fences removed)."""
    out, key, buf = {}, None, []
    for line in (text or "").split("\n"):
        t = line.strip()
        if t.startswith("===") and t.endswith("===") and len(t) > 6:
            if key is not None:
                out[key] = "\n".join(buf).strip()
            key, buf = t[3:-3].upper(), []
        elif key is not None:
            buf.append(line)
    if key is not None:
        out[key] = "\n".join(buf).strip()
    for k, v in list(out.items()):
        if v.startswith("```"):
            lines = v.split("\n")[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            out[k] = "\n".join(lines).strip()
    return out
