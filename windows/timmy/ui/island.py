"""The island: Timmy's face at the top center of the screen that opens into the recording card (Recorder.swift
BubbleView + BubbleHost + NotchPanel).

* rest: a black pill flush with the top edge, one eye on each side, a sound-wave mouth on hover; click = record.
* status: the same pill with an icon on the left wing and a short message on the right ("נשמר", "בוטל", "אין מקום").
* card: drops down while recording / saving. Header strip (always black): stop ("עצור" / "שומר…"), the level
  wave or a mic notice, the timer with a pulsing red dot, the eye (hide / show the live transcript) and x (cancel,
  first click arms a red "למחוק?" for 4 s, a second click after 0.5 s deletes). Body (the user's background
  color): key points on the left, the live transcript with speaker tags on the right. Hidden transcript = the card
  shrinks to the header strip, recording and transcription go on.

The window never takes focus (Qt.WindowDoesNotAcceptFocus + WA_ShowWithoutActivating, no taskbar button), so a
click on Timmy never steals the keyboard from the call. Header controls are painted and hit-tested here (like the
Mac's tapRects); the body uses child widgets. Right click opens the same menu as the tray icon.
"""
from __future__ import annotations

import html
import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from timmy.ui import paint as G
from timmy.ui import theme as T
from timmy.ui.paint import BLACK, WHITE, alpha, font

PILL_W = int(G.NOTCH_W + 2 * (G.MSG_WING_MAX + G.HOVER_GROW) + 2 * G.SHOULDER + 2 * G.SIDE_MARGIN)
PILL_H = G.NOTCH_H + G.MOUTH_H + G.BOTTOM_MARGIN
CARD_W = int(G.CARD_BODY_W + 2 * G.SHOULDER + 2 * G.SIDE_MARGIN)
CARD_H = G.CARD_BODY_H + G.BOTTOM_MARGIN
COMPACT_H = G.COMPACT_BODY_H + G.BOTTOM_MARGIN


def he(s: str) -> str:
    """RLM at the start of every line: a line that starts with Latin text or a digit still reads right to left."""
    return "\u200f" + (s or "").replace("\n", "\n\u200f")


# ---------------------------------------------------------------------------------------------------- small icons
def icon_path(name: str, r: QRectF) -> tuple[QPainterPath, bool]:
    """(path, stroked) for a tiny header icon drawn inside r."""
    p = QPainterPath()
    cx, cy, s = r.center().x(), r.center().y(), min(r.width(), r.height())
    if name in ("eye", "eye.slash"):
        w, h = s * 0.95, s * 0.6
        p.moveTo(cx - w / 2, cy)
        p.quadTo(cx, cy - h, cx + w / 2, cy)
        p.quadTo(cx, cy + h, cx - w / 2, cy)
        p.addEllipse(QPointF(cx, cy), s * 0.16, s * 0.16)
        if name == "eye.slash":
            p.moveTo(cx - w / 2, cy - h * 0.75)
            p.lineTo(cx + w / 2, cy + h * 0.75)
        return p, True
    if name == "xmark":
        d = s * 0.32
        p.moveTo(cx - d, cy - d)
        p.lineTo(cx + d, cy + d)
        p.moveTo(cx + d, cy - d)
        p.lineTo(cx - d, cy + d)
        return p, True
    if name == "trash":
        w, h = s * 0.6, s * 0.62
        p.moveTo(cx - w / 2 - 1, cy - h / 2)
        p.lineTo(cx + w / 2 + 1, cy - h / 2)
        p.moveTo(cx - s * 0.12, cy - h / 2 - 1.5)
        p.lineTo(cx + s * 0.12, cy - h / 2 - 1.5)
        p.addRoundedRect(QRectF(cx - w / 2, cy - h / 2 + 1.5, w, h), 1.2, 1.2)
        return p, True
    if name == "mic":
        p.addRoundedRect(QRectF(cx - s * 0.17, cy - s * 0.45, s * 0.34, s * 0.58), s * 0.17, s * 0.17)
        p.moveTo(cx - s * 0.3, cy - s * 0.05)
        p.quadTo(cx - s * 0.3, cy + s * 0.3, cx, cy + s * 0.3)
        p.quadTo(cx + s * 0.3, cy + s * 0.3, cx + s * 0.3, cy - s * 0.05)
        p.moveTo(cx, cy + s * 0.3)
        p.lineTo(cx, cy + s * 0.48)
        return p, True
    if name == "checkmark":
        p.moveTo(cx - s * 0.32, cy + s * 0.02)
        p.lineTo(cx - s * 0.08, cy + s * 0.26)
        p.lineTo(cx + s * 0.34, cy - s * 0.26)
        return p, True
    if name == "exclamation":
        p.moveTo(cx, cy - s * 0.4)
        p.lineTo(cx, cy + s * 0.08)
        return p, True
    if name == "sparkles":
        def star(x, y, rr):
            p.moveTo(x, y - rr)
            p.quadTo(x, y, x + rr, y)
            p.quadTo(x, y, x, y + rr)
            p.quadTo(x, y, x - rr, y)
            p.quadTo(x, y, x, y - rr)
        star(cx - s * 0.1, cy + s * 0.08, s * 0.38)
        star(cx + s * 0.3, cy - s * 0.28, s * 0.18)
        return p, False
    return p, True


