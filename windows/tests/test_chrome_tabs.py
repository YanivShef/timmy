"""engine.chrome_tabs: active tab titles from Chrome's window titles (the Mac asks Chrome through AppleScript)."""
import sys

import pytest

from timmy.engine import chrome_tabs as T


def test_clean_titles():
    raw = ["Meet - abc-defg-hij - Google Chrome", "Inbox - Google Chrome", "Google Chrome", "",
           "Meet - abc-defg-hij - Google Chrome", "Calendar - Chromium", "Picture in picture"]
    assert T.clean_titles(raw) == ["Meet - abc-defg-hij", "Inbox", "Calendar"]


def test_same_format_as_the_mac():
    # AppleScript returns a list as "a, b"; the summary prompt gets the same string
    assert ", ".join(T.clean_titles(["Meet - Google Chrome", "וואטסאפ - Google Chrome"])) == "Meet, וואטסאפ"


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_not_windows_is_empty():
    assert T.chrome_tabs() == ""


@pytest.mark.windows_only
def test_windows_enumeration_works():
    """No Chrome on the runner: an empty string, but EnumWindows / QueryFullProcessImageNameW must run cleanly."""
    titles = T._window_titles("explorer.exe")       # explorer windows exist on an interactive runner desktop
    assert isinstance(titles, list)
    assert isinstance(T.chrome_tabs(), str)
