"""Which processes to capture: the Chrome browser roots (DESIGN.md 5.1).

Chrome plays sound from its audio-service utility process, a child of the browser process. Process loopback with
INCLUDE_TARGET_PROCESS_TREE on the browser process covers it, so we target every chrome.exe whose parent is not a
chrome.exe (one per running Chrome instance / profile directory), the one with the most children first.
"""
import os
import sys

CHROME_EXE = "chrome.exe"


def process_table():
    """[(pid, ppid, exe name)] for every process. Toolhelp32 on Windows, psutil elsewhere (dev hosts, tests)."""
    if sys.platform == "win32":
        try:
            from timmy.audio.wasapi import toolhelp_processes
            return toolhelp_processes()
        except OSError:
            pass
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            out.append((int(p.info["pid"]), int(p.info["ppid"] or 0), p.info["name"] or ""))
        except (TypeError, ValueError):
            continue
    return out


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower() or (sys.platform != "win32" and os.path.splitext(a)[0].lower() == os.path.splitext(b)[0].lower())


def roots(table, exe: str = CHROME_EXE):
    """PIDs of the `exe` processes whose parent is not an `exe` process, most children first."""
    mine = [(pid, ppid) for pid, ppid, name in table if _same(name, exe)]
    pids = {pid for pid, _ in mine}
    kids = {}
    for _, ppid in mine:
        kids[ppid] = kids.get(ppid, 0) + 1
    top = [pid for pid, ppid in mine if ppid not in pids or ppid == pid]
    return sorted(top, key=lambda p: (-kids.get(p, 0), p))


def chrome_roots(exe: str = CHROME_EXE):
    try:
        return roots(process_table(), exe)
    except Exception:
        return []
