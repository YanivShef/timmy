"""Where Timmy keeps its files on Windows.

App dir (config.json, memory.json, chats, pdf/, brand/, models): %LOCALAPPDATA%\\Timmy, the Windows twin of
~/Library/Application Support/Timmy. TIMMY_APP_DIR overrides it, exactly like on the Mac (tests use it).
Recordings default to Documents\\טימי - הקלטות (same folder name as the Mac).
"""
import os
import sys
from pathlib import Path

APP_NAME = "Timmy"
RECORDINGS_FOLDER = "טימי - הקלטות"


def app_dir() -> Path:
    env = os.environ.get("TIMMY_APP_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    # non-Windows dev/CI hosts: a harmless private folder, never the Mac app's real directory
    return Path.home() / ".timmy-windows-dev"


def documents_dir() -> Path:
    """The user's Documents folder (follows OneDrive / a moved Documents folder via the shell known-folder API)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            if shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                try:
                    return Path(out.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:
            pass
    return Path.home() / "Documents"


def default_output_dir() -> Path:
    return documents_dir() / RECORDINGS_FOLDER


def config_path() -> Path:
    return app_dir() / "config.json"


def memory_path() -> Path:
    return app_dir() / "memory.json"
