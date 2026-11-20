"""engine.files: call file names (Mac rules + Windows rules) and the mp3 mix."""
import os
import re
import wave

import numpy as np
import pytest

from timmy.engine import files as F

MAC_SAFE = lambda t: re.sub(r'[\\/:*?"<>|\n]', "", t).strip()[:60] or "שיחה בכרום"   # noqa: E731  (mac/live.py)


@pytest.mark.parametrize("title", ["קביעת פגישה על הצעת מחיר", "מחיר: 3,000 ש\"ח?", "a/b\\c*d", "", "   ",
                                   "תכנון " * 20, "שיחה <עם> |לקוח|"])
def test_same_as_mac_for_ordinary_titles(title):
    mac = MAC_SAFE(title)
    win = F.safe_title(title)
    assert win == re.sub(r" {2,}", " ", mac).strip().rstrip(". ") or win == F.DEFAULT_TITLE


@pytest.mark.parametrize("title,expect", [
    ("סיכום.", "סיכום"),                  # Explorer cannot open names that end with a dot
    ("סיכום . . ", "סיכום"),
    ("טאב\tעם\rתווים", "טאבעםתווים"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("...", "שיחה בכרום"),
])
def test_windows_rules(title, expect):
    assert F.safe_title(title) == expect


def test_final_base_like_the_mac(tmp_path):
    base = str(tmp_path / "2026-11-20 14-03-07 כרום")
    assert F.final_base(base, "", "שיחת היכרות") == str(tmp_path / "2026-11-20 14-03 שיחת היכרות")
    assert F.final_base(base, "דנה כהן", "שיחת היכרות") == str(tmp_path / "2026-11-20 14-03 דנה כהן - שיחת היכרות")
    assert F.final_base(base, 'דנה "הגדולה"', "") == str(tmp_path / "2026-11-20 14-03 דנה הגדולה - שיחה בכרום")


def test_final_base_stays_under_max_path():
    d = os.path.join("C:" + os.sep, "Users", "Someone", "Documents", *(["תיקייה ארוכה מאוד"] * 9))
    base = os.path.join(d, "2026-11-20 14-03-07 כרום")
    new = F.final_base(base, "שם ארוך של אדם " * 3, "כותרת ארוכה " * 10)
    assert len(new + F.TRANSCRIPT_SUFFIX) < F.MAX_PATH
    assert os.path.dirname(new) == d
    assert os.path.basename(new).startswith("2026-11-20 14-03 ")


def test_unique_base(tmp_path):
    new = str(tmp_path / "2026-11-20 14-03 שיחה")
    own = str(tmp_path / "2026-11-20 14-03-07 כרום")
    assert F.unique_base(new, own) == new
    open(new + ".md", "w").close()
    assert F.unique_base(new, own) == new + " (2)"
    open(new + " (2) - תמלול.txt", "w").close()
    assert F.unique_base(new, own) == new + " (3)"


def test_move_overwrites_and_ignores_missing(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("1")
    b.write_text("2")
    F.move(str(a), str(b))                # os.rename would fail on Windows here
    assert b.read_text() == "1" and not a.exists()
    F.move(str(a), str(b))                # missing source: nothing happens


def test_stamp_of():
    assert F.stamp_of("C:\\x\\2026-11-20 14-03-07 כרום".replace("\\", os.sep)) == "2026-11-20 14-03-07"


def _wav(path, sec, freq):
    t = np.arange(int(16000 * sec)) / 16000
    a = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(a.tobytes())


def test_ffmpeg_from_the_wheel():
    p = F.ffmpeg_path({})
    assert os.path.isfile(p), p
    assert F.ffmpeg_path({"ffmpeg_path": p}) == p
    assert F.ffmpeg_path({"ffmpeg_path": "/nope/ffmpeg.exe"}) == p


def test_mix_mp3_two_channels(tmp_path):
    a, b = tmp_path / "x.ch0.wav", tmp_path / "x.ch1.wav"
    _wav(a, 2.0, 440)
    _wav(b, 3.0, 660)
    out = tmp_path / "שיחה.mp3"
    assert F.mix_mp3(F.ffmpeg_path({}), [str(a), str(b)], str(out))
    assert out.stat().st_size > 10_000          # 3 s at 64 kbit/s is about 24 kB
    one = tmp_path / "one.mp3"
    assert F.mix_mp3(F.ffmpeg_path({}), [str(a)], str(one))
    assert not F.mix_mp3(F.ffmpeg_path({}), [], str(tmp_path / "none.mp3"))
    assert not F.mix_mp3(str(tmp_path / "no-ffmpeg"), [str(a)], str(tmp_path / "z.mp3"))
