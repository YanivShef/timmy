"""Launch at login: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, value "Timmy" (the Mac's SMAppService).

Per user, no admin rights. The value is the command line of the windowless interpreter of Timmy's own venv:
    "<venv>\\pythonw.exe" -m timmy --background
`--background` starts Timmy quietly (the pill only, no "השיחות שלי" window), like the Mac login item.
Tests pass their own value name, so the user's real entry is never touched. Off Windows: no-ops that return False.
"""
import os
import subprocess
import sys

IS_WIN = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "Timmy"


def interpreter(python: str | None = None) -> str:
    """pythonw.exe next to the running python.exe (no console window at login); the given/running one otherwise."""
    exe = python or sys.executable
    d, name = os.path.split(exe)
    if name.lower() == "python.exe":
        w = os.path.join(d, "pythonw.exe")
        if os.path.exists(w) or python is not None:
            return w
    return exe


def command_line(python: str | None = None) -> str:
    return f'"{interpreter(python)}" -m timmy --background'


def _key(write: bool):
    import winreg
    access = winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE if write else winreg.KEY_QUERY_VALUE
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access) if write else winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def get(name: str = VALUE):
    """The registered command line, or None."""
    if not IS_WIN:
        return None
    import winreg
    try:
        with _key(False) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return str(v)
    except OSError:
        return None


def is_enabled(name: str = VALUE) -> bool:
    return bool(get(name))


def enable(name: str = VALUE, command: str | None = None) -> bool:
    if not IS_WIN:
        return False
    import winreg
    try:
        with _key(True) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, command or command_line())
        return True
    except OSError:
        return False


def disable(name: str = VALUE) -> bool:
    """True when the value is gone afterwards (also when it was never there)."""
    if not IS_WIN:
        return False
    import winreg
    try:
        with _key(True) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return get(name) is None


def set_enabled(on: bool, name: str = VALUE) -> bool:
    return enable(name) if on else disable(name)


def open_startup_settings() -> None:
    """Settings > Apps > Startup, where the user can also see / switch the entry."""
    if IS_WIN:
        try:
            subprocess.Popen(["explorer.exe", "ms-settings:startupapps"], creationflags=0x08000000)
        except OSError:
            pass
