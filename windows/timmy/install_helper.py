"""The Python half of windows/install.ps1 (the PowerShell half only checks the machine, gets uv and the venv):

    python -m timmy.install_helper config --name N [--about A] [--people "a, b"] [--output-dir D]
    python -m timmy.install_helper model [--repo R]        download / verify the transcription model, prints its folder
    python -m timmy.install_helper icon <out.ico>         the Timmy icon for the shortcuts (from assets/timmy.png)
    python -m timmy.install_helper startup on|off         the HKCU Run entry (pythonw -m timmy --background)
    python -m timmy.install_helper claude                 {"path", "logged_in"} of Claude Code, for the last message
    python -m timmy.install_helper cleanup                uninstall.ps1: the Run entry and the WhatsApp tokens (Credential Manager)

config follows mac/install.sh exactly: only values that were given are written, an explicit --output-dir wins, an
output_dir the user already chose is never overwritten, owner_name defaults to "אני", people / clients_dir /
meetings_dir get their defaults, the claude_path and ffmpeg_path that were found are saved. Every command prints one
JSON line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from timmy import config as tconfig
from timmy import paths


def do_config(a) -> dict:
    cfg = tconfig.load()
    upd = {}
    for key, v in (("owner_name", a.name), ("owner_description", a.about)):
        if v and v.strip():
            upd[key] = v.strip()
    out = tconfig.ensure_output_dir(cfg, explicit=a.output_dir)
    upd["output_dir"] = out
    try:
        os.makedirs(os.path.expandvars(os.path.expanduser(out)), exist_ok=True)
    except OSError:
        pass
    if a.people and a.people.strip():
        upd["people"] = [x.strip() for x in a.people.replace("\n", ",").split(",") if x.strip()]
    try:
        from timmy.engine import claude as C
        cp = C.find_claude(cfg)
        if cp:
            upd["claude_path"] = cp
    except Exception:
        pass
    try:
        from timmy.engine import files
        ff = files.ffmpeg_path()
        if ff:
            upd["ffmpeg_path"] = ff
    except Exception:
        pass
    for k, v in (("owner_name", "אני"), ("people", []), ("clients_dir", ""), ("meetings_dir", "")):
        if k not in cfg and k not in upd:
            upd[k] = v
    cfg = tconfig.save(upd)
    return {"ok": True, "owner_name": cfg.get("owner_name"), "output_dir": cfg.get("output_dir"), "config": str(paths.config_path())}


def do_model(a) -> dict:
    from timmy.engine import asr
    cfg = tconfig.load()
    if a.repo:
        cfg = dict(cfg, asr_model=a.repo)
        if a.repo != tconfig.DEFAULTS["asr_model"]:
            tconfig.save({"asr_model": a.repo})
    p = asr.resolve_model(cfg, download=True)
    return {"ok": bool(p) and asr.model_ready(p), "path": str(p)}


def do_icon(a) -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication, QImage
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "assets", "timmy.png")
    img = QImage(src)
    ok = not img.isNull() and img.scaled(256, 256).save(a.out, "ICO")
    return {"ok": bool(ok), "path": a.out}


def do_startup(a) -> dict:
    from timmy.winsys import startup
    on = a.state == "on"
    ok = startup.set_enabled(on)
    if ok:
        tconfig.save({"launch_at_login": on})
    return {"ok": bool(ok), "command": startup.command_line() if on else ""}


def do_claude(_a) -> dict:
    from timmy import onboard
    from timmy.engine import claude as C
    p = C.find_claude(tconfig.load())
    return {"ok": True, "path": p, "logged_in": onboard.auth_status(p) if p else None, "git_bash": onboard.git_bash()}


def do_cleanup(_a) -> dict:
    from timmy.integrations import greenolink, secrets
    from timmy.winsys import startup
    startup.set_enabled(False)
    secrets.delete(greenolink.account())
    return {"ok": True}


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m timmy.install_helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("config")
    c.add_argument("--name", default="")
    c.add_argument("--about", default="")
    c.add_argument("--people", default="")
    c.add_argument("--output-dir", default="")
    m = sub.add_parser("model")
    m.add_argument("--repo", default="")
    i = sub.add_parser("icon")
    i.add_argument("out")
    s = sub.add_parser("startup")
    s.add_argument("state", choices=["on", "off"])
    sub.add_parser("claude")
    sub.add_parser("cleanup")
    a = ap.parse_args(argv)
    try:
        res = {"config": do_config, "model": do_model, "icon": do_icon, "startup": do_startup, "claude": do_claude,
               "cleanup": do_cleanup}[a.cmd](a)
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
