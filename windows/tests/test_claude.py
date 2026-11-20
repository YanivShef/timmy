"""engine.claude: finding Claude Code on Windows and running `claude -p` (prompt on stdin, no console, tree kill)."""
import os
import sys
import tempfile
import time

import pytest

from tests.conftest import claude_calls
from timmy.engine import claude as C
from timmy.winsys import jobs


def test_config_path_comes_first(fake_claude, tmp_path):
    path, _ = fake_claude
    other = tmp_path / "elsewhere" / path.name
    other.parent.mkdir()
    other.write_bytes(path.read_bytes())
    other.chmod(0o755)
    assert C.find_claude({}) == str(path)
    assert C.find_claude({"claude_path": str(other)}) == str(other)


def test_missing_candidates_are_skipped(monkeypatch, tmp_path, fake_claude):
    path, _ = fake_claude
    monkeypatch.setenv("TIMMY_CLAUDE_SEARCH", os.pathsep.join([str(tmp_path / "nope" / "claude.exe"), str(path)]))
    assert C.find_claude({"claude_path": str(tmp_path / "gone.exe")}) == str(path)


def test_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMMY_CLAUDE_SEARCH", str(tmp_path / "missing"))
    assert C.find_claude({}) == ""
    assert C.run("", "שלום") == ""


def test_windows_needs_a_runnable_extension(monkeypatch, tmp_path):
    """npm puts an extensionless sh script `claude` next to claude.cmd; CreateProcess cannot start it."""
    monkeypatch.setattr(C, "IS_WIN", True)
    sh = tmp_path / "claude"
    sh.write_text("#!/bin/sh\n")
    cmd = tmp_path / "claude.cmd"
    cmd.write_text("@echo off\r\n")
    ps1 = tmp_path / "claude.ps1"
    ps1.write_text("")
    assert not C.runnable(str(sh))
    assert not C.runnable(str(ps1))
    assert C.runnable(str(cmd))


def test_windows_default_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "IS_WIN", True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Dana Levi"))
    monkeypatch.setenv("HOME", str(tmp_path / "Dana Levi"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Dana Levi" / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Dana Levi" / "AppData" / "Local"))
    monkeypatch.setattr(C.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path / "Dana Levi"), 1))
    monkeypatch.setattr(C.shutil, "which", lambda n: None)
    c = C.default_candidates()
    tail = [os.path.relpath(x, tmp_path / "Dana Levi").replace("\\", "/") for x in c]
    assert tail[:4] == [".local/bin/claude.exe", ".claude/local/claude.exe", ".claude/local/claude.cmd",
                        "AppData/Roaming/npm/claude.cmd"]


def test_desktop_app_copy_newest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "IS_WIN", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    for v in ("2.0.9", "2.0.10", "1.9.99"):
        p = tmp_path / "Roaming" / "Claude" / "claude-code" / v / "claude.exe"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"MZ")
    got = [os.path.basename(os.path.dirname(p)) for p in C.desktop_claude()]
    assert got == ["2.0.10", "2.0.9", "1.9.99"]


def test_cmd_shim_command_line(monkeypatch):
    """A .cmd runs through cmd.exe /d /s /c with the whole line quoted once more, so a path with spaces survives."""
    monkeypatch.setattr(C, "IS_WIN", True)
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")
    line = C.command(r"C:\Users\Dana Levi\AppData\Roaming\npm\claude.cmd", ["-p", "--model", "haiku"])
    assert line == r'"C:\Windows\System32\cmd.exe" /d /s /c ""C:\Users\Dana Levi\AppData\Roaming\npm\claude.cmd" -p --model haiku"'
    with pytest.raises(ValueError):
        C.command(r"C:\x\claude.cmd", ["-p", "a & calc"])
    assert C.command(r"C:\x\claude.exe", ["-p"]) == [r"C:\x\claude.exe", "-p"]


def test_run_sends_the_prompt_on_stdin(fake_claude):
    path, logf = fake_claude
    out = C.run(C.find_claude({}), "אתה רושם נקודות עיקריות\nשורה עם \"מרכאות\" & סימנים | <>", model="haiku", timeout=60)
    assert out.splitlines()[0].startswith("- ")
    (call,) = claude_calls(logf)
    assert call["argv"] == ["-p", "--model", "haiku"]
    assert "מרכאות" in call["prompt"] and "& סימנים | <>" in call["prompt"]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(tempfile.gettempdir())


def test_run_timeout_returns_empty_and_kills_claude(fake_claude, monkeypatch):
    path, logf = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "60")
    t0 = time.time()
    assert C.run(str(path), "שלום", model="sonnet", timeout=2) == ""
    assert time.time() - t0 < 30
    (call,) = claude_calls(logf)
    end = time.time() + 10
    while time.time() < end and jobs.pid_alive(call["pid"]):
        time.sleep(0.1)
    assert not jobs.pid_alive(call["pid"]), "the python behind the fake claude (a grandchild via the shim) survived"


@pytest.mark.skipif(sys.platform != "win32", reason="the .cmd path only exists on Windows")
def test_windows_fake_claude_is_a_cmd_shim(fake_claude):
    path, _ = fake_claude
    assert path.suffix == ".cmd" and " " in str(path.parent)
