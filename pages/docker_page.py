"""Docker tab - lists containers with live CPU/memory/GPU usage, like the Processes tab.
Containers can be marked as favorites, renamed, and given a custom icon so they're easier
to spot; this metadata is stored in app settings, keyed by the container's docker name.
"""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSize, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.settings import Settings
from app.system_info import DockerContainerRow, DockerWorker, docker_container_action
from widgets.resizable_table import ResizableRowsTableView

COLUMNS = ["★", "Name", "Image", "Status", "CPU %", "Speicher (MB)", "Limit (MB)",
           "Speicher %", "GPU %", "VRAM (MB)"]
_DEFAULT_COLUMN_WIDTHS = [28, 200, 160, 110, 70, 110, 90, 80, 70, 100]
# Same Win10-style heat-map highlighting used on the Processes tab.
_HEATMAP_COLUMNS = {4: 100.0, 5: 4096.0, 7: 100.0, 8: 100.0, 9: 4096.0}
_IMAGE_FILTER = "Bilder (*.png *.jpg *.jpeg *.bmp *.svg *.ico);;Alle Dateien (*)"


_REMOTE_ACTION_HINT = "Im Remote-Modus zeigt diese Liste Daten des Servers. Aktionen würden auf diesem PC ausgeführt und sind deshalb gesperrt."

class DockerTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[DockerContainerRow] = []
        self._meta: dict[str, dict] = {}

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
        row = self._rows[index.row()]
        col = index.column()
        meta = self._meta.get(row.name, {})

        if role == Qt.DecorationRole and col == 1:
            icon_path = meta.get("icon_path")
            if icon_path:
                return QIcon(icon_path)
            return None

        if role == Qt.BackgroundRole and col in _HEATMAP_COLUMNS:
            value = {
                4: row.cpu_percent, 5: row.memory_mb, 7: row.memory_percent,
                8: row.gpu_percent, 9: row.gpu_vram_mb,
            }[col]
            if value is None:
                return None
            ratio = min(value / _HEATMAP_COLUMNS[col], 1.0)
            if ratio > 0.05:
                alpha = int(30 + ratio * 150)
                return QColor(253, 165, 84, alpha)
            return None

        if role not in (Qt.DisplayRole, Qt.EditRole, Qt.TextAlignmentRole):
            return None
        if role == Qt.TextAlignmentRole:
            return Qt.AlignLeft | Qt.AlignVCenter
        if col == 0:
            return "★" if meta.get("favorite") else ""
        if col == 1:
            return meta.get("custom_name") or row.name
        if col == 2:
            return row.image
        if col == 3:
            return row.status
        if col == 4:
            return round(row.cpu_percent, 1) if row.running else "–"
        if col == 5:
            return round(row.memory_mb, 1) if row.running else "–"
        if col == 6:
            return round(row.memory_limit_mb, 1) if row.running else "–"
        if col == 7:
            return round(row.memory_percent, 1) if row.running else "–"
        if col == 8:
            return round(row.gpu_percent, 1) if row.running and row.gpu_percent is not None else "–"
        if col == 9:
            return round(row.gpu_vram_mb, 1) if row.running and row.gpu_vram_mb is not None else "–"
        return None

    def container_at(self, row: int) -> DockerContainerRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def set_rows(self, rows: list[DockerContainerRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def set_meta(self, meta: dict[str, dict]) -> None:
        self._meta = meta
        if self._rows:
            top_left = self.index(0, 0)
            bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)


class DockerPage(QWidget):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._meta: dict[str, dict] = settings.docker_meta

        self.model = DockerTableModel(self)
        self.model.set_meta(self._meta)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Container suchen...")
        self.search_box.textChanged.connect(self.proxy.setFilterFixedString)

        self.favorite_btn = QPushButton("★ Favorit")
        self.favorite_btn.setCheckable(True)
        self.icon_btn = QPushButton("Bild wählen...")
        self.rename_btn = QPushButton("Umbenennen...")
        self.start_btn = QPushButton("Starten")
        self.stop_btn = QPushButton("Stoppen")
        self.restart_btn = QPushButton("Neustart")
        self.remove_btn = QPushButton("Entfernen")
        for btn in (self.favorite_btn, self.icon_btn, self.rename_btn,
                    self.start_btn, self.stop_btn, self.restart_btn, self.remove_btn):
            btn.setEnabled(False)
        self.favorite_btn.clicked.connect(self._toggle_favorite)
        self.icon_btn.clicked.connect(self._choose_icon)
        self.rename_btn.clicked.connect(self._rename_container)
        self.start_btn.clicked.connect(lambda: self._run_action("start"))
        self.stop_btn.clicked.connect(lambda: self._run_action("stop"))
        self.restart_btn.clicked.connect(lambda: self._run_action("restart"))
        self.remove_btn.clicked.connect(lambda: self._run_action("rm"))

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_box)
        top_bar.addStretch(1)
        top_bar.addWidget(self.favorite_btn)
        top_bar.addWidget(self.icon_btn)
        top_bar.addWidget(self.rename_btn)
        top_bar.addWidget(self.start_btn)
        top_bar.addWidget(self.stop_btn)
        top_bar.addWidget(self.restart_btn)
        top_bar.addWidget(self.remove_btn)

        self.table = ResizableRowsTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setIconSize(QSize(20, 20))
        # Interactive on every column (not Stretch) so Name can be dragged wider too.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.verticalHeader().setVisible(False)
        saved_widths = self.settings.column_widths("docker")
        widths = saved_widths if saved_widths and len(saved_widths) == len(COLUMNS) else _DEFAULT_COLUMN_WIDTHS
        for col, width in enumerate(widths):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().sectionResized.connect(self._save_column_widths)
        self.table.set_default_row_height(
            self.settings.row_height("docker", self.table.verticalHeader().defaultSectionSize())
        )
        self.table.set_row_height_callback(lambda h: self.settings.set_row_height("docker", h))
        self.table.sortByColumn(0, Qt.DescendingOrder)  # favorites first
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        self.info_label = QLabel()
        self.info_label.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self.info_label)
        layout.addWidget(self.table)

        self._interval = 2.0
        self.worker: DockerWorker | None = None
        self._remote_rows = None
        self._start_worker()

    def _start_worker(self) -> None:
        self.worker = DockerWorker(interval=self._interval, parent=self)
        self.worker.rowsReady.connect(self._on_rows_ready)
        self.worker.unavailable.connect(self._on_unavailable)
        self.worker.start()

    def set_remote_docker(self, rows: list[dict] | None, error: str = "") -> None:
        self._remote_rows = rows
        if rows is None:
            self._on_selection_changed()
            if self.worker is None or not self.worker.isRunning():
                self._start_worker()
            return
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
        self._on_rows_ready([DockerContainerRow(**row) for row in rows])
        if error:
            self._on_unavailable(error)
        self._on_selection_changed()

    def _on_rows_ready(self, rows: list[DockerContainerRow]) -> None:
        self.info_label.setVisible(False)
        selected_id = self._selected_id()
        self.model.set_rows(rows)
        if selected_id is not None:
            self._reselect_id(selected_id)

    def _on_unavailable(self, message: str) -> None:
        self.model.set_rows([])
        self.info_label.setText(f"Docker nicht verfügbar: {message}")
        self.info_label.setVisible(True)

    def _selected_id(self) -> str | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        source_index = self.proxy.mapToSource(indexes[0])
        container = self.model.container_at(source_index.row())
        return container.id if container else None

    def _reselect_id(self, container_id: str) -> None:
        for row in range(self.model.rowCount()):
            container = self.model.container_at(row)
            if container and container.id == container_id:
                source_index = self.model.index(row, 0)
                proxy_index = self.proxy.mapFromSource(source_index)
                self.table.selectRow(proxy_index.row())
                break

    def _selected_container(self) -> DockerContainerRow | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        source_index = self.proxy.mapToSource(indexes[0])
        return self.model.container_at(source_index.row())

    def _on_selection_changed(self) -> None:
        container = self._selected_container()
        has_selection = container is not None
        # Icon / name / favourite are local metadata and stay usable in remote mode;
        # lifecycle actions would hit the local Docker daemon, not the server's.
        remote = self._remote_rows is not None
        for btn in (self.icon_btn, self.rename_btn, self.favorite_btn):
            btn.setEnabled(has_selection)
        self.restart_btn.setEnabled(has_selection and not remote)
        self.remove_btn.setEnabled(has_selection and not remote)
        self.start_btn.setEnabled(bool(container) and not container.running and not remote)
        self.stop_btn.setEnabled(bool(container) and container.running and not remote)
        for btn in (self.restart_btn, self.remove_btn, self.start_btn, self.stop_btn):
            btn.setToolTip(_REMOTE_ACTION_HINT if remote else "")
        self.favorite_btn.blockSignals(True)
        self.favorite_btn.setChecked(
            bool(self._meta.get(container.name, {}).get("favorite")) if container else False
        )
        self.favorite_btn.blockSignals(False)

    def _save_column_widths(self, *_args) -> None:
        widths = [self.table.columnWidth(col) for col in range(len(COLUMNS))]
        self.settings.set_column_widths("docker", widths)

    def _meta_for(self, name: str) -> dict:
        return self._meta.setdefault(name, {})

    def _save_meta(self) -> None:
        self.settings.set_docker_meta(self._meta)
        self.model.set_meta(self._meta)

    def _toggle_favorite(self, checked: bool) -> None:
        container = self._selected_container()
        if container is None:
            return
        self._meta_for(container.name)["favorite"] = checked
        self._save_meta()

    def _choose_icon(self) -> None:
        container = self._selected_container()
        if container is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Bild für Container wählen", "", _IMAGE_FILTER)
        if not path:
            return
        self._meta_for(container.name)["icon_path"] = path
        self._save_meta()

    def _rename_container(self) -> None:
        container = self._selected_container()
        if container is None:
            return
        current = self._meta.get(container.name, {}).get("custom_name", "")
        new_name, ok = QInputDialog.getText(
            self, "Container umbenennen",
            "Anzeigename (nur in dieser App sichtbar):", text=current or container.name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if new_name and new_name != container.name:
            self._meta_for(container.name)["custom_name"] = new_name
        else:
            self._meta_for(container.name).pop("custom_name", None)
        self._save_meta()

    def _run_action(self, action: str) -> None:
        container = self._selected_container()
        if container is None or self._remote_rows is not None:
            return
        if action == "rm":
            confirm = QMessageBox.question(
                self, "Container entfernen",
                f"Möchtest du den Container \"{container.name}\" wirklich entfernen?",
            )
            if confirm != QMessageBox.Yes:
                return
        ok, error = docker_container_action(container.id, action)
        if not ok:
            QMessageBox.warning(self, "Fehler", f"Aktion fehlgeschlagen:\n{error}")

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.deleteLater()
            self.worker = None

    def set_refresh_interval(self, seconds: float) -> None:
        if seconds == self._interval:
            return
        self._interval = seconds
        if self.worker is not None:
            self.worker.stop()
        self._start_worker()
