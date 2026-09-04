"""Processes tab - Windows 10 style process list with live, non-blocking updates."""
from __future__ import annotations

import getpass
import os
import shlex
from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QProcess, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.system_info import ProcessRow, ProcessWorker, kill_process
from widgets.resizable_table import ResizableRowsTableView

COLUMNS = ["Name", "PID", "Benutzer", "Status", "CPU %", "Speicher (MB)", "GPU %",
           "VRAM (MB)", "Threads"]
_DEFAULT_COLUMN_WIDTHS = [220, 70, 110, 90, 70, 110, 70, 100, 70]
# Windows 10 highlights cells of resource-hungry processes with a light orange tint.
_HEATMAP_COLUMNS = {4: 100.0, 5: 4096.0, 6: 100.0, 7: 4096.0}


@dataclass
class ProcessListEntry:
    process: ProcessRow | None = None
    group_key: str | None = None
    label: str = ""
    count: int = 0
    indent: int = 0

    @property
    def is_group(self) -> bool:
        return self.process is None


class ProcessTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._process_rows: list[ProcessRow] = []
        self._rows: list[ProcessListEntry] = []
        self._collapsed_groups: set[str] = set()
        self._filter = ""
        self._sort_column = 4
        self._sort_order = Qt.DescendingOrder
        self._current_user = getpass.getuser()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self._rows[index.row()]
        col = index.column()

        if entry.is_group:
            if role == Qt.DisplayRole and col == 0:
                arrow = "▶" if entry.group_key in self._collapsed_groups else "▼"
                return f"{arrow} {entry.label} ({entry.count})"
            if role == Qt.FontRole:
                font = QFont(QApplication.font())
                font.setBold(True)
                return font
            if role == Qt.ForegroundRole:
                return QColor("#e6e6e6")
            return None

        row = entry.process
        if role == Qt.BackgroundRole and col in _HEATMAP_COLUMNS:
            value = {
                4: row.cpu_percent, 5: row.memory_mb,
                6: row.gpu_percent, 7: row.gpu_vram_mb,
            }[col]
            if value is None:
                return None
            ratio = min(value / _HEATMAP_COLUMNS[col], 1.0)
            if ratio > 0.05:
                # 0 -> transparent, 1 -> saturated Win10 heat-map orange.
                alpha = int(30 + ratio * 150)
                return QColor(253, 165, 84, alpha)
            return None

        if role not in (Qt.DisplayRole, Qt.EditRole, Qt.TextAlignmentRole):
            return None
        if role == Qt.TextAlignmentRole:
            return Qt.AlignLeft | Qt.AlignVCenter
        if col == 0:
            return f"{'    ' * entry.indent}{row.name}"
        if col == 1:
            return row.pid
        if col == 2:
            return row.username
        if col == 3:
            return row.status
        if col == 4:
            return round(row.cpu_percent, 1)
        if col == 5:
            return round(row.memory_mb, 1)
        if col == 6:
            return round(row.gpu_percent, 1) if row.gpu_percent is not None else "–"
        if col == 7:
            return round(row.gpu_vram_mb, 1) if row.gpu_vram_mb is not None else "–"
        if col == 8:
            return row.num_threads
        return None

    def flags(self, index: QModelIndex):
        if index.isValid() and self._rows[index.row()].is_group:
            return Qt.ItemIsEnabled
        return super().flags(index)

    def pid_at(self, row: int) -> int | None:
        if 0 <= row < len(self._rows) and self._rows[row].process is not None:
            return self._rows[row].process.pid
        return None

    def toggle_group(self, row: int) -> bool:
        if not 0 <= row < len(self._rows) or not self._rows[row].is_group:
            return False
        key = self._rows[row].group_key
        if key in self._collapsed_groups:
            self._collapsed_groups.remove(key)
        else:
            self._collapsed_groups.add(key)
        self._rebuild_rows()
        return True

    def set_rows(self, rows: list[ProcessRow]) -> None:
        self._process_rows = rows
        self._rebuild_rows()

    def set_filter(self, value: str) -> None:
        self._filter = value.casefold()
        self._rebuild_rows()

    def set_sort(self, column: int, order: Qt.SortOrder) -> None:
        self._sort_column = column
        self._sort_order = order
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        rows = [row for row in self._process_rows if self._filter in row.name.casefold()]
        key_functions = {
            0: lambda row: row.name.casefold(), 1: lambda row: row.pid,
            2: lambda row: row.username.casefold(), 3: lambda row: row.status.casefold(),
            4: lambda row: row.cpu_percent, 5: lambda row: row.memory_mb,
            6: lambda row: row.gpu_percent if row.gpu_percent is not None else -1,
            7: lambda row: row.gpu_vram_mb if row.gpu_vram_mb is not None else -1,
            8: lambda row: row.num_threads,
        }
        rows.sort(key=key_functions[self._sort_column], reverse=self._sort_order == Qt.DescendingOrder)
        if self._sort_column == 0:
            apps = [row for row in rows if row.username == self._current_user]
            background = [row for row in rows if row.username != self._current_user]
            display_rows = []
            self._append_category(display_rows, "apps", "Apps", apps)
            self._append_category(display_rows, "background", "Hintergrundprozesse", background)
        else:
            display_rows = [ProcessListEntry(process=row) for row in rows]
        self.beginResetModel()
        self._rows = display_rows
        self.endResetModel()

    def _append_category(self, target: list[ProcessListEntry], key: str, label: str,
                         rows: list[ProcessRow]) -> None:
        if not rows:
            return
        category_key = f"category:{key}"
        target.append(ProcessListEntry(group_key=category_key, label=label, count=len(rows)))
        if category_key in self._collapsed_groups:
            return
        rows_by_name: dict[str, list[ProcessRow]] = {}
        for row in rows:
            rows_by_name.setdefault(row.name.casefold(), []).append(row)
        for matching_rows in rows_by_name.values():
            if len(matching_rows) == 1:
                target.append(ProcessListEntry(process=matching_rows[0], indent=1))
                continue
            group_key = f"process:{key}:{matching_rows[0].name.casefold()}"
            target.append(ProcessListEntry(
                group_key=group_key, label=matching_rows[0].name, count=len(matching_rows), indent=1,
            ))
            if group_key not in self._collapsed_groups:
                target.extend(ProcessListEntry(process=row, indent=2) for row in matching_rows)


