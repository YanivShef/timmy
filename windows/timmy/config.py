"""config.json: same file format and keys as the Mac app (so docs, INSTALL.md and Claude edits work the same).

Read with utf-8-sig (a BOM from Notepad must not break it), write atomically, never drop keys we do not know,
and never overwrite an output_dir the user already chose.
"""
import json
import os
import tempfile
from pathlib import Path

from timmy import paths

# Keys the Mac app and mac/live.py / mac/pdf.py read (Recorder.swift, install.sh). Windows uses the same names.
KNOWN_KEYS = (
    "owner_name", "owner_description", "people", "people_file", "people_sources", "clients_dir", "meetings_dir",
    "output_dir", "claude_path", "ffmpeg_path", "summary_model", "bullets_model", "custom_instructions",
    "accent_color", "eye_color", "background_color", "animations", "hide_live_transcript", "hide_in_screen_share",
    "linked_skills", "greenolink_fallback_off",
)
# Windows-only additions (ignored by the Mac app)
WINDOWS_KEYS = ("asr_model", "asr_model_path", "asr_device", "asr_compute_type", "asr_profile", "asr_threads", "launch_at_login",
                "onboarding_step", "onboarding_done")

DEFAULTS = {
    "owner_name": "אני",
    "people": [],
    "clients_dir": "",
    "meetings_dir": "",
    "summary_model": "sonnet",
    "bullets_model": "haiku",
    "accent_color": "#FFA046",
    "eye_color": "",
    "background_color": "#000000",
    "animations": True,
    "hide_in_screen_share": True,
    "asr_model": "ivrit-ai/whisper-large-v3-turbo-ct2",
    "asr_device": "auto",
    "asr_compute_type": "auto",
}


def load(path: Path | None = None) -> dict:
    p = Path(path or paths.config_path())
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get(cfg: dict, key: str):
    v = cfg.get(key)
    return DEFAULTS.get(key) if v is None else v


def save(updates: dict, path: Path | None = None) -> dict:
    """Merge `updates` into config.json (read-modify-write, atomic replace). Returns the new config."""
    p = Path(path or paths.config_path())
    cfg = load(p)
    cfg.update(updates)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return cfg


def ensure_output_dir(cfg: dict, default: Path | None = None, explicit: str | None = None) -> str:
    """The installer rule: an explicit --output-dir wins; otherwise keep what the user already chose;
    only a missing/empty value gets the default."""
    if explicit and explicit.strip():
        return explicit.strip()
    cur = str(cfg.get("output_dir") or "").strip()
    if cur:
        return cur
    return str(default or paths.default_output_dir())
