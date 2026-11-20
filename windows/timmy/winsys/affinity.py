"""Hide Timmy's windows from screen sharing and screenshots (the Mac's `sharingType = .none`).

SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE) (Windows 10 2004 / build 19041+): the window stays visible on
the user's own screen but is left out of every capture that goes through the compositor: Chrome / Meet "share entire
screen" (Windows Graphics Capture / DXGI duplication), the Snipping Tool, PrintScreen and BitBlt of the desktop.
On older builds the call fails and we leave the window alone (WDA_MONITOR would show a black box, which is worse
than showing Timmy).

The UI applies it to every top-level window through one application event filter (timmy.ui.app.ShareHider), so
menus, tooltips and dialogs are covered too. Off Windows every function is a harmless no-op (returns False / None).
"""
import sys

IS_WIN = sys.platform == "win32"

WDA_NONE = 0x00
WDA_MONITOR = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11

_user32 = None


def _u32():
    global _user32
    if _user32 is None and IS_WIN:
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.SetWindowDisplayAffinity.restype = wintypes.BOOL
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.GetWindowDisplayAffinity.restype = wintypes.BOOL
        u.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        _user32 = u
    return _user32


def supported() -> bool:
    from timmy.winsys import winver
    return IS_WIN and winver.exclude_from_capture_supported()


def set_hidden(hwnd: int, hide: bool = True) -> bool:
    """WDA_EXCLUDEFROMCAPTURE (hide) or WDA_NONE (show in shares). True when Windows accepted it."""
    if not hwnd or not supported():
        return False
    return bool(_u32().SetWindowDisplayAffinity(int(hwnd), WDA_EXCLUDEFROMCAPTURE if hide else WDA_NONE))


def get(hwnd: int):
    """The window's current affinity (0, 1 or 0x11), or None when it cannot be read (not Windows, bad window)."""
    if not hwnd or not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    v = wintypes.DWORD()
    if not _u32().GetWindowDisplayAffinity(int(hwnd), ctypes.byref(v)):
        return None
    return int(v.value)


def last_error() -> int:
    if not IS_WIN:
        return 0
    import ctypes
    return ctypes.get_last_error()
