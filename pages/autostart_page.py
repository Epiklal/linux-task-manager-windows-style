"""Autostart tab - lists apps launched automatically at login (XDG autostart .desktop
files under ~/.config/autostart and /etc/xdg/autostart) and lets you enable/disable them."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.system_info import (
    AutostartEntry, StoppableWorker, list_autostart_entries, set_autostart_enabled,
)
from widgets.resizable_table import ResizableRowsTableWidget

COLUMNS = ["Name", "Befehl", "Quelle", "Status"]
_DEFAULT_COLUMN_WIDTHS = [200, 320, 90, 100]


_REMOTE_ACTION_HINT = "Im Remote-Modus zeigt diese Liste Daten des Servers. Aktionen würden auf diesem PC ausgeführt und sind deshalb gesperrt."

class AutostartListWorker(StoppableWorker):
    entriesReady = Signal(list)

    def run(self) -> None:
        entries = list_autostart_entries()
        if not self._stop_event.is_set():
            self.entriesReady.emit(entries)


class AutostartPage(QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Autostart-Einträge suchen...")
        self.search_box.textChanged.connect(self._apply_filter)

        self.toggle_btn = QPushButton("Aktivieren/Deaktivieren")
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.clicked.connect(self._toggle_selected)

        self.refresh_btn = QPushButton("Aktualisieren")
        self.refresh_btn.clicked.connect(self.refresh)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_box)
        top_bar.addStretch(1)
        top_bar.addWidget(self.toggle_btn)
        top_bar.addWidget(self.refresh_btn)

        self.table = ResizableRowsTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        # Interactive on every column (not Stretch) so Befehl can be dragged wider too.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.verticalHeader().setVisible(False)
        saved_widths = self.settings.column_widths("autostart")
        widths = saved_widths if saved_widths and len(saved_widths) == len(COLUMNS) else _DEFAULT_COLUMN_WIDTHS
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().sectionResized.connect(self._save_column_widths)
        self.table.set_default_row_height(
            self.settings.row_height("autostart", self.table.verticalHeader().defaultSectionSize())
        )
        self.table.set_row_height_callback(lambda h: self.settings.set_row_height("autostart", h))
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self.table)

        self._all_entries: list[AutostartEntry] = []
        self.worker: AutostartListWorker | None = None
        self._remote_entries = None
        self.refresh()

    def refresh(self) -> None:
        if self._remote_entries is not None:
            self._on_entries_ready(self._remote_entries)
            return
        if self.worker is not None and self.worker.isRunning():
            return
        self.worker = AutostartListWorker(parent=self)
        self.worker.entriesReady.connect(self._on_entries_ready)
        self.worker.start()

    def _on_entries_ready(self, entries: list[AutostartEntry]) -> None:
        self._all_entries = entries
        self._apply_filter(self.search_box.text())

    def set_remote_autostart(self, entries: list[dict] | None) -> None:
        self._remote_entries = entries
        if entries is not None:
            self._stop_worker()
            self._on_entries_ready([AutostartEntry(**entry) for entry in entries])

    def _apply_filter(self, text: str) -> None:
        text = text.lower()
        filtered = [e for e in self._all_entries if text in e.name.lower()]
        self.table.setRowCount(len(filtered))
        for row_idx, entry in enumerate(filtered):
            name_item = QTableWidgetItem(entry.name)
            name_item.setData(Qt.UserRole, entry.filename)
            self.table.setItem(row_idx, 0, name_item)
            self.table.setItem(row_idx, 1, QTableWidgetItem(entry.command))
            self.table.setItem(row_idx, 2, QTableWidgetItem(entry.source))
            self.table.setItem(row_idx, 3, QTableWidgetItem(
                "Aktiviert" if entry.enabled else "Deaktiviert"
            ))
        self._on_selection_changed()

    def _selected_entry(self) -> AutostartEntry | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        filename = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((e for e in self._all_entries if e.filename == filename), None)

    def _on_selection_changed(self) -> None:
        # Remote entries describe files on the server; toggling would edit local files.
        remote = self._remote_entries is not None
        self.toggle_btn.setEnabled(self._selected_entry() is not None and not remote)
        self.toggle_btn.setToolTip(_REMOTE_ACTION_HINT if remote else "")

    def _save_column_widths(self, *_args) -> None:
        widths = [self.table.columnWidth(col) for col in range(len(COLUMNS))]
        self.settings.set_column_widths("autostart", widths)

    def _toggle_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None or self._remote_entries is not None:
            return
        ok, error = set_autostart_enabled(entry, not entry.enabled)
        if not ok:
            QMessageBox.warning(self, "Fehler", f"Autostart konnte nicht geändert werden:\n{error}")
            return
        self.refresh()

    def _stop_worker(self) -> None:
        if self.worker is None:
            return
        self.worker.entriesReady.disconnect(self._on_entries_ready)
        self.worker.stop()
        self.worker.deleteLater()
        self.worker = None

    def shutdown(self) -> None:
        self._stop_worker()
