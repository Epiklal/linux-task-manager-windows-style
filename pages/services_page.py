"""Services tab - lists systemd services (best-effort; works without systemd too)."""
from __future__ import annotations

import shutil
import subprocess

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

COLUMNS = ["Name", "Beschreibung", "Status"]


class ServiceListWorker(QThread):
    servicesReady = Signal(list)

    def run(self) -> None:
        services = self._fetch()
        self.servicesReady.emit(services)

    def _fetch(self) -> list[tuple[str, str, str]]:
        if not shutil.which("systemctl"):
            return []
        try:
            output = subprocess.check_output(
                [
                    "systemctl", "list-units", "--type=service", "--all",
                    "--no-legend", "--no-pager", "--plain",
                ],
                text=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return []

        rows = []
        for line in output.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            unit, load, active, sub, description = parts
            rows.append((unit, description, f"{active}/{sub}"))
        return rows

class ServicesPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Dienste suchen...")
        self.search_box.textChanged.connect(self._apply_filter)

        self.refresh_btn = QPushButton("Aktualisieren")
        self.refresh_btn.clicked.connect(self.refresh)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_box)
        top_bar.addStretch(1)
        top_bar.addWidget(self.refresh_btn)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        # Unit names like "systemd-journald.service" need room; the description stretches.
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(2, 130)
        self.table.verticalHeader().setVisible(False)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self.table)

        self._all_rows: list[tuple[str, str, str]] = []
        self.worker: ServiceListWorker | None = None
        self._remote_rows = None
        self.refresh()

    def refresh(self) -> None:
        if self._remote_rows is not None:
            self._on_services_ready(self._remote_rows)
            return
        if self.worker is not None and self.worker.isRunning():
            return
        self.worker = ServiceListWorker(parent=self)
        self.worker.servicesReady.connect(self._on_services_ready)
        self.worker.start()

    def _on_services_ready(self, rows: list[tuple[str, str, str]]) -> None:
        self._all_rows = rows
        if not rows:
            QMessageBox.information(
                self, "Dienste",
                "Keine systemd-Dienste gefunden (systemctl nicht verfügbar)."
            )
        self._apply_filter(self.search_box.text())

    def set_remote_services(self, rows: list | None) -> None:
        self._remote_rows = rows
        if rows is not None and self.worker is not None and self.worker.isRunning():
            self.worker.terminate()
        if rows is not None:
            self._on_services_ready(rows)

    def _apply_filter(self, text: str) -> None:
        text = text.lower()
        filtered = [r for r in self._all_rows if text in r[0].lower()]
        self.table.setRowCount(len(filtered))
        for row_idx, (name, desc, status) in enumerate(filtered):
            self.table.setItem(row_idx, 0, QTableWidgetItem(name))
            self.table.setItem(row_idx, 1, QTableWidgetItem(desc))
            self.table.setItem(row_idx, 2, QTableWidgetItem(status))

    def shutdown(self) -> None:
        if self.worker is not None:
            # One-shot listing: let it finish. terminate() on a thread running
            # Python code crashes the interpreter.
            self.worker.wait(3000)
            self.worker.deleteLater()
            self.worker = None
