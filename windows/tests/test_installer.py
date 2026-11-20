"""install.ps1 / uninstall.ps1 and their Python half (timmy.install_helper). Any OS: the helper commands and the script
rules (UTF-8 BOM for PowerShell 5.1, no PowerShell 7-only syntax, no empty native arguments). Windows: install.ps1
-DryRun runs here; the full install into a temp folder (tiny model, no shortcuts) is the CI job `installer`."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from timmy import config as tconfig
from timmy import install_helper as H

WIN = Path(__file__).resolve().parents[1]
SCRIPTS = [WIN / "install.ps1", WIN / "uninstall.ps1"]


def run_helper(*args):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = H.main(list(args))
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1])


@pytest.mark.parametrize("p", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_powershell_51_safe(p):
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM missing (PowerShell 5.1 would misread the Hebrew)"
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "CRLF line endings"
    s = raw.decode("utf-8-sig")
    assert "#Requires -Version 5.1" in s
    code = "\n".join(ln for ln in s.splitlines() if not ln.strip().startswith("#"))
    for bad in (" ?? ", "?.", " && ", " || ", "-Parallel", "ForEach-Object -Parallel"):
        assert bad not in code, bad
    assert not re.search(r"\$\w+\s*\?\s*[^:\s]+\s*:", code), "ternary operator"
    assert chr(0x2014) not in s


def test_install_script_rules():
    s = (WIN / "install.ps1").read_text(encoding="utf-8-sig")
    for p in ("Name", "About", "People", "OutputDir", "AppDir", "Model", "NoModel", "NoShortcuts", "NoStartup", "NoLaunch", "DryRun"):
        assert re.search(r"\[(string|switch)\]\$" + p + r"\b", s), p
    assert "19041" in s and "Is64BitOperatingSystem" in s and "huggingface.co" in s
    assert '"$($Here)[audio,asr,ui,system]"' in s and "--reinstall-package timmy-windows" in s
    # optional values are only passed when given (PowerShell 5.1 drops empty native arguments)
    for flag in ("--name", "--about", "--people", "--output-dir"):
        assert re.search(r"if \(\$\w+\.Trim\(\)\) \{ \$cargs \+= @\(\"" + flag, s), flag
    assert "output_dir" not in s.split("Helper $cargs")[0].split("Say \"שומר את ההגדרות\"")[1]
    assert "-m timmy --background" not in s           # the Run entry comes from winsys.startup through the helper


def test_native_stderr_never_stops_the_install():
    """QA blocker: on Windows PowerShell 5.1, `& python ... 2>&1` under ErrorActionPreference Stop turns the first stderr
    line (tqdm progress of the model download, a Python warning) into a terminating error, so the install died at
    "מוריד את מודל העברית". Every native call now runs under Continue and progress bars are off."""
    s = (WIN / "install.ps1").read_text(encoding="utf-8-sig")
    helper = s.split("function Helper(")[1].split("\nfunction ")[0]
    assert '$ErrorActionPreference = "Continue"' in helper and "finally { $ErrorActionPreference = $prev }" in helper
    assert '"$_"' in helper                       # ErrorRecords from stderr become plain strings
    assert '$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"' in s
    code = [ln for ln in s.splitlines() if not ln.strip().startswith("#")]
    for ln in code:
        # every uv / python call outside the Helper goes through Native { ... }
        if re.search(r"& \$(uv|Py)\b", ln) and "function Helper" not in ln and "install_helper" not in ln:
            assert "Native {" in ln, ln


def test_helper_writes_nothing_to_stderr(tmp_path):
    """The helper itself keeps stderr empty (progress, warnings, tracebacks go to logs\\install_helper.log): even a failed
    model download prints only its JSON line."""
    env = {**os.environ, "TIMMY_APP_DIR": str(tmp_path / "Timmy"), "HF_HUB_OFFLINE": "1", "HF_HOME": str(tmp_path / "hf"),
           "PYTHONPATH": str(WIN), "PYTHONWARNINGS": "default"}
    r = subprocess.run([sys.executable, "-m", "timmy.install_helper", "model", "--repo", "timmy-test/no-such-model"],
                       capture_output=True, timeout=120, env=env, cwd=WIN)
    assert r.stderr == b"", r.stderr.decode("utf-8", "replace")
    out = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    assert r.returncode == 1 and out["ok"] is False and out["error"]
    assert (tmp_path / "Timmy" / "logs" / "install_helper.log").exists()


def test_install_docs_fetch_main_for_windows():
    """The Windows code is on main: the Windows steps clone main or download main.zip (folder timmy-main)."""
    doc = (WIN.parent / "INSTALL.md").read_text(encoding="utf-8")
    assert "git clone https://github.com/YanivShef/timmy.git $HOME\\Timmy" in doc
    assert "archive/refs/heads/main.zip -OutFile" in doc and "timmy-main $HOME" in doc
    assert "-b windows" not in doc and "heads/windows.zip" not in doc
    assert "-b windows" not in (WIN.parent / "README.md").read_text(encoding="utf-8")


def test_helper_config_rules(app_dir, tmp_path, fake_claude):
    rc, r = run_helper("config", "--name", "רוני", "--about", "סטודיו לעיצוב", "--people", "דנה כהן,\nאבי לוי , ")
    cfg = tconfig.load()
    assert rc == 0 and r["ok"] and cfg["owner_name"] == "רוני" and cfg["people"] == ["דנה כהן", "אבי לוי"]
    assert cfg["owner_description"] == "סטודיו לעיצוב" and cfg["clients_dir"] == "" and cfg["meetings_dir"] == ""
    assert cfg["claude_path"] == str(fake_claude[0]) and cfg["output_dir"].endswith("טימי - הקלטות")
    first = cfg["output_dir"]
    # an update without --output-dir keeps the folder the user chose (also in the onboarding)
    tconfig.save({"output_dir": str(tmp_path / "בחירה שלי"), "accent_color": "#3B82F6"})
    rc, r = run_helper("config")
    cfg = tconfig.load()
    assert cfg["output_dir"] == str(tmp_path / "בחירה שלי") and cfg["owner_name"] == "רוני" and cfg["accent_color"] == "#3B82F6"
    assert cfg["people"] == ["דנה כהן", "אבי לוי"]
    # an explicit --output-dir wins
    rc, r = run_helper("config", "--output-dir", str(tmp_path / "חדשה"))
    assert tconfig.load()["output_dir"] == str(tmp_path / "חדשה") and (tmp_path / "חדשה").is_dir() and first


def test_helper_icon_and_claude(qapp, app_dir, tmp_path, fake_claude):
    ico = tmp_path / "timmy.ico"
    rc, r = run_helper("icon", str(ico))
    assert rc == 0 and ico.exists() and ico.read_bytes()[:4] == b"\x00\x00\x01\x00"      # ICO header
    rc, r = run_helper("claude")
    assert r["path"] == str(fake_claude[0]) and r["logged_in"] is True


def test_helper_startup_and_cleanup(app_dir, monkeypatch):
    from timmy.winsys import startup
    calls = []
    monkeypatch.setattr(startup, "set_enabled", lambda on, name=startup.VALUE: calls.append(on) or True)
    rc, r = run_helper("startup", "on")
    assert rc == 0 and calls == [True] and tconfig.load()["launch_at_login"] is True
    from timmy.integrations import greenolink, secrets
    secrets.use_memory({})
    greenolink.save_store({"access_token": "a"})
    rc, r = run_helper("cleanup")
    assert rc == 0 and calls == [True, False] and not greenolink.has_token()
    secrets._BACKEND.clear()


@pytest.mark.asr
def test_helper_model_tiny(app_dir):
    pytest.importorskip("huggingface_hub")
    from huggingface_hub import scan_cache_dir
    try:
        have = any(r.repo_id == "Systran/faster-whisper-tiny" for r in scan_cache_dir().repos)
    except Exception:
        have = False
    if not have:
        pytest.skip("tiny model not cached (the CI test job caches it)")
    rc, r = run_helper("model", "--repo", "Systran/faster-whisper-tiny")
    assert rc == 0 and r["ok"] and os.path.exists(os.path.join(r["path"], "model.bin"))
    assert tconfig.load()["asr_model"] == "Systran/faster-whisper-tiny"


def _powershell():
    import shutil
    return shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.windows_only
def test_install_dry_run(tmp_path):
    ps = _powershell()
    if not ps:
        pytest.skip("no PowerShell")
    r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WIN / "install.ps1"), "-Name", "בדיקה",
                        "-AppDir", str(tmp_path / "Timmy App"), "-DryRun"], capture_output=True, timeout=120)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")
    assert "==> בודק את המחשב" in out and "DryRun" in out and "✓ ווינדוס build" in out
    assert not (tmp_path / "Timmy App" / "venv").exists()


def test_python_version_gate():
    assert sys.version_info[:2] >= (3, 12)
