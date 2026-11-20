"""timmy/engine/_mac/ is generated from mac/ by tools/sync_mac.py: it must match, and the generated code must work."""
import importlib
import subprocess
import sys

import numpy as np
import pytest

from tests.conftest import WIN


def test_vendored_mac_code_is_in_sync():
    r = subprocess.run([sys.executable, "tools/sync_mac.py", "--check"], cwd=WIN, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (r.stdout + r.stderr) + "\nmac/ changed: run `python tools/sync_mac.py` in windows/ and commit timmy/engine/_mac/"


def test_patches_fail_loudly_when_mac_changes(monkeypatch):
    sys.path.insert(0, str(WIN / "tools"))
    try:
        sm = importlib.import_module("sync_mac")
    finally:
        sys.path.pop(0)
    monkeypatch.setitem(sm.PATCHES, "pdf.py", [("this text is not in pdf.py", "x", 1)])
    with pytest.raises(sm.SyncError):
        sm.generate()
    monkeypatch.setattr(sm, "LIVE_NAMES", sm.LIVE_NAMES + ["no_such_function"])
    with pytest.raises(sm.SyncError, match="no_such_function"):
        sm.build_live_shared((sm.MAC / "live.py").read_text(encoding="utf-8"))


def test_live_shared_behaves_like_mac():
    from timmy.engine._mac import live_shared as L
    L.configure({})
    assert L.OWNER == "אני" and L.NAMES == {0: "צד שני", 1: "אני"} and L.CUSTOM == [] and L.custom_block("x") == ""
    assert L.SUMMARY_MODEL == "sonnet" and L.BULLETS_MODEL == "haiku"
    L.configure({"owner_name": " דנה ", "custom_instructions": "קצר", "summary_model": "OPUS", "bullets_model": "gpt"})
    assert L.OWNER == "דנה" and L.CUSTOM == ["קצר"] and L.SUMMARY_MODEL == "opus" and L.BULLETS_MODEL == "haiku"
    assert "- קצר" in L.custom_block("נקודות")
    L.configure({})
    assert L.ts(3725) == "01:02:05"
    assert L.dedupe("כן כן כן כן בסדר") == ("כן בסדר", True)
    assert L.clean("אה") == ("", False)
    assert L.final_ok("תודה רבה", {"max_temp": 0, "min_lp": -0.9, "changed": False}) is False
    assert L.final_ok("נקבע פגישה ליום שלישי", {"max_temp": 0, "min_lp": -0.3, "changed": False}) is True
    assert L.text_overlap("שלום לך", "שלום לך חבר") == 1.0
    assert len(L.speech_frames(np.zeros(16000, np.float32))) == 10
    assert "{transcript}" in L.SUMMARY_PROMPT and "{bullets}" in L.PROMPT and "{md}" in L.RESTYLE_PROMPT
    orig = "# t\n\n## סיכום\n\nטקסט\n\n![תמונה](a.png)\n\nתמלול מלא: x"
    assert "![תמונה](a.png)" in L.keep_images(orig, "# t\n\n## סיכום\n\nטקסט אחר\n\nתמלול מלא: x")


def test_vendored_pdf_and_skill_render_patched():
    src = (WIN / "timmy" / "engine" / "_mac" / "pdf.py").read_text(encoding="utf-8")
    assert 'cwd="/tmp"' not in src and "_win.app_dir()" in src
    # build step 3: every macOS-only call of pdf.py goes through timmy/engine/pdf_win.py
    for gone in ("import fcntl", "os.killpg(", ':/usr/bin:/bin', '.split(":")'):
        assert gone not in src, gone
    for there in ("return _win.find_claude(CFG)", "return _win.run(argv", "fcntl = _win.fcntl", "return _win.interpreters(kind)",
                  'env["PATH"] = _win.skill_path(interp)'):
        assert there in src, there
    sr = (WIN / "timmy" / "engine" / "_mac" / "skill_render.py").read_text(encoding="utf-8")
    assert "chrome.exe" in sr and "msedge.exe" in sr
    for name in ("pdf_template.html", "pdf_sample.md"):
        assert (WIN / "timmy" / "engine" / "_mac" / name).read_text(encoding="utf-8").strip()
