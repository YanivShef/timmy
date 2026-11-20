"""Entry point: `python -m timmy` (or `pythonw -m timmy` from the shortcut).

Build step 3 replaces the body with the real Qt app (timmy.ui.app.run). Until then it only reports the environment,
so the installer and CI can already call it.
"""
import json
import sys

from timmy import __version__, paths
from timmy.winsys import winver


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(__version__)
        return 0
    info = {
        "version": __version__,
        "python": sys.version.split()[0],
        "app_dir": str(paths.app_dir()),
        "windows_build": winver.current_build(),
        "process_loopback": winver.loopback_supported(),
        "exclude_from_capture": winver.exclude_from_capture_supported(),
    }
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
