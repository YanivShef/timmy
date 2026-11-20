"""procs.py: which chrome.exe processes are browser roots."""
import os
import sys

import pytest

from timmy.audio import procs


def test_roots_from_table():
    table = [
        (4, 0, "System"),
        (100, 50, "explorer.exe"),
        (200, 100, "chrome.exe"),          # browser (profile A)
        (201, 200, "chrome.exe"),          # renderer
        (202, 200, "chrome.exe"),          # audio service utility
        (203, 200, "chrome.exe"),
        (300, 100, "CHROME.EXE"),          # second instance (another --user-data-dir), fewer children
        (301, 300, "chrome.exe"),
        (400, 201, "notepad.exe"),
        (500, 999, "msedge.exe"),
    ]
    assert procs.roots(table) == [200, 300]
    assert procs.roots(table, "msedge.exe") == [500]
    assert procs.roots(table, "firefox.exe") == []


def test_orphaned_child_counts_as_root():
    assert procs.roots([(10, 1, "chrome.exe"), (11, 10, "chrome.exe"), (12, 77, "chrome.exe")]) == [10, 12]


def test_process_table_has_this_process():
    table = procs.process_table()
    mine = [r for r in table if r[0] == os.getpid()]
    assert mine, "current process missing from the process table"
    assert mine[0][1] == os.getppid()
    exe = mine[0][2].lower()
    assert "python" in exe or exe.endswith(".exe") or exe


@pytest.mark.windows_only
def test_toolhelp_matches_psutil():
    if sys.platform != "win32":
        pytest.skip("windows only")
    import psutil
    from timmy.audio.wasapi import toolhelp_processes
    th = {pid: (ppid, name) for pid, ppid, name in toolhelp_processes()}
    me = psutil.Process()
    assert th[me.pid][0] == me.ppid()
    assert th[me.pid][1].lower() == me.name().lower()
    print("processes:", len(th), "chrome roots:", procs.chrome_roots())
