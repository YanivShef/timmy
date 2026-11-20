import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]     # repo root
WIN = ROOT / "windows"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
OUT = WIN / "out"                               # CI uploads this folder (logs, screenshots, junit)


def pytest_collection_modifyitems(config, items):
    skip_win = pytest.mark.skip(reason="needs a Windows host")
    for item in items:
        if "windows_only" in item.keywords and sys.platform != "win32":
            item.add_marker(skip_win)


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """A private TIMMY_APP_DIR for each test: never the user's real Timmy folder."""
    d = tmp_path / "Timmy"
    d.mkdir()
    monkeypatch.setenv("TIMMY_APP_DIR", str(d))
    return d


@pytest.fixture(scope="session")
def out_dir():
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT


@pytest.fixture(scope="session")
def repo_root():
    return ROOT


os.environ.setdefault("PYTHONUTF8", "1")


def make_fake_claude(folder: Path) -> Path:
    """A runnable `claude` that runs tests/fake_claude.py: claude.cmd on Windows (exercises the cmd.exe path of
    timmy.engine.claude, like npm's shim), an executable sh script elsewhere."""
    folder.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve().parent / "fake_claude.py"
    if sys.platform == "win32":
        p = folder / "claude.cmd"
        p.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        p = folder / "claude"
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        p.chmod(0o755)
    return p


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """TIMMY_CLAUDE_SEARCH -> a fake claude in a folder with a space in its name (like C:\\Users\\Dana Levi\\...).
    Returns (path, log file)."""
    p = make_fake_claude(tmp_path / "claude bin")
    logf = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("TIMMY_CLAUDE_SEARCH", str(p))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(logf))
    return p, logf


def claude_calls(logf: Path) -> list:
    import json
    if not logf.exists():
        return []
    return [json.loads(x) for x in logf.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---- Qt (build step 3) ---------------------------------------------------------------------------------------------
SCREENS = OUT / "screens"          # PNG screenshots of every UI state, uploaded by CI


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the session, offscreen unless QT_QPA_PLATFORM says otherwise (CI sets offscreen)."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TIMMY_INSTANCE_NAME", f"timmy-test-{os.getpid()}")
    from timmy.ui.app import make_app
    app = make_app(["pytest"])
    yield app


@pytest.fixture
def screens(qapp):
    SCREENS.mkdir(parents=True, exist_ok=True)
    return SCREENS


def wait_until(qapp, cond, timeout=20.0, step=0.02):
    """Run the Qt event loop until cond() is true (or the timeout). Returns cond()."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(step)
    qapp.processEvents()
    return bool(cond())


def save_shot(widget, path, backdrop="#3a4a5c"):
    """widget.grab() over an opaque backdrop (the island is translucent), saved as PNG. Returns the QImage."""
    from PySide6.QtGui import QColor, QPainter, QPixmap
    pm = widget.grab()
    canvas = QPixmap(pm.size())
    canvas.setDevicePixelRatio(pm.devicePixelRatio())
    canvas.fill(QColor(backdrop))
    p = QPainter(canvas)
    p.drawPixmap(0, 0, pm)
    p.end()
    canvas.save(str(path))
    return canvas.toImage()