class ProcessTableView(ResizableRowsTableView):
    """Process table that accepts executable files dropped from the file manager."""

    filesDropped = Signal(list)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()


class ProcessesPage(QWidget):
    statsChanged = Signal(int, int)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

        self.model = ProcessTableModel(self)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Prozesse suchen...")
        self.search_box.textChanged.connect(self.model.set_filter)

        self.end_task_btn = QPushButton("Task beenden")
        self.end_task_btn.setEnabled(False)
        self.end_task_btn.clicked.connect(self._end_selected_task)
        self.start_command_btn = QPushButton("Befehl ausführen")
        self.start_command_btn.clicked.connect(self._start_command)
        self.start_file_btn = QPushButton("Datei starten")
        self.start_file_btn.clicked.connect(self._choose_and_start_file)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_box)
        top_bar.addStretch(1)
        top_bar.addWidget(self.start_command_btn)
        top_bar.addWidget(self.start_file_btn)
        top_bar.addWidget(self.end_task_btn)

        self.table = ProcessTableView()
        self.table.setModel(self.model)
        self.table.setAcceptDrops(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Interactive on every column (not Stretch) so the user can actually drag
        # each column - including Name - wider or narrower; Stretch locked Name's
        # width to "fill remaining space" and ignored manual resize attempts.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.verticalHeader().setVisible(False)
        saved_widths = self.settings.column_widths("processes")
        widths = saved_widths if saved_widths and len(saved_widths) == len(COLUMNS) else _DEFAULT_COLUMN_WIDTHS
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().sectionResized.connect(self._save_column_widths)
        self.table.set_default_row_height(
            self.settings.row_height("processes", self.table.verticalHeader().defaultSectionSize())
        )
        self.table.set_row_height_callback(lambda h: self.settings.set_row_height("processes", h))
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(4, Qt.DescendingOrder)
        header.sectionClicked.connect(self._sort_by_column)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.clicked.connect(self._on_table_clicked)
        self.table.filesDropped.connect(self._start_files)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self.table)

        self._interval = 1.0
        self.worker = ProcessWorker(interval=self._interval, parent=self)
        self.worker.rowsReady.connect(self._on_rows_ready)
        self.worker.start()

    def _on_rows_ready(self, rows: list[ProcessRow]) -> None:
        # Keep the current selection (by PID) across the periodic refresh.
        selected_pid = self._selected_pid()
        self.model.set_rows(rows)
        if selected_pid is not None:
            self._reselect_pid(selected_pid)
        thread_count = sum(r.num_threads for r in rows)
        self.statsChanged.emit(len(rows), thread_count)

    def _selected_pid(self) -> int | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self.model.pid_at(indexes[0].row())

    def _reselect_pid(self, pid: int) -> None:
        for row in range(self.model.rowCount()):
            if self.model.pid_at(row) == pid:
                source_index = self.model.index(row, 0)
                self.table.selectRow(source_index.row())
                break

    def _sort_by_column(self, column: int) -> None:
        header = self.table.horizontalHeader()
        order = Qt.AscendingOrder
        if header.sortIndicatorSection() == column:
            order = (Qt.AscendingOrder if header.sortIndicatorOrder() == Qt.DescendingOrder
                     else Qt.DescendingOrder)
        header.setSortIndicator(column, order)
        self.model.set_sort(column, order)

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if index.column() == 0 and self.model.toggle_group(index.row()):
            self.table.clearSelection()

    def _on_selection_changed(self) -> None:
        self.end_task_btn.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def _save_column_widths(self, *_args) -> None:
        widths = [self.table.columnWidth(col) for col in range(len(COLUMNS))]
        self.settings.set_column_widths("processes", widths)

    def _end_selected_task(self) -> None:
        pid = self._selected_pid()
        if pid is None:
            return
        confirm = QMessageBox.question(
            self,
            "Task beenden",
            f"Möchtest du den Prozess mit PID {pid} wirklich beenden?",
        )
        if confirm != QMessageBox.Yes:
            return
        ok, error = kill_process(pid)
        if not ok:
            QMessageBox.warning(self, "Fehler", f"Prozess konnte nicht beendet werden:\n{error}")

    def _start_command(self) -> None:
        command, ok = QInputDialog.getText(self, "Befehl ausführen", "Befehl:")
        if not ok or not command.strip():
            return
        try:
            parts = shlex.split(command)
        except ValueError as error:
            QMessageBox.warning(self, "Ungültiger Befehl", str(error))
            return
        if not parts or not self._start_detached(parts[0], parts[1:]):
            QMessageBox.warning(self, "Start fehlgeschlagen", "Der Befehl konnte nicht gestartet werden.")

    def _choose_and_start_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Datei starten")
        if path:
            self._start_files([path])

    def _start_files(self, paths: list[str]) -> None:
        invalid_paths = []
        for path in paths:
            if not os.path.isfile(path) or not os.access(path, os.X_OK):
                invalid_paths.append(path)
                continue
            if not self._start_detached(path, []):
                invalid_paths.append(path)
        if invalid_paths:
            QMessageBox.warning(
                self, "Start fehlgeschlagen",
                "Nur ausführbare Dateien können gestartet werden:\n" + "\n".join(invalid_paths),
            )

    @staticmethod
    def _start_detached(program: str, arguments: list[str]) -> bool:
        result = QProcess.startDetached(program, arguments)
        return result[0] if isinstance(result, tuple) else result

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.deleteLater()
            self.worker = None

    def set_refresh_interval(self, seconds: float) -> None:
        if seconds == self._interval:
            return
        self._interval = seconds
        self.worker.stop()
        self.worker = ProcessWorker(interval=seconds, parent=self)
        self.worker.rowsReady.connect(self._on_rows_ready)
        self.worker.start()
