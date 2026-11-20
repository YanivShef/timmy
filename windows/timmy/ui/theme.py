"""Timmy's look, ported 1:1 from Recorder.swift `Theme` (config keys accent_color, eye_color, background_color,
animations, the same defaults and the same contrast rules), plus the Qt side: colors and one generated stylesheet.

Rules (Mac):
* any background is allowed, also white: the text color (ink) flips to near-black by itself on a light background;
* the accent is drawn in a version that is visible on what it sits on (contrast >= 3:1, lightened on dark and
  darkened on light backgrounds, in steps of 4%), text on an accent fill is black or white, whichever reads better;
* the pill (and the card's header strip) stays black, so the eyes / mouth are made visible on black;
* green / red text are darkened on light backgrounds (okInk / errInk).
The pure functions below have no Qt dependency (tests compare them with the Swift numbers); `Look` turns the
config into QColors and a stylesheet, and `bus()` tells open windows to restyle when the look changes.
"""
from __future__ import annotations

import re

from timmy import config as tconfig

DEFAULT_ACCENT = "#FFA046"
DEFAULT_BG = "#000000"
BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)
INK_DARK = (0.07, 0.07, 0.09)
REC_RED = (1.0, 0.27, 0.27)
OK_GREEN = (0.188, 0.82, 0.345)     # #30D158
ERR_RED = (1.0, 0.35, 0.35)

RGB = tuple[float, float, float]


def rgb(hex_str) -> RGB | None:
    """'#RGB' / '#RRGGBB' (with or without #) -> (r, g, b) in 0..1, or None."""
    h = str(hex_str or "").strip().replace("#", "")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return None
    v = int(h, 16)
    return ((v >> 16) & 0xFF) / 255, ((v >> 8) & 0xFF) / 255, (v & 0xFF) / 255


def to_hex(c: RGB) -> str:
    return "#%02X%02X%02X" % tuple(int(round(max(0.0, min(1.0, x)) * 255)) for x in c)


def readable_background(hex_str) -> str:
    """Mac readableBackground: any color is allowed; only normalized to #RRGGBB (invalid -> black)."""
    c = rgb(hex_str)
    return to_hex(c) if c else DEFAULT_BG


