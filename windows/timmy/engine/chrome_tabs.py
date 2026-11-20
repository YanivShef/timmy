"""Titles of the active tab of every Chrome window (helps the summary guess who the call is with).

The Mac asks Chrome through AppleScript (`get title of active tab of windows`, result "a, b"). On Windows the
title of a top-level Chrome window is "<active tab title> - Google Chrome", so EnumWindows over visible windows of
chrome.exe gives the same list without any permission. Same output format as the Mac: titles joined by ", ".
"""
import os
import sys

SUFFIXES = (" - Google Chrome", " - Chromium", " - Google Chrome Beta", " - Google Chrome Dev", " - Google Chrome Canary")
# Chrome's own hidden / helper windows that have a title but are not browser windows
IGNORE = {"Google Chrome", "Chrome Legacy Window", "Picture in picture", "תמונה בתוך תמונה"}


def clean_titles(raw) -> list[str]:
    out = []
    for t in raw:
        t = (t or "").strip()
        for s in SUFFIXES:
            if t.endswith(s):
                t = t[: -len(s)].strip()
                break
        if t and t not in IGNORE and t not in out:
            out.append(t)
    return out


def _window_titles(exe: str = "chrome.exe") -> list[str]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    names: dict[int, str] = {}

    def proc_name(pid: int) -> str:
        if pid not in names:
            name = ""
            h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        name = os.path.basename(buf.value).lower()
                finally:
                    k32.CloseHandle(h)
            names[pid] = name
        return names[pid]

    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):      # GW_OWNER: owned popups out
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if proc_name(pid.value) != exe:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            titles.append(buf.value)
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return titles


def chrome_tabs() -> str:
    """"title one, title two" or "" (Chrome closed, not Windows, any error)."""
    if sys.platform != "win32":
        return ""
    try:
        return ", ".join(clean_titles(_window_titles()))
    except Exception:
        return ""
