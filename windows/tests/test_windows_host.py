"""Checks that only make sense on the real Windows runner: the APIs Timmy depends on exist on this OS build."""
import ctypes
import os
import sys

import pytest

from timmy import paths
from timmy.winsys import winver

pytestmark = pytest.mark.windows_only


def test_build_supports_process_loopback():
    b = winver.current_build()
    print("windows build", b, winver.describe(b))
    assert winver.loopback_supported(b)


def test_required_exports_exist():
    if sys.platform != "win32":
        pytest.skip("windows only")
    user32 = ctypes.WinDLL("user32")
    assert hasattr(user32, "SetWindowDisplayAffinity") and hasattr(user32, "GetWindowDisplayAffinity")
    mmdev = ctypes.WinDLL("Mmdevapi")
    assert hasattr(mmdev, "ActivateAudioInterfaceAsync")
    ole32 = ctypes.WinDLL("ole32")
    assert hasattr(ole32, "CoInitializeEx")


def test_known_folders(monkeypatch):
    monkeypatch.delenv("TIMMY_APP_DIR", raising=False)
    assert paths.documents_dir().exists()
    assert str(paths.app_dir()).lower().startswith(os.environ["LOCALAPPDATA"].lower())