def luminance(c: RGB) -> float:
    def lin(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def contrast(a: RGB, b: RGB) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def is_light(bg: RGB) -> bool:
    return contrast(bg, BLACK) > contrast(bg, WHITE)


def visible(c: RGB, on: RGB, ratio: float = 3.0) -> RGB:
    """Mac Theme.visible: move c toward black (light bg) or white (dark bg) in 4% steps until contrast >= ratio."""
    if contrast(c, on) >= ratio:
        return c
    toward = BLACK if contrast(on, BLACK) > contrast(on, WHITE) else WHITE
    t, out = 0.0, c
    while t < 1 and contrast(out, on) < ratio:
        t += 0.04
        out = tuple(c[i] + (toward[i] - c[i]) * t for i in range(3))
    return out


class Look:
    """The current look, from config.json. Every color the UI paints comes from here."""

    def __init__(self, cfg: dict | None = None):
        cfg = tconfig.load() if cfg is None else cfg
        self.accent_hex = str(cfg.get("accent_color") or DEFAULT_ACCENT)
        self.eye_hex = str(cfg.get("eye_color") or "")
        self.bg_hex = readable_background(cfg.get("background_color") or DEFAULT_BG)
        a = cfg.get("animations")
        self.animations = True if a is None else bool(a)

    # ---- the Swift computed properties -------------------------------------------------------------------------
    @property
    def bg(self) -> RGB:
        return rgb(self.bg_hex) or BLACK

    @property
    def accent(self) -> RGB:
        return rgb(self.accent_hex) or rgb(DEFAULT_ACCENT)

    @property
    def bg_is_light(self) -> bool:
        return is_light(self.bg)

    def accent_on(self, bg: RGB) -> RGB:
        return visible(self.accent, bg)

    @property
    def amber(self) -> RGB:
        """The accent on the window background (Mac `amber`)."""
        return self.accent_on(self.bg)

    @property
    def island_accent(self) -> RGB:
        return self.accent_on(BLACK)

    @property
    def on_accent(self) -> RGB:
        a = self.amber
        return BLACK if contrast(a, BLACK) >= contrast(a, WHITE) else WHITE

    @property
    def eye(self) -> RGB:
        return visible(rgb(self.eye_hex) or self.accent, BLACK)

    @property
    def ink(self) -> RGB:
        return INK_DARK if self.bg_is_light else WHITE

    @property
    def ok_ink(self) -> RGB:
        return visible(OK_GREEN, self.bg, 3.2)

    @property
    def err_ink(self) -> RGB:
        return visible(ERR_RED, self.bg, 4.0)

    @property
    def scrim_alpha(self) -> float:
        return 0.28 if self.bg_is_light else 0.55

    @property
    def is_default(self) -> bool:
        return self.accent_hex == DEFAULT_ACCENT and not self.eye_hex and self.bg_hex == DEFAULT_BG and self.animations

    # ---- Qt ----------------------------------------------------------------------------------------------------
    def q(self, c: RGB, alpha: float = 1.0):
        from PySide6.QtGui import QColor
        q = QColor.fromRgbF(c[0], c[1], c[2])
        q.setAlphaF(max(0.0, min(1.0, alpha)))
        return q

    def css(self, c: RGB, alpha: float = 1.0) -> str:
        return "rgba(%d,%d,%d,%.3f)" % (round(c[0] * 255), round(c[1] * 255), round(c[2] * 255), alpha)

    def stylesheet(self) -> str:
        """One stylesheet for every Timmy window (not the pill/card, which paint themselves). Object names used:
        QPushButton#primary (accent fill), #ghost (outline), #chip; QLabel#h1 / #muted / #accent / #ok / #err;
        QFrame#panel (soft fill); QLineEdit#search."""
        ink, bg, amber, on = self.ink, self.bg, self.amber, self.on_accent
        c = self.css
        return f"""
* {{ font-family: "Segoe UI", "Segoe UI Variable Text", "Arial", sans-serif; }}
QWidget#timmyWindow, QDialog, QMessageBox {{ background: {c(bg)}; color: {c(ink)}; }}
QLabel {{ color: {c(ink)}; background: transparent; }}
QLabel#h1 {{ font-size: 24px; font-weight: 700; }}
QLabel#h2 {{ font-size: 20px; font-weight: 700; }}
QLabel#muted {{ color: {c(ink, 0.55)}; }}
QLabel#faint {{ color: {c(ink, 0.4)}; }}
QLabel#accent {{ color: {c(amber)}; font-weight: 600; }}
QLabel#ok {{ color: {c(self.ok_ink)}; }}
QLabel#err {{ color: {c(self.err_ink)}; }}
QFrame#panel {{ background: {c(ink, 0.05)}; border-radius: 12px; }}
QFrame#sidebar {{ background: {c(ink, 0.035)}; border-radius: 14px; }}
QFrame#rule {{ background: {c(ink, 0.08)}; max-height: 1px; min-height: 1px; }}
QPushButton {{ color: {c(ink, 0.88)}; background: {c(ink, 0.08)}; border: none; border-radius: 16px;
              padding: 7px 14px; font-size: 13px; font-weight: 600; }}
QPushButton:hover {{ background: {c(ink, 0.14)}; }}
QPushButton:disabled {{ color: {c(ink, 0.35)}; background: {c(ink, 0.05)}; }}
QPushButton#primary {{ color: {c(on)}; background: {c(amber)}; }}
QPushButton#primary:hover {{ background: {c(amber, 0.85)}; }}
QPushButton#ghost {{ color: {c(amber)}; background: transparent; border: 1px solid {c(amber, 0.5)}; }}
QPushButton#small {{ padding: 3px 10px; font-size: 12px; border-radius: 11px; background: {c(ink, 0.1)}; }}
QPushButton#icon {{ padding: 0px; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
                   border-radius: 14px; background: {c(ink, 0.06)}; color: {c(ink, 0.6)}; }}
QPushButton#icon:hover {{ background: {c(ink, 0.12)}; color: {c(ink, 0.95)}; }}
QPushButton#link {{ background: transparent; color: {c(amber)}; padding: 0px 4px; }}
QLineEdit {{ color: {c(ink)}; background: {c(ink, 0.07)}; border: 1px solid transparent; border-radius: 10px;
            padding: 8px 12px; font-size: 13px; selection-background-color: {c(amber, 0.45)}; }}
QLineEdit:focus {{ border: 1px solid {c(amber, 0.8)}; }}
QLineEdit#title {{ font-size: 20px; font-weight: 700; padding: 6px 12px; }}
QPlainTextEdit {{ color: {c(ink)}; background: {c(ink, 0.07)}; border: 1px solid transparent; border-radius: 10px;
                 padding: 6px 8px; font-size: 14px; selection-background-color: {c(amber, 0.45)}; }}
QPlainTextEdit:focus {{ border: 1px solid {c(amber, 0.8)}; }}
QCheckBox {{ color: {c(ink, 0.88)}; font-size: 13px; spacing: 8px; background: transparent; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border-radius: 10px; margin: 2px 0px; }}
QListWidget::item:selected {{ background: {c(ink, 0.1)}; }}
QListWidget::item:hover:!selected {{ background: {c(ink, 0.05)}; }}
QTextBrowser {{ background: transparent; border: none; color: {c(ink)}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c(ink, 0.18)}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QComboBox {{ color: {c(ink)}; background: {c(ink, 0.08)}; border: none; border-radius: 12px; padding: 4px 12px;
            font-weight: 600; }}
QComboBox QAbstractItemView {{ background: {c(bg)}; color: {c(ink)}; selection-background-color: {c(amber, 0.35)}; }}
QMenu {{ background: {c(bg)}; color: {c(ink)}; border: 1px solid {c(ink, 0.12)}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c(amber, 0.3)}; }}
QMenu::item:disabled {{ color: {c(ink, 0.4)}; }}
QMenu::separator {{ height: 1px; background: {c(ink, 0.1)}; margin: 4px 8px; }}
QToolTip {{ background: {c(bg)}; color: {c(ink)}; border: 1px solid {c(ink, 0.2)}; padding: 4px; }}
"""


_BUS = []


def bus():
    """A QObject with `changed(Look)`: windows restyle themselves when the look changes (the Mac themeChanged)."""
    if not _BUS:
        from PySide6.QtCore import QObject, Signal

        class Bus(QObject):
            changed = Signal(object)

        _BUS.append(Bus())
    return _BUS[0]


def current() -> Look:
    return Look()


def apply(changes: dict, save: bool = True) -> Look:
    """Mac Theme.apply: accent_color / eye_color / background_color / animations / reset. Saves to config.json
    (all four keys, like the Mac) and tells every window."""
    cfg = tconfig.load()
    look = Look(cfg)
    if changes.get("reset") is True:
        look.accent_hex, look.eye_hex, look.bg_hex, look.animations = DEFAULT_ACCENT, "", DEFAULT_BG, True
    if isinstance(changes.get("accent_color"), str):
        look.accent_hex = changes["accent_color"]
    if isinstance(changes.get("eye_color"), str):
        look.eye_hex = changes["eye_color"]
    if isinstance(changes.get("background_color"), str):
        look.bg_hex = readable_background(changes["background_color"])
    if isinstance(changes.get("animations"), bool):
        look.animations = changes["animations"]
    if save:
        tconfig.save({"accent_color": look.accent_hex, "eye_color": look.eye_hex, "background_color": look.bg_hex,
                      "animations": look.animations})
    if _BUS:
        _BUS[0].changed.emit(look)
    return look
