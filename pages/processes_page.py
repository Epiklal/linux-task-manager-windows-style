"""Processes tab - Windows 10 style process list with live, non-blocking updates."""
from __future__ import annotations

import getpass
import os
import shlex
from dataclasses import dataclass

import psutil
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPointF,
    QProcess,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
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
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from app.system_info import ProcessRow, ProcessWorker, kill_process
from widgets.resizable_table import ResizableRowsTableView

# Column order follows the Windows 10 "Prozesse" tab; PID/Benutzer/Threads come last.
COLUMNS = ["Name", "Status", "CPU", "Arbeitsspeicher", "GPU", "GPU-Speicher",
           "PID", "Benutzer", "Threads"]
_COLUMN_WIDTHS_KEY = "processes_win10"
_DEFAULT_COLUMN_WIDTHS = [280, 90, 75, 115, 75, 110, 70, 110, 70]
_NUMERIC_COLUMNS = {2, 3, 4, 5, 6, 8}
# Windows 10 highlights cells of resource-hungry processes with a yellow heat map.
_HEATMAP_COLUMNS = {2: 100.0, 3: 4096.0, 4: 100.0, 5: 4096.0}
# Heat map colours (idle -> very busy) come from the colour scheme, see app.themes.
_HEAT_RATIOS = (0.0, 0.3, 0.6, 1.0)
_DEFAULT_HEAT = ["#fff4c4", "#ffe8a6", "#ffd27a", "#ffa04a"]  # Windows 10 yellow
_STATUS_LABELS = {"stopped": "Angehalten", "zombie": "Zombie", "dead": "Beendet"}

_REMOTE_ACTION_HINT = "Im Remote-Modus zeigt diese Liste Daten des Servers. Aktionen würden auf diesem PC ausgeführt und sind deshalb gesperrt."

TOTAL_ROLE = Qt.UserRole + 1
INDENT_ROLE = Qt.UserRole + 2
EXPANDED_ROLE = Qt.UserRole + 3

_INDENT_STEP = 16
_ARROW_SLOT = 16


