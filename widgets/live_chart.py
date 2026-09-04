"""Simple rolling line-chart widget (no extra plotting dependency)."""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import Property, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class LiveChart(QWidget):
    def __init__(self, max_points: int = 60, max_value: float = 100.0,
                 line_color: str = "#00b7c3", fill: bool = True, parent=None):
        super().__init__(parent)
        self._max_points = max_points
        self._max_value = max_value
        self._data: deque[float] = deque([0.0] * max_points, maxlen=max_points)
        self._line_color = QColor(line_color)
        self._fill = fill
        self._bg_color = QColor("#ffffff")
        self._grid_color = QColor("#e0e0e0")
        self._grid_phase = 0.0
        self.setMinimumHeight(80)

    def add_value(self, value: float) -> None:
        self._data.append(max(0.0, min(value, self._max_value)))
        self.advance_grid()
        self.update()

    def advance_grid(self) -> None:
        """Move the vertical grid one sample to the left, like Task Manager."""
        self._grid_phase = (self._grid_phase + 1 / (self._max_points - 1)) % 1.0

    def set_max_value(self, value: float) -> None:
        self._max_value = max(value, 0.0001)

    def set_line_color(self, color: str) -> None:
        self._line_color = QColor(color)
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

    def set_data(self, data: deque[float]) -> None:
        """Point the chart at an externally-owned rolling buffer (shared, not copied)."""
        self._data = data
        self.update()

    def set_theme(self, dark: bool) -> None:
        self._bg_color = QColor("#191919" if dark else "#ffffff")
        self._grid_color = QColor("#303030" if dark else "#e0e0e0")
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        painter.fillRect(rect, self._bg_color)

        # grid lines
        grid_pen = QPen(self._grid_color)
        grid_pen.setWidth(1)
        painter.setPen(grid_pen)
        for i in range(1, 4):
            y = rect.height() * i / 4
            painter.drawLine(0, int(y), rect.width(), int(y))
        grid_spacing = rect.width() / 12
        offset = (self._grid_phase * rect.width()) % grid_spacing
        for i in range(-1, 13):
            x = i * grid_spacing - offset
            painter.drawLine(int(x), 0, int(x), rect.height())

        if len(self._data) < 2:
            return

        step_x = rect.width() / (self._max_points - 1)
        path = QPainterPath()
        points = []
        for i, value in enumerate(self._data):
            x = i * step_x
            y = rect.height() - (value / self._max_value) * rect.height()
            points.append((x, y))
        path.moveTo(*points[0])
        for x, y in points[1:]:
            path.lineTo(x, y)

        if self._fill:
            fill_path = QPainterPath(path)
            fill_path.lineTo(points[-1][0], rect.height())
            fill_path.lineTo(points[0][0], rect.height())
            fill_path.closeSubpath()
            fill_color = QColor(self._line_color)
            fill_color.setAlpha(60)
            painter.fillPath(fill_path, fill_color)

        pen = QPen(self._line_color)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawPath(path)