def draw_icon(p: QPainter, name: str, r: QRectF, color: QColor, width: float = 1.6):
    path, stroked = icon_path(name, r)
    p.save()
    if stroked:
        p.setPen(QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
    p.drawPath(path)
    p.restore()


# ---------------------------------------------------------------------------------------------------- card body
class _Rich(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setStyleSheet("background: transparent;")


class _Scroll(QScrollArea):
    """Scroll area that sticks to the bottom while new content arrives (unless the user scrolled up)."""

    def __init__(self, inner: QWidget, fade: QColor, parent=None, bottom_fade: bool = False):
        super().__init__(parent)
        self.setWidget(inner)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.viewport().setAutoFillBackground(False)
        inner.setAutoFillBackground(False)
        self.fade = G.TopFade(fade, 14, self.viewport())
        # the key points box: a line cut at the bottom edge fades out instead of ending mid-line (QA)
        self.fade_bottom = G.TopFade(fade, 18, self.viewport(), bottom=True) if bottom_fade else None
        self._stick = True
        self.verticalScrollBar().rangeChanged.connect(self._range)
        self.verticalScrollBar().valueChanged.connect(self._moved)

    def _moved(self, v):
        sb = self.verticalScrollBar()
        self._stick = v >= sb.maximum() - 4
        self._fades()

    def _range(self, _lo, hi):
        if self._stick:
            self.verticalScrollBar().setValue(hi)
        self._fades()

    def to_bottom_if_stuck(self):
        """After new content: the range may only change on the next layout pass, so scroll once more then."""
        if self._stick:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def set_fade_color(self, c: QColor):
        self.fade.color = c
        if self.fade_bottom:
            self.fade_bottom.color = c
        self._fades()

    def _fades(self):
        sb = self.verticalScrollBar()
        if self.fade_bottom:
            self.fade_bottom.setVisible(sb.value() < sb.maximum())
            self.fade.setVisible(sb.value() > 0)
            self.fade_bottom.update()
        self.fade.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fade.setGeometry(0, 0, self.viewport().width(), 14)
        self.fade.raise_()
        if self.fade_bottom:
            h = self.fade_bottom.height()
            self.fade_bottom.setGeometry(0, self.viewport().height() - h, self.viewport().width(), h)
            self.fade_bottom.raise_()
        self._fades()


class CardBody(QWidget):
    """Key points (220 px, left) + live transcript (right). LTR container like the Mac HStack, RTL content."""

    def __init__(self, rec, look: T.Look, parent=None):
        super().__init__(parent)
        self.rec = rec
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        self.notes = QFrame(self)
        self.notes.setFixedWidth(220)
        nl = QVBoxLayout(self.notes)
        nl.setContentsMargins(12, 10, 12, 10)
        nl.setSpacing(6)
        self.notes_head = _Rich(self.notes)
        nl.addWidget(self.notes_head)
        self.bullets = _Rich()
        self.bullets_scroll = _Scroll(self.bullets, QColor(0, 0, 0, 0), self.notes, bottom_fade=True)
        nl.addWidget(self.bullets_scroll, 1)
        lay.addWidget(self.notes)

        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 6, 0, 0)
        il.setSpacing(2)
        self.text = _Rich(inner)
        il.addWidget(self.text)
        self.dots = G.ThinkingDots(QColor(255, 255, 255))
        il.addWidget(self.dots, 0, Qt.AlignmentFlag.AlignRight)
        il.addStretch(1)
        self.scroll = _Scroll(inner, QColor(0, 0, 0), self)
        lay.addWidget(self.scroll, 1)
        self.restyle(look)

    def restyle(self, look: T.Look):
        self.look = look
        ink = look.q(look.ink)
        self.notes.setStyleSheet(f"QFrame {{ background: {look.css(look.ink, 0.07)}; border-radius: 12px; }}")
        self.scroll.fade.color = look.q(look.bg)
        # the notes panel is ink at 7 % over the card: its fades need that exact opaque color
        self.bullets_scroll.set_fade_color(QColor.fromRgbF(*(look.bg[i] * 0.93 + look.ink[i] * 0.07 for i in range(3))))
        self.dots.ink = ink
        self.dots.animate = look.animations
        self.notes_head.setText(
            f'<div dir="rtl"><span style="color:{look.css(look.amber)}; font-size:11px;">✦</span>&nbsp;'
            f'<span style="color:{look.css(look.ink, 0.5)}; font-size:11px; font-weight:600;">נקודות עיקריות</span></div>')
        self.refresh()

    def refresh(self):
        r, look = self.rec, self.look
        ink = lambda a: look.css(look.ink, a)          # noqa: E731
        amber = look.css(look.amber)
        # key points
        if r.bullets:
            rows = "".join(
                f'<p style="margin:0 0 6px 0; line-height:135%;"><span style="color:{look.css(look.amber, 0.85)}; font-size:8px;">●</span>'
                f'&nbsp;&nbsp;<span style="color:{ink(0.8)}; font-size:12.5px;">{html.escape(b)}</span></p>' for b in r.bullets)
        else:
            rows = f'<p style="color:{ink(0.3)}; font-size:12.5px;">הנקודות יופיעו תוך דקה…</p>'
        self.bullets.setText(f'<div dir="rtl">{rows}</div>')
        self.bullets_scroll.to_bottom_if_stuck()
        # transcript
        parts, prev = [], None
        for ln in r.lines[-300:]:
            parts.append(self._line(ln["speaker"], ln["text"], 0.78 if ln["speaker"] == "me" else 0.95, ln["speaker"] != prev, amber))
            prev = ln["speaker"]
        if r.partial:
            parts.append(self._line(r.partial_speaker, r.partial, 0.45, prev != r.partial_speaker, amber))
        show_dots = bool(r.thinking and not r.partial)
        if show_dots and prev != r.partial_speaker:
            parts.append(self._tag_only(r.partial_speaker, amber))
        if not r.lines and not r.partial and not r.thinking:
            parts.append(f'<p style="color:{ink(0.35)}; font-size:14px;">{"טוען מודל…" if r.loading else "מקשיב…"}</p>')
        self.text.setText('<div dir="rtl">' + "".join(parts) + "</div>")
        self.dots.setVisible(show_dots)

    def _tag(self, sp, amber):
        if sp == "me":
            return f'<span style="color:{amber}; font-size:12px; font-weight:600;">אני&nbsp;&nbsp;</span>'
        return f'<span style="color:{self.look.css(self.look.ink, 0.5)}; font-size:12px; font-weight:600;">צד שני&nbsp;&nbsp;</span>'

    def _line(self, sp, text, a, show_tag, amber):
        tag = self._tag(sp, amber) if show_tag else ""
        return (f'<p style="margin:0 0 8px 0; line-height:140%;">{tag}<span style="color:{self.look.css(self.look.ink, a)}; '
                f'font-size:16px;">{html.escape(text)}</span></p>')

    def _tag_only(self, sp, amber):
        return f'<p style="margin:0;">{self._tag(sp, amber)}</p>'


# ---------------------------------------------------------------------------------------------------- the window
class Island(QWidget):
    menu_requested = Signal(object)          # QPoint (global): the app pops the tray menu here

    def __init__(self, rec, look: T.Look | None = None, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("timmyIsland")
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)     # the Mac header is an LTR HStack; Hebrew text gets RLM
        self.setWindowTitle("טימי")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.rec = rec
        self.look = look or T.current()
        self.hover = False
        self.over_tap: str | None = None
        self.tap_rects: dict[str, QRectF] = {}
        self.wave = [0.0] * G.Waveform.BARS
        self._t0 = time.monotonic()
        self._mode = None
        self.forced_hover = None              # screenshots: force the hover look
        self.blink = G.Blinker(self.update, lambda: self.look.animations)
        self.body = CardBody(rec, self.look, self)
        self.body.hide()
        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._frame)
        rec.changed.connect(self.sync)
        T.bus().changed.connect(self.restyle)
        self.sync()

    # ---- state -> geometry ------------------------------------------------------------------------------------
    def mode(self) -> str:
        if self.rec.is_card:
            return "compact" if self.rec.hide_live else "card"
        return "pill"

    def target_size(self, mode=None):
        mode = mode or self.mode()
        if mode == "card":
            return CARD_W, CARD_H
        if mode == "compact":
            return CARD_W, COMPACT_H
        return PILL_W, PILL_H

    def screen_for_placement(self):
        return QGuiApplication.screenAt(QCursor.pos()) or self.screen() or QGuiApplication.primaryScreen()

    def place(self):
        w, h = self.target_size()
        scr = self.screen_for_placement()
        g = scr.geometry() if scr else None
        x = int(g.x() + (g.width() - w) // 2) if g else 0
        y = g.y() if g else 0
        self.setGeometry(x, y, w, h)

    def sync(self):
        m = self.mode()
        if m != self._mode:
            self._mode = m
            self.place()
            self.body.setVisible(m == "card")
            if m == "card":
                self._layout_body()
            if self.look.animations and self.isVisible():
                self.setWindowOpacity(0.0)
                self._fade_start = time.monotonic()
        if self.rec.phase == "recording":
            last = self.wave[-1]
            self.wave = self.wave[1:] + [min(1.0, last * 0.25 + float(self.rec.level) * 0.75)]
        if self.body.isVisible():
            self.body.refresh()
        self._update_anim()
        self.update()

    def _layout_body(self):
        x0 = (self.width() - (G.CARD_BODY_W + 2 * G.SHOULDER)) / 2 + G.SHOULDER
        self.body.setGeometry(int(x0 + 18), G.NOTCH_H + 12, G.CARD_BODY_W - 36, G.CARD_BODY_H - G.NOTCH_H - 28)

    def restyle(self, look: T.Look):
        self.look = look
        self.body.restyle(look)
        self._update_anim()
        self.update()

    def _update_anim(self):
        need = (self.look.animations and (self.rec.is_card or self._hovering())) or getattr(self, "_fade_start", None) is not None
        if need and not self._anim.isActive():
            self._anim.start()
        elif not need and self._anim.isActive():
            self._anim.stop()

    def _frame(self):
        fs = getattr(self, "_fade_start", None)
        if fs is not None:
            a = min(1.0, (time.monotonic() - fs) / 0.18)
            self.setWindowOpacity(a)
            if a >= 1.0:
                self._fade_start = None
        self.update()
        self._update_anim()

    def _hovering(self) -> bool:
        return self.forced_hover if self.forced_hover is not None else self.hover

    # ---- mouse -----------------------------------------------------------------------------------------------------
    def _shape_rect(self) -> QRectF:
        m = self.mode()
        if m == "pill":
            w = self._pill_width()
            h = G.NOTCH_H + (G.MOUTH_H if (self._hovering() and self.rec.phase != "message") else 0)
            return QRectF((self.width() - w) / 2, 0, w, h)
        bw = G.CARD_BODY_W + 2 * G.SHOULDER
        bh = G.COMPACT_BODY_H if m == "compact" else G.CARD_BODY_H
        return QRectF((self.width() - bw) / 2, 0, bw, bh)

    def enterEvent(self, e):
        self._set_hover(True)

    def leaveEvent(self, e):
        self._set_hover(False)
        self.over_tap = None
        self.update()

    def _set_hover(self, v):
        if self.hover != v:
            self.hover = v
            self._update_anim()
            self.update()

    def hit(self, p: QPointF) -> str | None:
        hits = {k: r for k, r in self.tap_rects.items() if r.width() > 0 and r.adjusted(-6, -6, 6, 6).contains(p)}
        for k, r in hits.items():
            if r.contains(p):
                return k
        if not hits:
            return None
        return min(hits, key=lambda k: math.hypot(hits[k].center().x() - p.x(), hits[k].center().y() - p.y()))

    def mouseMoveEvent(self, e):
        p = e.position()
        inside = self._shape_rect().contains(p)
        self._set_hover(inside)
        t = self.hit(p) if self.rec.is_card else None
        if t != self.over_tap:
            self.over_tap = t
            self.setCursor(Qt.CursorShape.PointingHandCursor if (t or (inside and not self.rec.is_card)) else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, e):
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(e.globalPosition().toPoint())
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        p = e.position()
        if self.rec.is_card:
            self.click(self.hit(p))
        elif self._shape_rect().contains(p):
            self.rec.toggle()

    def click(self, name: str | None):
        if name == "stop":
            self.rec.stop()
        elif name == "eye":
            self.rec.set_hide_live(not self.rec.hide_live)
        elif name == "cancel":
            self.rec.cancel_tapped()

    def contextMenuEvent(self, e):
        e.accept()

    # ---- painting ----------------------------------------------------------------------------------------------
    def _pill_width(self) -> float:
        msg = self.rec.message if self.rec.phase == "message" else None
        grow = G.HOVER_GROW if self._hovering() else 0
        if msg is None:
            w = G.WING_W + grow
        else:
            fm = QFontMetricsF(font(12, QFont.Weight.DemiBold))
            w = min(G.MSG_WING_MAX, max(G.MSG_WING_W, fm.horizontalAdvance(self._msg_text(msg)) + 26)) + grow
        return G.NOTCH_W + 2 * w + 2 * G.SHOULDER

    @staticmethod
    def _msg_text(msg: str) -> str:
        return msg.replace(" ✓", "")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.tap_rects = {}
        if self.mode() == "pill":
            self._paint_pill(p)
        else:
            self._paint_card(p)
        p.end()

    def _eye_color(self) -> QColor:
        return self.look.q(self.look.eye)

    def _paint_pill(self, p: QPainter):
        r = self._shape_rect()
        hov = self._hovering()
        msg = self.rec.message if self.rec.phase == "message" else None
        path = G.notch_path(r, 14 if hov and msg is None else 12)
        G.soft_shadow(p, path, 0.35)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(BLACK)
        p.drawPath(path)
        wing = (r.width() - 2 * G.SHOULDER - G.NOTCH_W) / 2
        lx = r.left() + G.SHOULDER
        rx = r.right() - G.SHOULDER - wing
        cy = G.NOTCH_H / 2
        if msg is None:
            eye = self._eye_color()
            G.draw_eye(p, QPointF(lx + wing / 2, cy), eye, hov, self.blink.closed)
            G.draw_eye(p, QPointF(rx + wing / 2, cy), eye, hov, self.blink.closed)
            if hov:
                G.draw_mouth(p, QPointF(r.center().x(), G.NOTCH_H + (G.MOUTH_H - 4) / 2 - 1), eye, time.monotonic() - self._t0,
                             self.look.animations)
            return
        # status: icon on the left wing, text on the right wing
        ic = QRectF(lx + wing / 2 - 7, cy - 7, 14, 14)
        if "נשמר" in msg:
            draw_icon(p, "checkmark", ic, G.OK_GREEN, 1.9)
        elif msg == "בוטל":
            draw_icon(p, "trash", ic, alpha(WHITE, 0.7), 1.5)
        else:
            acc = self.look.q(self.look.island_accent)
            draw_icon(p, "exclamation", ic, acc, 2.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(acc)
            p.drawEllipse(QPointF(ic.center().x(), ic.center().y() + 14 * 0.36), 1.3, 1.3)
        p.setFont(font(12, QFont.Weight.DemiBold))
        p.setPen(alpha(WHITE, 0.9))
        p.drawText(QRectF(rx, 0, wing - 12, G.NOTCH_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   he(self._msg_text(msg)))

    def _capsule(self, p: QPainter, r: QRectF, color: QColor):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)

    def _paint_card(self, p: QPainter):
        look, rec = self.look, self.rec
        compact = self.mode() == "compact"
        finishing = rec.phase == "finishing"
        r = self._shape_rect()
        path = G.notch_path(r, 16 if compact else 24)
        G.soft_shadow(p, path, 0.22 if (look.bg_is_light and not compact) else 0.35)
        p.save()
        p.setClipPath(path)
        p.fillRect(r, BLACK if compact else look.q(look.bg))
        p.fillRect(QRectF(r.left(), 0, r.width(), G.NOTCH_H), BLACK)
        p.restore()

        left = r.left() + G.SHOULDER + 18
        right = r.right() - G.SHOULDER - 18
        hh = G.NOTCH_H
        ch = min(28, hh - 4)
        now = time.monotonic() - self._t0

        # stop control
        p.setFont(font(12, QFont.Weight.DemiBold))
        fm = QFontMetricsF(p.font())
        label = "שומר…" if finishing else "עצור"
        sw = 10 + 9 + 6 + fm.horizontalAdvance(label) + 10
        sr = QRectF(left, (hh - ch) / 2, sw, ch)
        self._capsule(p, sr, alpha(WHITE, 0.2 if (self.over_tap == "stop" and not finishing) else 0.12))
        if finishing:
            c = QPointF(sr.left() + 10 + 4.5, sr.center().y())
            p.setPen(QPen(alpha(WHITE, 0.85), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRectF(c.x() - 5, c.y() - 5, 10, 10), int(-now * 360 * 16) % (360 * 16), 270 * 16)
            p.setPen(alpha(WHITE, 0.7))
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(G.REC_RED)
            p.drawRoundedRect(QRectF(sr.left() + 10, sr.center().y() - 4.5, 9, 9), 2.5, 2.5)
            p.setPen(alpha(WHITE, 0.9))
            self.tap_rects["stop"] = sr
        p.drawText(QRectF(sr.left() + 25, sr.top(), sw - 25, ch), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

        # wave or notice
        center_l = r.center().x() - G.NOTCH_W / 2
        x = sr.right() + 12
        if rec.notice and not finishing:
            draw_icon(p, "mic", QRectF(x, hh / 2 - 6, 12, 12), look.q(look.island_accent), 1.4)
            p.setFont(font(11, QFont.Weight.Medium))
            p.setPen(alpha(WHITE, 0.85))
            avail = max(20.0, center_l - (x + 17) - 4)
            text = QFontMetricsF(p.font()).elidedText(he(rec.notice), Qt.TextElideMode.ElideRight, avail)
            p.drawText(QRectF(x + 17, 0, avail, hh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            mid = hh / 2
            for v in self.wave:
                v = 0.0 if finishing else v
                h = 3 + v * 11
                p.setBrush(alpha(WHITE, 0.25 if finishing else 0.35 + 0.55 * v))
                p.drawRoundedRect(QRectF(x, mid - h / 2, 2.5, h), 1.25, 1.25)
                x += 5.0

        # right side: [timer][dot] ... [eye][x]
        ih = min(26, hh - 6)
        xr = right
        if not finishing:
            if rec.cancel_armed:
                p.setFont(font(12, QFont.Weight.Bold))
                tw = QFontMetricsF(p.font()).horizontalAdvance("למחוק?")
                cw = 9 + 12 + 4 + tw + 9
                cr = QRectF(xr - cw, (hh - ih) / 2, cw, ih)
                self._capsule(p, cr, alpha(G.REC_RED, 1.0 if self.over_tap == "cancel" else 0.85))
                draw_icon(p, "trash", QRectF(cr.left() + 9, cr.center().y() - 6, 12, 12), WHITE, 1.4)
                p.setPen(WHITE)
                p.drawText(QRectF(cr.left() + 25, cr.top(), tw + 4, ih), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "למחוק?")
            else:
                cr = QRectF(xr - 26, (hh - ih) / 2, 26, ih)
                self._capsule(p, cr, alpha(WHITE, 0.2 if self.over_tap == "cancel" else 0.1))
                draw_icon(p, "xmark", cr.adjusted(7, 6, -7, -6), WHITE if self.over_tap == "cancel" else alpha(WHITE, 0.75), 1.6)
            self.tap_rects["cancel"] = cr
            xr = cr.left() - 7
        er = QRectF(xr - 26, (hh - ih) / 2, 26, ih)
        self._capsule(p, er, alpha(WHITE, 0.2 if self.over_tap == "eye" else 0.1))
        eye_tint = look.q(look.island_accent) if rec.hide_live else (WHITE if self.over_tap == "eye" else alpha(WHITE, 0.75))
        draw_icon(p, "eye.slash" if rec.hide_live else "eye", er.adjusted(6, 5, -6, -5), eye_tint, 1.4)
        self.tap_rects["eye"] = er

        p.setFont(font(15, QFont.Weight.DemiBold))
        timer = f"{rec.elapsed // 60}:{rec.elapsed % 60:02d}"
        tw = QFontMetricsF(p.font()).horizontalAdvance(timer)
        tx = r.center().x() + G.NOTCH_W / 2 + 4
        p.setPen(alpha(WHITE, 0.5 if finishing else 1.0))
        p.drawText(QRectF(tx, 0, tw + 4, hh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, timer)
        if not finishing:
            op = 1.0
            if look.animations:
                op = 0.3 + 0.7 * (0.5 + 0.5 * math.cos(now * math.pi / 0.9))
            G.draw_rec_dot(p, QPointF(tx + tw + 11, hh / 2), 7, op)
