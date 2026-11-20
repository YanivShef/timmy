"""Finding and running the Claude Code CLI (`claude -p`) on Windows.

Port of mac/live.py `find_claude` / `_desktop_claude` and Recorder.swift `claudeCandidatePaths`, with the places
Claude Code lands on Windows:
  1. config.json `claude_path`
  2. %USERPROFILE%\\.local\\bin\\claude.exe                      native installer (claude.ai/install.ps1)
  3. %USERPROFILE%\\.claude\\local\\claude.exe / claude.cmd        "claude migrate-installer" local install
  4. %APPDATA%\\npm\\claude.cmd                                    npm install -g @anthropic-ai/claude-code
  5. Volta / bun / scoop shims
  6. whatever `where claude` finds on PATH (only real executables: .exe / .cmd / .bat)
  7. the copy inside the Claude desktop app (%APPDATA%\\Claude\\claude-code\\<version>\\...\\claude.exe), newest first,
     searched again every time (the folder changes with every app update, like on the Mac)
TIMMY_CLAUDE_SEARCH (os.pathsep-separated paths) replaces 2..7 (tests point it at a fake claude).

Running: prompts always go through stdin (never the command line), no console window, cwd = the temp folder
(the Mac code uses /tmp), and the child plus everything it starts lives in a Job Object, so a timeout kills node
and its children too (winsys.jobs). `.cmd` shims run through `cmd.exe /d /s /c` with fixed arguments only.
"""
import glob
import os
import re
import shutil
import sys
import tempfile

from timmy.winsys import jobs

IS_WIN = sys.platform == "win32"
RUNNABLE = (".exe", ".cmd", ".bat", ".com")


def _ver(p: str):
    m = re.search(r"claude-code[\\/](\d+)\.(\d+)\.(\d+)", p)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def desktop_claude() -> list[str]:
    """Claude Code bundled with the Claude desktop app (the Code tab), newest version first."""
    out = []
    if IS_WIN:
        for env in ("APPDATA", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if not root:
                continue
            for pat in ("Claude/claude-code/*/claude.exe", "Claude/claude-code/*/*/claude.exe",
                        "Claude/claude-code/*/*/*/claude.exe", "AnthropicClaude/claude-code/*/claude.exe"):
                out += glob.glob(os.path.join(root, pat))
    else:
        base = os.path.expanduser("~/Library/Application Support/Claude/claude-code")
        out += glob.glob(base + "/*/*/claude.app/Contents/MacOS/claude")
    return sorted(set(out), key=_ver, reverse=True)


def default_candidates() -> list[str]:
    home = os.path.expanduser("~")
    if IS_WIN:
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        c = [os.path.join(home, ".local", "bin", "claude.exe"),
             os.path.join(home, ".claude", "local", "claude.exe"),
             os.path.join(home, ".claude", "local", "claude.cmd"),
             os.path.join(appdata, "npm", "claude.cmd"),
             os.path.join(local, "Volta", "bin", "claude.exe"),
             os.path.join(home, ".bun", "bin", "claude.exe"),
             os.path.join(home, "scoop", "shims", "claude.exe"),
             os.path.join(home, "scoop", "shims", "claude.cmd")]
        w = shutil.which("claude")
        if w:
            c.append(w)
        return c + desktop_claude()
    # dev hosts (macOS / Linux): the Mac list
    c = [shutil.which("claude") or "", os.path.join(home, ".claude", "local", "claude"), os.path.join(home, ".local", "bin", "claude"),
         "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    c += sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin", "claude")), reverse=True)
    return c + desktop_claude()


def candidates(cfg: dict | None = None) -> list[str]:
    cfg = cfg or {}
    first = [str(cfg.get("claude_path") or "").strip()]
    env = os.environ.get("TIMMY_CLAUDE_SEARCH")
    rest = [p for p in env.split(os.pathsep)] if env is not None else default_candidates()
    return [os.path.expandvars(os.path.expanduser(p)) for p in first + rest if p and p.strip()]


def runnable(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if IS_WIN:
        # npm also drops an extensionless sh script "claude" next to claude.cmd: CreateProcess cannot run it
        return os.path.splitext(path)[1].lower() in RUNNABLE
    return os.access(path, os.X_OK)


def find_claude(cfg: dict | None = None) -> str:
    """The first Claude Code that exists, or "" (then Timmy only transcribes, no key points and no summary)."""
    return next((c for c in candidates(cfg) if runnable(c)), "")


def command(claude: str, args: list[str]):
    """argv (or, for a .cmd shim, one verbatim cmd.exe command line). `args` are fixed flags, never user text."""
    if IS_WIN and os.path.splitext(claude)[1].lower() in (".cmd", ".bat"):
        for a in args:
            if any(ch in a for ch in '"&|<>^%!\r\n'):
                raise ValueError(f"unsafe argument for a .cmd shim: {a!r}")
        comspec = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
        # an empty argument (pdf.py passes --tools "") must survive as "" or the next flag becomes its value
        inner = " ".join([f'"{claude}"'] + [f'"{a}"' if (" " in a or not a) else a for a in args])
        return f'"{comspec}" /d /s /c "{inner}"'
    return [claude] + list(args)


def run(claude: str, prompt: str, model: str | None = None, timeout: float = 90, extra_args=()) -> str:
    """`claude -p [--model m]` with the prompt on stdin. Returns stdout ("" if Claude is missing, fails to start or
    times out: the callers treat that exactly like the Mac code treats an exception)."""
    if not claude:
        return ""
    args = ["-p"] + (["--model", model] if model else []) + list(extra_args)
    try:
        cmd = command(claude, args)
    except ValueError:
        return ""
    rc, out = jobs.run(cmd, prompt, timeout=timeout, cwd=tempfile.gettempdir())
    if rc is None:
        return ""
    return out
