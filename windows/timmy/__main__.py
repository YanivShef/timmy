"""Entry point: `pythonw -m timmy` (the shortcut and the login item) starts the app.

    python -m timmy                 the app (island + tray); a second launch opens "השיחות שלי" in the running one
    python -m timmy --background    the same, quietly (login item)
    python -m timmy --show-calls    the app with "השיחות שלי" open
    python -m timmy --report        environment report as JSON (installer and CI), no UI
    python -m timmy --version
"""
import json
import sys

from timmy import __version__, paths
from timmy.winsys import winver


def report() -> dict:
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "app_dir": str(paths.app_dir()),
        "windows_build": winver.current_build(),
        "process_loopback": winver.loopback_supported(),
        "exclude_from_capture": winver.exclude_from_capture_supported(),
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(__version__)
        return 0
    if "--report" in argv:
        print(json.dumps(report(), ensure_ascii=False))
        return 0
    from timmy.ui.app import run
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
