"""Live list of all sensor endpoints readable through Linux sysfs."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.system_info import list_hardware_sensors


class SensorsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.summary = QLabel("")
        self.summary.setObjectName("detailInfo")
        refresh_button = QPushButton("Aktualisieren")
        refresh_button.clicked.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(self.summary)
        top.addStretch(1)
        top.addWidget(refresh_button)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Kategorie", "Sensor", "Wert", "Einheit", "Quelle"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 135)
        self.table.setColumnWidth(1, 240)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 70)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def refresh(self) -> None:
        sensors = list_hardware_sensors()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(sensors))
        for row, sensor in enumerate(sensors):
            values = (
                str(sensor["category"]),
                str(sensor["name"]),
                f"{sensor['value']:.2f}",
                str(sensor["unit"]),
                str(sensor["source"]),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.summary.setText(f"{len(sensors)} lesbare Sensoren")

    def shutdown(self) -> None:
        self.timer.stop()