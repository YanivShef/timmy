"""File names and audio files of a call, the mac/live.py rules plus what Windows needs on top.

Names (mac/live.py end of call, Recorder.swift safeCallTitle):
  "<YYYY-MM-DD HH-MM> [<person> - ]<title>" + ".md" / ".mp3" / " - תמלול.txt"
  title: without \\ / : * ? " < > | and line breaks, at most 60 characters, default "שיחה בכרום".
Windows on top: no control characters, no trailing dots or spaces (Explorer cannot open such files), no reserved
device names (CON, NUL, COM1...), and the full path of the longest file stays under MAX_PATH (260), shortening the
title first and then the person, because long-path support is off on most PCs.
Audio: the two channel WAVs are mixed into one mp3 with ffmpeg, the same filter and bitrate as the Mac. ffmpeg comes
from config `ffmpeg_path`, then the imageio-ffmpeg wheel (installed with Timmy, no PATH needed), then PATH.
"""
import os
import re
import shutil
import subprocess

from timmy.winsys import jobs

MAC_BAD = r'[\\/:*?"<>|\n]'               # exactly what mac/live.py strips
WIN_BAD = r'[\\/:*?"<>|\x00-\x1f\x7f]'     # plus every control character (\r \t ...)
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_PATH = 260
TITLE_MAX = 60
DEFAULT_TITLE = "שיחה בכרום"
TRANSCRIPT_SUFFIX = " - תמלול.txt"         # the longest name a call gets


def _strip_win(s: str) -> str:
    s = re.sub(WIN_BAD, "", s or "")
    s = re.sub(r" {2,}", " ", s)
    return s.strip().rstrip(". ").strip()


def safe_title(title: str, limit: int = TITLE_MAX) -> str:
    """Title as a file-name part (mac: re.sub(MAC_BAD, "", title).strip()[:60] or "שיחה בכרום")."""
    s = _strip_win(re.sub(MAC_BAD, "", title or ""))[:limit]
    s = s.rstrip(". ").strip()
    if s.upper().split(".")[0] in RESERVED:
        s = "_" + s
    return s or DEFAULT_TITLE


def safe_person(person: str) -> str:
    s = _strip_win(person or "")
    if s.upper().split(".")[0] in RESERVED:
        s = "_" + s
    return s


def stamp_of(base: str) -> str:
    """'2026-11-20 14-03-07 כרום' -> '2026-11-20 14-03-07' (mac: basename(base).replace(" כרום", ""))."""
    return os.path.basename(base).replace(" כרום", "")


def final_base(base: str, person: str, title: str, max_path: int = MAX_PATH) -> str:
    """The new base path after the summary (without extension)."""
    d = os.path.dirname(base)
    stamp = stamp_of(base)[:16]
    p = safe_person(person)
    t = safe_title(title)

    def make(p_, t_):
        return os.path.join(d, f"{stamp} {p_ + ' - ' if p_ else ''}{t_}")

    budget = max_path - 1 - len(TRANSCRIPT_SUFFIX)
    new = make(p, t)
    while len(new) > budget and len(t) > 8:
        t = t[:-1].rstrip(". ")
        new = make(p, t)
    while len(new) > budget and p:
        p = p[:-1].rstrip(". ")
        new = make(p, t)
    return new


def unique_base(new: str, own: str) -> str:
    """A second call in the same minute with the same title must not overwrite the first (os.replace would).
    `own` = the call's current base (its own files may be renamed onto themselves)."""
    if os.path.abspath(new) == os.path.abspath(own):
        return new
    exts = (".md", ".mp3", TRANSCRIPT_SUFFIX)
    cand, n = new, 2
    while any(os.path.exists(cand + e) for e in exts):
        cand = f"{new} ({n})"
        n += 1
    return cand


def ffmpeg_path(cfg: dict | None = None) -> str:
    cfg = cfg or {}
    p = str(cfg.get("ffmpeg_path") or "").strip()
    if p and os.path.isfile(p):
        return p
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg") or next((x for x in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(x)), "ffmpeg")


def mix_mp3(ffmpeg: str, wavs: list[str], out: str, timeout: float = 600) -> bool:
    """mac/live.py: ffmpeg -i ch0 -i ch1 -filter_complex amix=inputs=2:duration=longest:normalize=0 -b:a 64k out.mp3"""
    if not wavs:
        return False
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for i in wavs:
        cmd += ["-i", i]
    if len(wavs) == 2:
        cmd += ["-filter_complex", "amix=inputs=2:duration=longest:normalize=0"]
    cmd += ["-b:a", "64k", out]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
                       **jobs.no_window())
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.exists(out)


def move(a: str, b: str) -> None:
    """mac mv(): rename if it exists. os.replace, because os.rename fails on Windows when b exists."""
    if os.path.exists(a) and os.path.abspath(a) != os.path.abspath(b):
        os.replace(a, b)