def _de_number(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _format_percent(value: float) -> str:
    return "0 %" if value < 0.05 else f"{_de_number(value)} %"


def _format_mb(value: float) -> str:
    return f"{_de_number(value)} MB"


def _scaled_font(font: QFont, factor: float) -> QFont:
    """Copy of `font` scaled by `factor`; works for pixel- (QSS "px") and point-sized fonts."""
    scaled = QFont(font)
    if font.pixelSize() > 0:
        scaled.setPixelSize(round(font.pixelSize() * factor))
    else:
        scaled.setPointSizeF(font.pointSizeF() * factor)
    return scaled


def _heat_color(ratio: float, colors: list[str]) -> QColor:
    stops = list(zip(_HEAT_RATIOS, (QColor(c) for c in colors)))
    for (start, low), (end, high) in zip(stops, stops[1:]):
        if ratio <= end:
            t = (ratio - start) / (end - start)
            return QColor(
                round(low.red() + (high.red() - low.red()) * t),
                round(low.green() + (high.green() - low.green()) * t),
                round(low.blue() + (high.blue() - low.blue()) * t),
            )
    return QColor(stops[-1][1])


_icon_cache: dict[str, QIcon] = {}


def _process_icon(name: str) -> QIcon:
    """Icon from the desktop icon theme matching the process name (like Win10's app icons)."""
    icon = _icon_cache.get(name)
    if icon is None:
        icon = QIcon()
        for candidate in (name, name.lower(), name.lower().split("-")[0]):
            if candidate and QIcon.hasThemeIcon(candidate):
                icon = QIcon.fromTheme(candidate)
                break
        if icon.isNull():
            icon = QIcon.fromTheme("application-x-executable")
        _icon_cache[name] = icon
    return icon


@dataclass
class ProcessListEntry:
    process: ProcessRow | None = None
    group_key: str | None = None
    label: str = ""
    count: int = 0
    indent: int = 0
    totals: ProcessRow | None = None  # summed values shown on a collapsed process group

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
        self._sort_column = 0
        self._sort_order = Qt.AscendingOrder
        self._current_user = getpass.getuser()
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._totals: dict[int, str] = {}
        self._win11 = False
        self._heat = _DEFAULT_HEAT
        self._group_color = QColor("#1c3b8c")
        self._text_color = QColor("#000000")

    def set_colors(self, colors: dict, win11: bool = False) -> None:
        """Scheme colours from app.themes.widget_colors (heat map, group titles)."""
        self._win11 = win11
        self._heat = colors["heat"]
        self._group_color = QColor(colors["group"])
        self._text_color = QColor(colors["text"])
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._rows) - 1, len(COLUMNS) - 1))
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._rows) - 1, len(COLUMNS) - 1))

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMNS[section]
        if role == TOTAL_ROLE:
            return self._totals.get(section)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self._rows[index.row()]
        col = index.column()

        if role == INDENT_ROLE:
            return entry.indent if col == 0 else 0
        if role == EXPANDED_ROLE:
            if col != 0 or not entry.is_group:
                return None
            return entry.group_key not in self._collapsed_groups

        if entry.is_group and entry.totals is None:
            # Category header ("Apps (12)"): larger, Win10-blue text, no values.
            if role == Qt.DisplayRole and col == 0:
                return f"{entry.label} ({entry.count})"
            if role == Qt.FontRole:
                font = _scaled_font(QApplication.font(), 1.15)
                font.setWeight(QFont.DemiBold if self._win11 else QFont.Normal)
                return font
            if role == Qt.ForegroundRole:
                return self._group_color
            return None

        row = entry.process or entry.totals
        if role == Qt.DecorationRole and col == 0:
            return _process_icon(row.name)

        if role == Qt.BackgroundRole and col in _HEATMAP_COLUMNS:
            value = {
                2: row.cpu_percent, 3: row.memory_mb,
                4: row.gpu_percent, 5: row.gpu_vram_mb,
            }[col]
            ratio = 0.0 if value is None else min(value / _HEATMAP_COLUMNS[col], 1.0)
            return _heat_color(ratio, self._heat)

        if role == Qt.ForegroundRole and col in _HEATMAP_COLUMNS:
            return self._text_color
        if role == Qt.TextAlignmentRole:
            horizontal = Qt.AlignRight if col in _NUMERIC_COLUMNS else Qt.AlignLeft
            return horizontal | Qt.AlignVCenter
        if role != Qt.DisplayRole:
            return None
        if col == 0:
            return f"{row.name} ({entry.count})" if entry.is_group else row.name
        if col == 1:
            return "" if entry.is_group else _STATUS_LABELS.get(row.status, "")
        if col == 2:
            return _format_percent(row.cpu_percent)
        if col == 3:
            return _format_mb(row.memory_mb)
        if col == 4:
            return _format_percent(row.gpu_percent) if row.gpu_percent is not None else "–"
        if col == 5:
            return _format_mb(row.gpu_vram_mb) if row.gpu_vram_mb is not None else "–"
        if col == 6:
            return "" if entry.is_group else row.pid
        if col == 7:
            return row.username
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

    def set_rows(self, rows: list[ProcessRow], memory_percent: float | None = None) -> None:
        self._process_rows = rows
        self._update_totals(rows, memory_percent)
        self._rebuild_rows()

    def _update_totals(self, rows: list[ProcessRow], memory_percent: float | None) -> None:
        """Overall usage printed above the column names, like Windows 10's "7% / CPU"."""
        cpu = min(100.0, sum(row.cpu_percent for row in rows) / self._cpu_count)
        memory_mb = sum(row.memory_mb for row in rows)
        gpu_values = [row.gpu_percent for row in rows if row.gpu_percent is not None]
        vram_values = [row.gpu_vram_mb for row in rows if row.gpu_vram_mb is not None]
        self._totals = {
            2: f"{cpu:.0f}%",
            3: (f"{memory_percent:.0f}%" if memory_percent is not None
                else f"{_de_number(memory_mb / 1024)} GB"),
        }
        if gpu_values:
            self._totals[4] = f"{min(100.0, sum(gpu_values)):.0f}%"
        if vram_values:
            self._totals[5] = f"{_de_number(sum(vram_values) / 1024)} GB"
        self.headerDataChanged.emit(Qt.Horizontal, 2, 5)

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
            0: lambda row: row.name.casefold(), 1: lambda row: row.status.casefold(),
            2: lambda row: row.cpu_percent, 3: lambda row: row.memory_mb,
            4: lambda row: row.gpu_percent if row.gpu_percent is not None else -1,
            5: lambda row: row.gpu_vram_mb if row.gpu_vram_mb is not None else -1,
            6: lambda row: row.pid, 7: lambda row: row.username.casefold(),
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
                totals=self._sum_rows(matching_rows),
            ))
            if group_key not in self._collapsed_groups:
                target.extend(ProcessListEntry(process=row, indent=2) for row in matching_rows)


    @staticmethod
    def _sum_rows(rows: list[ProcessRow]) -> ProcessRow:
        gpu = [row.gpu_percent for row in rows if row.gpu_percent is not None]
        vram = [row.gpu_vram_mb for row in rows if row.gpu_vram_mb is not None]
        return ProcessRow(
            pid=rows[0].pid, name=rows[0].name, username=rows[0].username, status="",
            cpu_percent=sum(row.cpu_percent for row in rows),
            memory_mb=sum(row.memory_mb for row in rows),
            num_threads=sum(row.num_threads for row in rows),
            gpu_percent=sum(gpu) if gpu else None,
            gpu_vram_mb=sum(vram) if vram else None,
        )


