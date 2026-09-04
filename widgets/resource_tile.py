"""Small tile widget used in the Performance sidebar (CPU / Memory / Disk / Network)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from widgets.live_chart import LiveChart


class ResourceTile(QWidget):
    def __init__(self, title: str, line_color: str = "#00b7c3", parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("resourceTile")

        self.title_label = QLabel(title)
        self.title_label.setObjectName("tileTitle")
        self.value_label = QLabel("0%")
        self.value_label.setObjectName("tileValue")

        self.chart = LiveChart(max_points=40, line_color=line_color)
        self.chart.setFixedHeight(36)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 5, 8, 5)
        layout.setSpacing(1)
        layout.addWidget(self.title_label)
        layout.addWidget(self.chart)
        layout.addWidget(self.value_label)
        self.setFixedHeight(77)

    def update_value(self, value: float, text: str | None = None) -> None:
        self.chart.add_value(value)
        self.value_label.setText(text if text is not None else f"{value:.0f}%")

    def set_theme(self, dark: bool) -> None:
        self.chart.set_theme(dark)
