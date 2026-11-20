"""Painting pieces of the island, ported from the SwiftUI views in Recorder.swift: NotchShape, TimmyEye, TimmyMouth,
RecDot, Waveform, ThinkingDots, the header icons. Sizes are the Mac ones (points = Qt logical pixels).
"""
from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

# Geometry (Recorder.swift "עיצוב A"). Windows has no notch: the black strip in the middle of the pill is narrower,
# so the two eyes read as one face at the top of the screen.
NOTCH_W = 72
NOTCH_H = 30
SHOULDER = 8.0
SIDE_MARGIN = 26
BOTTOM_MARGIN = 40
WING_W, MSG_WING_W, HOVER_GROW = 46, 84, 6
MSG_WING_MAX = 150
MOUTH_H = 16
CARD_BODY_W = 600
CARD_BODY_H = NOTCH_H + 188
COMPACT_BODY_H = NOTCH_H + 8

REC_RED = QColor.fromRgbF(1.0, 0.27, 0.27)
OK_GREEN = QColor.fromRgbF(0.188, 0.82, 0.345)

FONT_FAMILIES = ["Segoe UI", "Segoe UI Variable Text", "Arial", "Helvetica Neue"]


def font(px: float, weight=QFont.Weight.Normal) -> QFont:
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPixelSize(max(1, int(round(px))))
    f.setWeight(weight)
    return f


def alpha(c: QColor, a: float) -> QColor:
    q = QColor(c)
    q.setAlphaF(max(0.0, min(1.0, a)))
    return q


WHITE = QColor(255, 255, 255)
BLACK = QColor(0, 0, 0)


def notch_path(r: QRectF, bottom_radius: float) -> QPainterPath:
    """Top straight and flush with the screen edge, concave 'shoulders', continuous (squircle-ish) bottom corners."""
    s, w, h = SHOULDER, r.width(), r.height()
    br = max(0.0, min(bottom_radius, (w - 2 * s) / 2, h - s))
    k = 0.32
    x0, y0 = r.left(), r.top()
    p = QPainterPath()
    p.moveTo(x0, y0)
    p.quadTo(x0 + s, y0, x0 + s, y0 + s)
    p.lineTo(x0 + s, y0 + h - br)
    p.cubicTo(x0 + s, y0 + h - br * k, x0 + s + br * k, y0 + h, x0 + s + br, y0 + h)
    p.lineTo(x0 + w - s - br, y0 + h)
    p.cubicTo(x0 + w - s - br * k, y0 + h, x0 + w - s, y0 + h - br * k, x0 + w - s, y0 + h - br)
    p.lineTo(x0 + w - s, y0 + s)
    p.quadTo(x0 + w - s, y0, x0 + w, y0)
    p.closeSubpath()
    return p


