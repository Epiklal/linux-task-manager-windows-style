"""Simple rolling line-chart widget (no extra plotting dependency)."""
from __future__ import annotations

import math
from collections import deque

from PySide6.QtCore import Property, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


def _blend(color: QColor, background: QColor, amount: float) -> QColor:
    """Mix `amount` (0..1) of `color` into `background`."""
    return QColor(
        round(color.red() * amount + background.red() * (1 - amount)),
        round(color.green() * amount + background.green() * (1 - amount)),
        round(color.blue() * amount + background.blue() * (1 - amount)),
    )


class LiveChart(QWidget):
    def __init__(self, max_points: int = 60, max_value: float = 100.0,
                 line_color: str = "#00b7c3", fill: bool = True, show_grid: bool = True,
                 parent=None):
        super().__init__(parent)
        self._max_points = max_points
        self._max_value = max_value
        self._data: deque[float] = deque([0.0] * max_points, maxlen=max_points)
        self._line_color = QColor(line_color)
        self._fill = fill
        self._show_grid = show_grid
        self._bg_color = QColor("#ffffff")
        self._grid_color = QColor("#e0e0e0")
        self._grid_phase = 0.0
        self._overlay_text = ""
        self._secondary: deque[float] | None = None
        self._auto_scale = False
        self._auto_floor = 1.0
        # Windows 10 look: a frame in the line colour, a grid tinted with the line
        # colour and a thin 1px line - on white (light) or near-black (dark).
        self._win10 = True
        self._dark = False
        self._apply_win10_colors()
        self.setMinimumHeight(40)

    def add_value(self, value: float) -> None:
        value = max(0.0, value)
        self._data.append(value if self._auto_scale else min(value, self._max_value))
        self.advance_grid()
        self.update()

    def advance_grid(self) -> None:
        """Move the vertical grid one sample to the left, like Task Manager."""
        self._grid_phase = (self._grid_phase + 1 / (self._max_points - 1)) % 1.0

    def set_max_value(self, value: float) -> None:
        self._max_value = max(value, 0.0001)

    def set_line_color(self, color: str) -> None:
        self._line_color = QColor(color)
        if self._win10:
            self._apply_win10_colors()
        self.update()

    def _get_line_color(self) -> QColor:
        return self._line_color

    def _set_line_color(self, color: QColor) -> None:
        self._line_color = QColor(color)
        self.update()

    def _get_background_color(self) -> QColor:
        return self._bg_color

    def _set_background_color(self, color: QColor) -> None:
        self._bg_color = QColor(color)
        self.update()

    def _get_grid_color(self) -> QColor:
        return self._grid_color

    def _set_grid_color(self, color: QColor) -> None:
        self._grid_color = QColor(color)
        self.update()

    lineColor = Property(QColor, _get_line_color, _set_line_color)
    backgroundColor = Property(QColor, _get_background_color, _set_background_color)
    gridColor = Property(QColor, _get_grid_color, _set_grid_color)

    def set_auto_scale(self, enabled: bool, floor: float = 1.0) -> None:
        """Scale the y axis to the largest visible value (rounded up to 1/2/5 x 10^n),
        like Task Manager's network graph. `floor` is the smallest axis maximum."""
        self._auto_scale = enabled
        self._auto_floor = floor
        self.update()

    def set_secondary_data(self, data: deque[float] | None) -> None:
        """Optional second series drawn as a dashed line without fill (e.g. "Senden")."""
        self._secondary = data
        self.update()

    def scale_max(self) -> float:
        if not self._auto_scale:
            return self._max_value
        peak = max(self._data, default=0.0)
        if self._secondary:
            peak = max(peak, max(self._secondary))
        peak = max(peak, self._auto_floor)
        magnitude = 10 ** math.floor(math.log10(peak))
        for step in (1, 2, 5, 10):
            if peak <= step * magnitude:
                return step * magnitude
        return peak

    def set_overlay_text(self, text: str) -> None:
        """Small caption drawn inside the chart's top-left corner (e.g. "CPU 3")."""
        self._overlay_text = text
        self.update()

    def set_show_grid(self, show: bool) -> None:
        self._show_grid = show
        self.update()

    def set_data(self, data: deque[float]) -> None:
        """Point the chart at an externally-owned rolling buffer (shared, not copied)."""
        self._data = data
        self.update()

    def set_theme(self, dark: bool, background: str | None = None) -> None:
        """`background` overrides the chart fill (Windows 11 draws charts on the page colour)."""
        self._dark = dark
        self._custom_bg = background
        self._apply_win10_colors()
        self.update()

    def _apply_win10_colors(self) -> None:
        default = "#1c1c1c" if self._dark else "#ffffff"
        self._bg_color = QColor(getattr(self, "_custom_bg", None) or default)
        self._grid_color = _blend(self._line_color, self._bg_color, 0.25 if self._dark else 0.18)

    def _series_path(self, data, rect, max_value: float):
        step_x = rect.width() / (self._max_points - 1)
        offset = self._max_points - len(data)
        points = [
            ((offset + i) * step_x,
             rect.height() - min(value / max_value, 1.0) * rect.height())
            for i, value in enumerate(data)
        ]
        path = QPainterPath()
        path.moveTo(*points[0])
        for x, y in points[1:]:
            path.lineTo(x, y)
        return path, points

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        painter.fillRect(rect, self._bg_color)

        if self._show_grid:
            painter.setRenderHint(QPainter.Antialiasing, False)
            grid_pen = QPen(self._grid_color)
            grid_pen.setWidth(1)
            painter.setPen(grid_pen)
            # Keep grid cells a readable size: 10 x 20 on a big chart, fewer lines on
            # small or flat ones so they don't turn into a dense mesh.
            rows = max(2, min(10, round(rect.height() / 30)))
            columns = max(4, min(20, round(rect.width() / 45)))
            for i in range(1, rows):
                y = rect.height() * i / rows
                painter.drawLine(0, int(y), rect.width(), int(y))
            grid_spacing = rect.width() / columns
            offset = (self._grid_phase * rect.width()) % grid_spacing
            for i in range(-1, columns + 1):
                x = i * grid_spacing - offset
                painter.drawLine(int(x), 0, int(x), rect.height())
            painter.setRenderHint(QPainter.Antialiasing)

        max_value = self.scale_max()
        if len(self._data) >= 2:
            path, points = self._series_path(self._data, rect, max_value)
            if self._fill:
                fill_path = QPainterPath(path)
                fill_path.lineTo(points[-1][0], rect.height())
                fill_path.lineTo(points[0][0], rect.height())
                fill_path.closeSubpath()
                # Translucent so the grid stays visible underneath, as in Task Manager.
                fill_color = QColor(self._line_color)
                fill_color.setAlpha(70 if self._dark else 40)
                painter.fillPath(fill_path, fill_color)

            pen = QPen(self._line_color)
            pen.setWidthF(1.2 if self._win10 else 2)
            painter.setPen(pen)
            painter.drawPath(path)

        if self._secondary is not None and len(self._secondary) >= 2:
            path, _points = self._series_path(self._secondary, rect, max_value)
            pen = QPen(self._line_color)
            pen.setWidthF(1.2)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawPath(path)

        if self._overlay_text:
            painter.setPen(QColor("#d0d0d0" if self._dark else "#333333"))
            painter.drawText(QRectF(rect).adjusted(5, 3, -5, -3),
                             Qt.AlignLeft | Qt.AlignTop, self._overlay_text)

        if self._win10:
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setPen(QPen(self._line_color, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(rect).adjusted(0, 0, -1, -1))
