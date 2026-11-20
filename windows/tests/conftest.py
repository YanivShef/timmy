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
