"""`python -m timmy.engine.pdf ...`: runs the vendored mac/pdf.py (timmy/engine/_mac/pdf.py, with the Windows patches
from tools/sync_mac.py) as a script, exactly like the Mac app runs `python pdf.py ...`:

    python -m timmy.engine.pdf "<summary.md>" --out "<file.html>" [--local] [--preview]
    python -m timmy.engine.pdf --design "<request>" [--logo f] [--history f] | --undo | --reset | --snapshot
Output: one JSON line on stdout ({"html", "claude", "footer", "brand", "error", ...}). The UI (ui/pdf_render.py)
prints the HTML to an A4 PDF with QtWebEngine.
"""
import os
import runpy
import sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mac", "pdf.py")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.argv = [SCRIPT] + argv
    try:
        runpy.run_path(SCRIPT, run_name="__main__")
    except SystemExit as e:
        return int(e.code or 0) if not isinstance(e.code, str) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
