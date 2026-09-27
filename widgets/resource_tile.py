"""Small tile widget used in the Performance sidebar (CPU / Memory / Disk / Network)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QWidget

from widgets.live_chart import LiveChart


class ResourceTile(QWidget):
    """Windows 10 style: small framed chart on the left, name / device / value on the right."""

    def __init__(self, title: str, line_color: str = "#00b7c3", detail: str = "", parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("resourceTile")
        # Needed so the QSS background (hover / selected) is painted on a plain QWidget.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("tileTitle")
        # Second line under the name (device / adapter / GPU model), as in Windows 10.
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("tileValue")
        self.detail_label.setVisible(bool(detail))
        self.value_label = QLabel("0%")
        self.value_label.setObjectName("tileValue")

        self.chart = LiveChart(max_points=40, line_color=line_color, show_grid=False)
        self.chart.setFixedSize(66, 48)

        layout = QGridLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(0)
        layout.addWidget(self.chart, 0, 0, 4, 1, Qt.AlignVCenter)
        layout.addWidget(self.title_label, 0, 1)
        layout.addWidget(self.detail_label, 1, 1)
        layout.addWidget(self.value_label, 2, 1)
        layout.setRowStretch(0, 1)
        layout.setRowStretch(3, 1)
        layout.setColumnStretch(1, 1)
        self.setFixedHeight(66)

    def set_detail(self, text: str) -> None:
        if text != self.detail_label.text():
            self.detail_label.setText(text)
            self.detail_label.setVisible(bool(text))

    def update_value(self, value: float, text: str | None = None) -> None:
        self.chart.add_value(value)
        self.value_label.setText(text if text is not None else f"{value:.0f}%")

    def set_theme(self, dark: bool, chart_background: str | None = None) -> None:
        self.chart.set_theme(dark, chart_background)