class NameColumnDelegate(QStyledItemDelegate):
    """Draws the Name column Windows-10-style: indentation, a chevron for expandable
    groups, then icon + text. The selection/hover background still spans the full cell."""

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        indent = index.data(INDENT_ROLE) or 0
        expanded = index.data(EXPANDED_ROLE)

        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ""
        background.icon = QIcon()
        style.drawControl(QStyle.CE_ItemViewItem, background, painter, widget)

        x = option.rect.left() + indent * _INDENT_STEP
        if expanded is not None:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            color = QColor(option.palette.text().color())
            color.setAlpha(170)
            painter.setPen(QPen(color, 1.3))
            cx = x + _ARROW_SLOT / 2
            cy = option.rect.center().y() + 0.5
            if expanded:
                points = [QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2)]
            else:
                points = [QPointF(cx - 2, cy - 4), QPointF(cx + 2, cy), QPointF(cx - 2, cy + 4)]
            painter.drawPolyline(points)
            painter.restore()

        content = QStyleOptionViewItem(option)
        self.initStyleOption(content, index)
        content.rect = QRect(option.rect).adjusted(x - option.rect.left() + _ARROW_SLOT, 0, 0, 0)
        content.backgroundBrush = Qt.NoBrush
        content.state &= ~QStyle.State_Selected
        content.state &= ~QStyle.State_MouseOver
        if option.state & QStyle.State_Selected:
            content.palette.setColor(content.palette.ColorRole.Text,
                                     option.palette.highlightedText().color())
        style.drawControl(QStyle.CE_ItemViewItem, content, painter, widget)


