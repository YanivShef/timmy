"""ui/theme.py against the Mac Theme rules (Recorder.swift): contrast, visible(), light backgrounds, defaults."""
import pytest

from timmy import config as tconfig
from timmy.ui import theme as T


def test_rgb_and_hex():
    assert T.rgb("#FFA046") == pytest.approx((1.0, 160 / 255, 70 / 255))
    assert T.rgb("fff") == (1.0, 1.0, 1.0)
    assert T.rgb("#12") is None and T.rgb("nope") is None
    assert T.to_hex((1.0, 160 / 255, 70 / 255)) == "#FFA046"
    assert T.readable_background("#abc") == "#AABBCC"
    assert T.readable_background("nope") == "#000000" and T.readable_background("bad") == "#BBAADD"
    assert T.readable_background("#FFFFFF") == "#FFFFFF"        # any color is allowed now, also white (Mac)


def test_contrast_matches_wcag():
    assert T.contrast(T.BLACK, T.WHITE) == pytest.approx(21.0)
    assert T.contrast(T.WHITE, T.WHITE) == pytest.approx(1.0)
    assert T.luminance(T.WHITE) == pytest.approx(1.0)
    assert T.is_light(T.WHITE) and not T.is_light(T.BLACK)


def test_visible_moves_toward_readable():
    # black accent on a black background is lightened until 3:1 (Mac: "הדגשה שחורה על רקע שחור מוצגת אפורה")
    v = T.visible(T.BLACK, T.BLACK)
    assert T.contrast(v, T.BLACK) >= 3 and v[0] == pytest.approx(v[1])
    # white on white is darkened
    w = T.visible(T.WHITE, T.WHITE)
    assert T.contrast(w, T.WHITE) >= 3 and w[0] < 1
    # already visible: unchanged
    a = T.rgb("#FFA046")
    assert T.visible(a, T.BLACK) == a


def test_look_defaults_and_dark():
    lk = T.Look({})
    assert lk.accent_hex == "#FFA046" and lk.bg_hex == "#000000" and lk.animations and lk.is_default
    assert lk.ink == T.WHITE and not lk.bg_is_light
    assert lk.on_accent == T.BLACK                     # amber is light: black text on the accent fill
    assert lk.eye == lk.accent                         # eye_color "" = the accent


def test_look_light_background_flips_ink():
    lk = T.Look({"background_color": "#FFFFFF", "accent_color": "#FACC15", "eye_color": "#000000"})
    assert lk.bg_is_light and lk.ink == T.INK_DARK
    assert T.contrast(lk.amber, lk.bg) >= 3            # yellow accent darkened on white
    assert T.contrast(lk.eye, T.BLACK) >= 3            # the eyes sit on the black pill: black eyes are lightened
    assert T.contrast(lk.ok_ink, lk.bg) >= 3.2 and T.contrast(lk.err_ink, lk.bg) >= 4
    css = lk.stylesheet()
    assert "rgba(18,18,23,1.000)" in css               # INK_DARK text
    assert "\u2014" not in css


def test_on_accent_dark_accent_gets_white_text():
    lk = T.Look({"accent_color": "#1E3A8A", "background_color": "#FFFFFF"})
    assert lk.on_accent == T.WHITE


def test_apply_saves_all_four_keys(app_dir):
    tconfig.save({"owner_name": "דנה"})
    lk = T.apply({"background_color": "#fff", "animations": False})
    cfg = tconfig.load()
    assert cfg["owner_name"] == "דנה"                   # unknown keys stay
    assert cfg["background_color"] == "#FFFFFF" and cfg["animations"] is False
    assert cfg["accent_color"] == "#FFA046" and cfg["eye_color"] == ""
    assert lk.bg_is_light
    lk = T.apply({"reset": True})
    assert lk.is_default and tconfig.load()["background_color"] == "#000000"


def test_bus_tells_windows(app_dir, qapp):
    got = []
    T.bus().changed.connect(got.append)
    try:
        T.apply({"accent_color": "#3B82F6"})
    finally:
        T.bus().changed.disconnect(got.append)
    assert got and got[-1].accent_hex == "#3B82F6"
