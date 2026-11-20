"""Opening things outside Timmy: Explorer with a file selected, a file in its default app, a folder, a URL.

TIMMY_DRY_OPEN=1 (tests, like the Mac): nothing opens, each request is appended to <app dir>/dry-open.log as
"reveal: <path>" / "open: <path or url>".
"""
import os
import subprocess
import sys

from timmy import paths

IS_WIN = sys.platform == "win32"


def _dry(line: str) -> bool:
    if not os.environ.get("TIMMY_DRY_OPEN"):
        return False
    try:
        d = paths.app_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "dry-open.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return True


def reveal(path: str) -> None:
    """Explorer with the file selected (Mac: activateFileViewerSelecting)."""
    if _dry("reveal: " + str(path)):
        return
    try:
        if IS_WIN:
            # one verbatim command line: explorer is picky about how /select,"path" is quoted
            subprocess.Popen(f'explorer.exe /select,"{os.path.normpath(path)}"', creationflags=0x08000000)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(str(path))])
    except OSError:
        pass


def open_path(path: str) -> None:
    """A file in its default app (PDF viewer, Notepad for the transcript), or a URL in the browser."""
    if _dry("open: " + str(path)):
        return
    try:
        if IS_WIN:
            os.startfile(str(path))          # noqa: S606 (the user's own file)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


def open_folder(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    open_path(path)