class Win10HeaderView(QHeaderView):
    """Two-line column headers as in the Windows 10 Task Manager: the overall usage
    ("7%") above the column name, a small sort chevron centred on top."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.set_colors({"header_bg": "#ffffff", "header_hover": "#d9ebf9",
                         "header_text": "#4d4d4d", "header_total": "#1a1a1a",
                         "header_border": "#e5e5e5", "chevron": "#7a7a7a"})

    def set_colors(self, colors: dict, separators: bool = True) -> None:
        """Scheme colours; Windows 11 draws no vertical column separators."""
        self._bg, self._hover = QColor(colors["header_bg"]), QColor(colors["header_hover"])
        self._text, self._total = QColor(colors["header_text"]), QColor(colors["header_total"])
        self._border, self._chevron = QColor(colors["header_border"]), QColor(colors["chevron"])
        self._separators = separators
        self.viewport().update()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        hint = super().sizeHint()
        return QSize(hint.width(), 46)

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:  # noqa: N802
        painter.save()
        hovered = rect.contains(self.mapFromGlobal(self.cursor().pos())) and self.underMouse()
        painter.fillRect(rect, self._hover if hovered else self._bg)
        painter.setPen(self._border)
        if self._separators:
            painter.drawLine(rect.topRight(), rect.bottomRight())
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        model = self.model()
        label = model.headerData(logical_index, Qt.Horizontal, Qt.DisplayRole) or ""
        total = model.headerData(logical_index, Qt.Horizontal, TOTAL_ROLE)
        align = Qt.AlignRight if logical_index in _NUMERIC_COLUMNS else Qt.AlignLeft
        text_rect = rect.adjusted(7, 3, -7, -4)

        base_font = QFont(self.font())
        painter.setFont(base_font)
        painter.setPen(self._text)
        painter.drawText(text_rect, align | Qt.AlignBottom, label)
        if total:
            total_font = _scaled_font(base_font, 1.35)
            painter.setFont(total_font)
            painter.setPen(self._total)
            painter.drawText(text_rect.adjusted(0, 4, 0, 0), align | Qt.AlignTop, total)

        if self.isSortIndicatorShown() and self.sortIndicatorSection() == logical_index:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(self._chevron, 1.2))
            cx, cy = rect.center().x() + 0.5, rect.top() + 5.5
            if self.sortIndicatorOrder() == Qt.AscendingOrder:
                points = [QPointF(cx - 4, cy + 2), QPointF(cx, cy - 2), QPointF(cx + 4, cy + 2)]
            else:
                points = [QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2)]
            painter.drawPolyline(points)
        painter.restore()

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.setMouseTracking(True)
        super().enterEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.viewport().update()
        super().leaveEvent(event)


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
        self._remote_enabled = False

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Prozesse suchen...")
        self.search_box.textChanged.connect(self.model.set_filter)

        self.search_box.setFixedWidth(260)
        self.search_box.setClearButtonEnabled(True)

        # Shown in the main window's footer (bottom right), like in Windows 10 ...
        self.end_task_btn = QPushButton("Task beenden")
        self.end_task_btn.setEnabled(False)
        self.end_task_btn.clicked.connect(self._end_selected_task)
        # ... and in the page header in the Windows 11 style.
        self.header_end_task_btn = QPushButton("Task beenden")
        self.header_end_task_btn.setEnabled(False)
        self.header_end_task_btn.clicked.connect(self._end_selected_task)

        self.top_bar = QWidget()
        self._top_bar_layout = top_bar = QHBoxLayout(self.top_bar)
        top_bar.setContentsMargins(8, 6, 8, 6)
        top_bar.addStretch(1)
        top_bar.addWidget(self.search_box)

        self.table = ProcessTableView()
        self.header = Win10HeaderView(self.table)
        self.table.setHorizontalHeader(self.header)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(0, NameColumnDelegate(self.table))
        self.table.setShowGrid(False)
        self.table.setIconSize(QSize(16, 16))
        self.table.setMouseTracking(True)
        self.table.setAcceptDrops(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(False)
        self.table.setAlternatingRowColors(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Interactive on every column (not Stretch) so the user can actually drag
        # each column - including Name - wider or narrower; Stretch locked Name's
        # width to "fill remaining space" and ignored manual resize attempts.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.verticalHeader().setVisible(False)
        saved_widths = self.settings.column_widths(_COLUMN_WIDTHS_KEY)
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
        header.setSortIndicator(0, Qt.AscendingOrder)
        header.sectionClicked.connect(self._sort_by_column)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.clicked.connect(self._on_table_clicked)
        self.table.filesDropped.connect(self._start_files)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.top_bar)
        layout.addWidget(self.table)

        self._interval = 1.0
        self.worker: ProcessWorker | None = None
        self._start_worker()
        self._remote_enabled = False

    def _start_worker(self) -> None:
        self.worker = ProcessWorker(interval=self._interval, parent=self)
        self.worker.rowsReady.connect(self._on_rows_ready)
        self.worker.start()

    def _stop_worker(self) -> None:
        if self.worker is None:
            return
        self.worker.rowsReady.disconnect(self._on_rows_ready)
        self.worker.stop()
        self.worker.deleteLater()
        self.worker = None

    def _on_rows_ready(self, rows: list[ProcessRow]) -> None:
        try:
            # Keep the current selection (by PID) across the periodic refresh.
            selected_pid = self._selected_pid()
            memory_percent = None if self._remote_enabled else psutil.virtual_memory().percent
            self.model.set_rows(rows, memory_percent)
            if selected_pid is not None:
                self._reselect_pid(selected_pid)
            thread_count = sum(r.num_threads for r in rows)
            self.statsChanged.emit(len(rows), thread_count)
        finally:
            # Also after an exception, or the worker would never send rows again.
            if self.worker is not None:
                self.worker.mark_consumed()

    def set_remote_rows(self, rows: list[dict] | None) -> None:
        if rows is None:
            if self._remote_enabled:
                self._remote_enabled = False
                self._on_selection_changed()
                self._start_worker()
            return
        if not self._remote_enabled:
            self._remote_enabled = True
            self._on_selection_changed()
            self._stop_worker()
        self._on_rows_ready([ProcessRow(**row) for row in rows])

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
        # PIDs shown in remote mode belong to the server - never kill a local process
        # that merely happens to have the same PID.
        can_end = bool(self.table.selectionModel().selectedRows()) and not self._remote_enabled
        for button in (self.end_task_btn, self.header_end_task_btn):
            button.setEnabled(can_end)
            button.setToolTip(_REMOTE_ACTION_HINT if self._remote_enabled else "")

    def _save_column_widths(self, *_args) -> None:
        widths = [self.table.columnWidth(col) for col in range(len(COLUMNS))]
        self.settings.set_column_widths(_COLUMN_WIDTHS_KEY, widths)

    def _end_selected_task(self) -> None:
        pid = self._selected_pid()
        if pid is None or self._remote_enabled:
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

    def place_search_box(self, header_layout: QHBoxLayout | None) -> None:
        """Move the search box into the Windows 11 page header (or back above the list)."""
        if header_layout is None:
            self._top_bar_layout.addWidget(self.search_box)
        else:
            # Right after the title/stretch, before the command buttons.
            header_layout.insertWidget(2, self.search_box)
        self.top_bar.setVisible(header_layout is None)

    def set_theme(self, colors: dict, win11: bool = False) -> None:
        self.model.set_colors(colors, win11)
        self.header.set_colors(colors, separators=not win11)

    def run_new_task(self) -> None:
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

    def choose_and_start_file(self) -> None:
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
        self._stop_worker()

    def set_refresh_interval(self, seconds: float) -> None:
        # Change the running worker instead of replacing it: no thread restart per
        # spin-box step, and no orphaned worker left behind.
        self._interval = seconds
        if self.worker is not None:
            self.worker.set_interval(seconds)