def soft_shadow(p: QPainter, path: QPainterPath, strength: float = 0.35, radius: int = 18, dy: float = 10):
    """A cheap blurred shadow under the island: the path stroked with growing pens of falling opacity."""
    p.save()
    p.translate(0, dy)
    steps = 9
    for i in range(steps, 0, -1):
        a = strength * (1 - i / (steps + 1)) / steps * 1.6
        pen = QPen(alpha(BLACK, a), radius * i / steps * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(alpha(BLACK, strength * 0.6))
    p.drawPath(path)
    p.restore()


def draw_glow(p: QPainter, center: QPointF, rx: float, ry: float, color: QColor, glow: float, strength: float = 0.75):
    g = QRadialGradient(center, max(rx, ry) + glow)
    g.setColorAt(0.0, alpha(color, strength * 0.9))
    g.setColorAt(0.45, alpha(color, strength * 0.35))
    g.setColorAt(1.0, alpha(color, 0.0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawEllipse(center, rx + glow, ry + glow)


def draw_eye(p: QPainter, c: QPointF, color: QColor, big: bool, blink: bool):
    w = 11.0 if big else 9.0
    h = 1.5 if blink else (12.0 if big else 10.0)
    draw_glow(p, c, w / 2, h / 2, color, 5 if big else 3.5)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(c.x() - w / 2, c.y() - h / 2, w, h), w / 2, min(w, h) / 2)


MOUTH = [3, 6, 9, 5, 10, 6, 3]


def draw_mouth(p: QPainter, c: QPointF, color: QColor, t: float, animate: bool):
    bw, gap = 2.5, 2.5
    total = len(MOUTH) * bw + (len(MOUTH) - 1) * gap
    x = c.x() - total / 2
    p.setPen(Qt.PenStyle.NoPen)
    for i, h in enumerate(MOUTH):
        if animate:
            ph = 0.5 + 0.5 * math.sin(2 * math.pi * (t / 1.0) - i * 0.38)
            hh = max(2.5, h * (0.45 + 0.55 * ph))
        else:
            hh = h * 0.8
        p.setBrush(alpha(color, 0.9))
        p.drawRoundedRect(QRectF(x, c.y() - hh / 2, bw, hh), bw / 2, bw / 2)
        x += bw + gap


def draw_rec_dot(p: QPainter, c: QPointF, size: float, opacity: float = 1.0):
    draw_glow(p, c, size / 2, size / 2, REC_RED, 2, 0.5 * opacity)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(alpha(REC_RED, opacity))
    p.drawEllipse(c, size / 2, size / 2)


class Blinker:
    """Timmy blinks every 2.8 to 5.5 s for 0.13 s (only with animations on)."""

    def __init__(self, on_change, animations=lambda: True):
        self.closed = False
        self._cb, self._anim = on_change, animations
        self._t = QTimer(singleShot=True)
        self._t.timeout.connect(self._step)
        self._schedule()

    def _schedule(self):
        self._t.start(int(random.uniform(2.8, 5.5) * 1000))

    def _step(self):
        if self.closed:
            self.closed = False
            self._cb()
            self._schedule()
        elif self._anim():
            self.closed = True
            self._cb()
            self._t.start(130)
        else:
            self._schedule()


class Waveform(QWidget):
    """18 bars of rolling level history (Mac Waveform)."""
    BARS = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hist = [0.0] * self.BARS
        self.active = True
        self.setFixedSize(self.BARS * 2.5 + (self.BARS - 1) * 2.5 + 2, 16)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def push(self, level: float):
        last = self.hist[-1]
        self.hist = self.hist[1:] + [min(1.0, last * 0.25 + float(level) * 0.75)]
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        x, mid = 1.0, self.height() / 2
        for v in self.hist:
            v = v if self.active else 0.0
            h = 3 + v * 11
            p.setBrush(alpha(WHITE, (0.35 + 0.55 * v) if self.active else 0.25))
            p.drawRoundedRect(QRectF(x, mid - h / 2, 2.5, h), 1.25, 1.25)
            x += 5.0


class ThinkingDots(QWidget):
    """'מתמלל' + three bouncing dots (Mac ThinkingDots)."""

    def __init__(self, ink: QColor, animate: bool = True, parent=None):
        super().__init__(parent)
        self.ink, self.animate, self.t = ink, animate, 0.0
        self.setFixedHeight(24)
        self.setMinimumWidth(90)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def _tick(self):
        if self.isVisible() and self.animate:
            self.t += 0.05
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(font(13, QFont.Weight.Medium))
        p.setPen(alpha(self.ink, 0.4))
        fm = p.fontMetrics()
        label = "מתמלל"
        w = fm.horizontalAdvance(label)
        right = self.width() - 2
        p.drawText(QRectF(right - w, 0, w, self.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, label)
        x = right - w - 8
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(alpha(self.ink, 0.55))
        for i in range(3):
            off = -3 + 5 * (0.5 + 0.5 * math.sin(2 * math.pi * (self.t / 0.9) - i * 0.33 * math.pi)) if self.animate else 0
            p.drawEllipse(QPointF(x - i * 10, self.height() / 2 + off), 2.5, 2.5)


class TopFade(QWidget):
    """A gradient from the card color to transparent over the top of a scroll area (the Mac mask)."""

    def __init__(self, color: QColor, h: int = 14, parent=None):
        super().__init__(parent)
        self.color = color
        self.setFixedHeight(h)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, self.color)
        g.setColorAt(1, alpha(self.color, 0.0))
        p.fillRect(self.rect(), g)
