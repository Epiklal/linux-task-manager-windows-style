"""Main tab bar that can switch between two looks:

* Windows 10: ordinary tabs across the top (styled by the application QSS).
* Windows 11: a navigation pane on the left - rounded hover/selection "pills", an
  accent indicator on the selected entry and monochrome line icons.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QTabBar, QTabWidget

_NAV_WIDTH = 210
_NAV_ITEM_HEIGHT = 40


def draw_nav_icon(painter: QPainter, kind: str, rect: QRectF, color: QColor) -> None:
    """Draw a 16px Fluent-style line icon for a navigation entry."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color, 1.3)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    x, y, s = rect.left(), rect.top(), rect.width()

    def p(fx: float, fy: float) -> QPointF:
        return QPointF(x + fx * s, y + fy * s)

    if kind == "processes":  # 2x2 app grid
        for fx, fy in ((0.08, 0.08), (0.56, 0.08), (0.08, 0.56), (0.56, 0.56)):
            painter.drawRoundedRect(QRectF(p(fx, fy), p(fx + 0.36, fy + 0.36)), 1.5, 1.5)
    elif kind == "performance":  # pulse line
        painter.drawPolyline([p(0.02, 0.55), p(0.28, 0.55), p(0.4, 0.2), p(0.58, 0.85),
                              p(0.7, 0.45), p(0.98, 0.45)])
    elif kind == "memory":  # RAM module with contacts
        painter.drawRoundedRect(QRectF(p(0.04, 0.28), p(0.96, 0.66)), 1.5, 1.5)
        for fx in (0.22, 0.41, 0.6, 0.79):
            painter.drawLine(p(fx, 0.66), p(fx, 0.84))
    elif kind == "sensors":  # thermometer
        painter.drawRoundedRect(QRectF(p(0.4, 0.04), p(0.6, 0.66)), 2, 2)
        painter.drawEllipse(QRectF(p(0.3, 0.6), p(0.7, 0.98)))
        painter.drawLine(p(0.5, 0.35), p(0.5, 0.72))
    elif kind == "services":  # puzzle-ish: two linked boxes
        painter.drawRoundedRect(QRectF(p(0.06, 0.06), p(0.56, 0.56)), 2, 2)
        painter.drawRoundedRect(QRectF(p(0.44, 0.44), p(0.94, 0.94)), 2, 2)
    elif kind == "docker":  # cube
        painter.drawPolygon([p(0.5, 0.04), p(0.94, 0.27), p(0.94, 0.73), p(0.5, 0.96),
                             p(0.06, 0.73), p(0.06, 0.27)])
        painter.drawPolyline([p(0.06, 0.27), p(0.5, 0.5), p(0.94, 0.27)])
        painter.drawLine(p(0.5, 0.5), p(0.5, 0.96))
    elif kind == "autostart":  # gauge
        path = QPainterPath()
        path.arcMoveTo(QRectF(p(0.04, 0.14), p(0.96, 1.06)), 200)
        path.arcTo(QRectF(p(0.04, 0.14), p(0.96, 1.06)), 200, -220)
        painter.drawPath(path)
        painter.drawLine(p(0.5, 0.6), p(0.74, 0.34))
    elif kind == "benchmarks":  # stopwatch
        painter.drawEllipse(QRectF(p(0.1, 0.16), p(0.9, 0.96)))
        painter.drawLine(p(0.38, 0.04), p(0.62, 0.04))
        painter.drawLine(p(0.5, 0.56), p(0.5, 0.34))
    elif kind == "settings":  # gear
        center = p(0.5, 0.5)
        painter.drawEllipse(center, s * 0.15, s * 0.15)
        teeth = QPainterPath()
        for i in range(16):
            angle = math.pi * 2 * i / 16
            radius = s * (0.46 if i % 2 == 0 else 0.34)
            point = QPointF(center.x() + math.cos(angle) * radius,
                            center.y() + math.sin(angle) * radius)
            if i == 0:
                teeth.moveTo(point)
            else:
                teeth.lineTo(point)
        teeth.closeSubpath()
        painter.drawPath(teeth)
    painter.restore()


class NavTabBar(QTabBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._win11 = False
        self._hover = -1
        self.icon_kinds: list[str] = []
        self.setMouseTracking(True)
        self.set_colors({"nav_bg": "#f3f3f3", "nav_hover": "#eaeaea",
                         "nav_selected": "#eaeaea", "nav_text": "#1a1a1a",
                         "nav_accent": "#005fb8"})

    def set_win11(self, enabled: bool) -> None:
        self._win11 = enabled
        # The pane's top border already separates tabs and page; the base line
        # would show up as a stray light line in the dark theme.
        self.setDrawBase(False)
        self.setExpanding(False)
        self.updateGeometry()
        self.update()

    def set_colors(self, colors: dict) -> None:
        """`colors`: nav_bg / nav_hover / nav_selected / nav_text / nav_accent."""
        self._bg, self._hover_bg = QColor(colors["nav_bg"]), QColor(colors["nav_hover"])
        self._selected_bg = QColor(colors["nav_selected"])
        self._text, self._accent = QColor(colors["nav_text"]), QColor(colors["nav_accent"])
        self.update()

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802 (Qt override)
        if self._win11:
            return QSize(_NAV_WIDTH, _NAV_ITEM_HEIGHT)
        return super().tabSizeHint(index)

    def minimumTabSizeHint(self, index: int) -> QSize:  # noqa: N802 (Qt override)
        return self.tabSizeHint(index)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        hover = self.tabAt(event.position().toPoint())
        if hover != self._hover:
            self._hover = hover
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if not self._win11:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self._bg)
        for index in range(self.count()):
            rect = QRectF(self.tabRect(index)).adjusted(6, 2, -6, -2)
            selected = index == self.currentIndex()
            if selected or index == self._hover:
                painter.setPen(Qt.NoPen)
                painter.setBrush(self._selected_bg if selected else self._hover_bg)
                painter.drawRoundedRect(rect, 4, 4)
            if selected:
                painter.setBrush(self._accent)
                painter.drawRoundedRect(
                    QRectF(rect.left(), rect.center().y() - 8, 3, 16), 1.5, 1.5)
            if index < len(self.icon_kinds):
                icon_rect = QRectF(rect.left() + 14, rect.center().y() - 8, 16, 16)
                draw_nav_icon(painter, self.icon_kinds[index], icon_rect, self._text)
            painter.setPen(self._text)
            painter.setFont(self.font())
            painter.drawText(rect.adjusted(44, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft,
                             self.tabText(index))


class NavTabWidget(QTabWidget):
    """QTabWidget using NavTabBar; `set_win11` moves the tabs to a left pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nav_bar = NavTabBar(self)
        self.setTabBar(self.nav_bar)
        self.setObjectName("mainTabs")

    def set_win11(self, enabled: bool, colors: dict) -> None:
        self.nav_bar.set_colors(colors)
        self.setTabPosition(QTabWidget.West if enabled else QTabWidget.North)
        self.nav_bar.set_win11(enabled)
