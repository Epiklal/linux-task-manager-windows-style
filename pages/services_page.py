"""Services tab - lists systemd services (best-effort; works without systemd too)."""
from __future__ import annotations

import shutil
import subprocess

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.system_info import StoppableWorker, WorkerStopped

COLUMNS = ["Name", "Beschreibung", "Status"]


class ServiceListWorker(StoppableWorker):
    servicesReady = Signal(list)

    def run(self) -> None:
        try:
            services = self._fetch()
        except WorkerStopped:
            return
        if not self._stop_event.is_set():
            self.servicesReady.emit(services)

    def _fetch(self) -> list[tuple[str, str, str]]:
        if not shutil.which("systemctl"):
            return []
        try:
            output = self._run_command(
                [
                    "systemctl", "list-units", "--type=service", "--all",
                    "--no-legend", "--no-pager", "--plain",
                ],
                timeout=5,
                check=True,
            ).stdout
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

        # Shown instead of a modal dialog when there is nothing to list: without
        # systemd (containers, Alpine, Devuan, ...) a dialog would pop up on every
        # start and refresh, and headless (tests, offscreen) it would block forever.
        self.hint_label = QLabel("Keine systemd-Dienste gefunden (systemctl nicht verfügbar).")
        self.hint_label.setWordWrap(True)
        self.hint_label.hide()

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
        layout.addWidget(self.hint_label)
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
        self.hint_label.setVisible(not rows)
        self._apply_filter(self.search_box.text())

    def set_remote_services(self, rows: list | None) -> None:
        self._remote_rows = rows
        if rows is not None:
            self._stop_worker()
            self._on_services_ready(rows)

    def _apply_filter(self, text: str) -> None:
        text = text.lower()
        filtered = [r for r in self._all_rows if text in r[0].lower()]
        self.table.setRowCount(len(filtered))
        for row_idx, (name, desc, status) in enumerate(filtered):
            self.table.setItem(row_idx, 0, QTableWidgetItem(name))
            self.table.setItem(row_idx, 1, QTableWidgetItem(desc))
            self.table.setItem(row_idx, 2, QTableWidgetItem(status))

    def _stop_worker(self) -> None:
        if self.worker is None:
            return
        self.worker.servicesReady.disconnect(self._on_services_ready)
        self.worker.stop()
        self.worker.deleteLater()
        self.worker = None

    def shutdown(self) -> None:
        self._stop_worker()
