"""Memory tab - what uses RAM, suggestions, and targeted actions on a single program."""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.i18n import N_, tr
from app.memory_info import (
    IDLE_MIN_SECONDS,
    MIB,
    OWN_PROGRAM_NAME,
    DropCachesResult,
    DropCachesWorker,
    EndProgramWorker,
    EndResult,
    MemoryOverview,
    MemorySnapshot,
    MemoryWorker,
    PageOutResult,
    PageOutWorker,
    ProgramRow,
    build_hints,
    format_bytes,
    is_tight,
    memory_data_sections,
    overview_from_remote,
    pressure_level,
    read_meminfo,
    read_overview,
    read_swaps,
    rows_from_remote,
)
from app.themes import WIN10_CHART_COLORS
from widgets.live_chart import LiveChart

COLUMNS = [N_("Program"), N_("User"), N_("Processes"), N_("RAM"), N_("Swap"), N_("Note")]
_NUMBER_COLUMNS = (2, 3, 4)

_PRESSURE_TEXTS = {
    "none": N_("None"),
    "light": N_("Light"),
    "high": N_("High"),
    None: N_("Unknown"),
}

_REMOTE_ACTION_HINT = N_(
    "In remote mode this list shows the server's data. "
    "Actions would run on this PC and are therefore locked."
)
# Below this, a page-out did not achieve anything the user would notice.
_NOTICEABLE_BYTES = 16 * MIB

_CHART_POINTS = 60
_CHART_HEIGHT = 110
_CHART_INTERVAL_MS = 1000


def _empty_history() -> deque:
    return deque([0.0] * _CHART_POINTS, maxlen=_CHART_POINTS)


def _process_count(count: int) -> str:
    if count == 1:
        return tr("1 process")
    return tr("{count} processes", count=count)


def _program_note(row: ProgramRow) -> str:
    if row.name == OWN_PROGRAM_NAME:
        return tr("this program")
    if row.essential:
        return tr("needed by the desktop")
    if not row.own:
        return tr("other user")
    if row.idle_seconds >= IDLE_MIN_SECONDS:
        return tr("idle for {minutes} min", minutes=int(row.idle_seconds // 60))
    return ""


def _hints_text(snapshot: MemorySnapshot) -> str:
    if not snapshot.hints:
        return tr("No program stands out right now.")
    lines = []
    for hint in snapshot.hints:
        if hint.kind == "idle":
            lines.append(tr("{name} has been idle for {minutes} min and holds {memory}.",
                            name=hint.name, minutes=int(hint.idle_seconds // 60),
                            memory=format_bytes(hint.memory_bytes)))
        else:
            lines.append(tr("{name} uses {memory}.",
                            name=hint.name, memory=format_bytes(hint.memory_bytes)))
    lines.append(tr("These are only suggestions. Nothing is ended automatically."))
    return "\n".join(lines)


class MemoryPage(QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

        self._chart_colors = dict(WIN10_CHART_COLORS)
        self.memory_chart_label = QLabel("")
        self.memory_chart = self._new_chart(self._chart_colors["memory"])
        self.swap_chart_label = QLabel("")
        self.swap_chart = self._new_chart(self._chart_colors["disk"])

        charts = QGridLayout()
        charts.addWidget(self.memory_chart_label, 0, 0)
        charts.addWidget(self.swap_chart_label, 0, 1)
        charts.addWidget(self.memory_chart, 1, 0)
        charts.addWidget(self.swap_chart, 1, 1)
        charts.setColumnStretch(0, 1)
        charts.setColumnStretch(1, 1)

        tiles = QGridLayout()
        self.in_use_value = self._add_tile(tiles, 0, tr("In use"))
        self.available_value = self._add_tile(tiles, 1, tr("Available now"))
        self.cache_value = self._add_tile(tiles, 2, tr("Cache"))
        self.swap_value = self._add_tile(tiles, 3, tr("Swap"))
        self.pressure_value = self._add_tile(tiles, 4, tr("Memory pressure"))

        self.rating_label = self._text_label()
        self.hints_label = self._text_label()

        self.end_btn = QPushButton(tr("End program"))
        self.end_btn.clicked.connect(self._on_end_clicked)
        self.swap_btn = QPushButton(tr("Move to swap"))
        self.swap_btn.clicked.connect(self._on_swap_clicked)
        self.action_hint_label = self._text_label()

        action_bar = QHBoxLayout()
        action_bar.addWidget(self.end_btn)
        action_bar.addWidget(self.swap_btn)
        action_bar.addWidget(self.action_hint_label, 1)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(name) for name in COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(5, self._note_column_width())
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        self.status_label = self._text_label()
        self.status_label.hide()

        self.cache_box = QGroupBox(tr("Emptying the cache"))
        self.cache_check = QCheckBox(tr("Offer to empty the cache"))
        self.cache_check.setChecked(settings.memory_cache_option)
        self.cache_check.toggled.connect(self._on_cache_option_toggled)
        self.cache_warning = self._text_label(tr(
            "This rarely helps. Linux gives the cache back on its own as soon as programs need "
            "the memory, and after emptying it programs and files load more slowly until it has "
            "filled up again. It needs the setup under Settings."
        ))
        self.drop_btn = QPushButton(tr("Empty cache now"))
        self.drop_btn.clicked.connect(self._on_drop_clicked)
        cache_layout = QVBoxLayout(self.cache_box)
        cache_layout.addWidget(self.cache_check)
        cache_layout.addWidget(self.cache_warning)
        cache_layout.addWidget(self.drop_btn, 0, Qt.AlignLeft)

        programs_tab = QWidget()
        programs_layout = QVBoxLayout(programs_tab)
        programs_layout.addWidget(self.hints_label)
        programs_layout.addLayout(action_bar)
        programs_layout.addWidget(self.table, 1)
        programs_layout.addWidget(self.status_label)
        programs_layout.addWidget(self.cache_box)

        self.data_table = QTableWidget(0, 2)
        self.data_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.data_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.data_table.setFocusPolicy(Qt.NoFocus)
        self.data_table.horizontalHeader().setVisible(False)
        self.data_table.horizontalHeader().setStretchLastSection(True)
        self.data_table.verticalHeader().setVisible(False)
        self.data_table.setColumnWidth(0, 280)

        self.tabs = QTabWidget()
        self.tabs.addTab(programs_tab, tr("Programs"))
        self.tabs.addTab(self.data_table, tr("Data"))
        self.tabs.currentChanged.connect(self._update_data_table)

        layout = QVBoxLayout(self)
        layout.addLayout(charts)
        layout.addLayout(tiles)
        layout.addWidget(self.rating_label)
        layout.addWidget(self.tabs, 1)

        self._rows: list[ProgramRow] = []
        self._selected_key: tuple[str, str] | None = None
        self._overview: MemoryOverview | None = None
        self._remote = False
        self._remote_sensors: list[dict] | None = None
        self._hardware = None
        self._ram_sensors: list[dict] = []
        self._data_labels: list[tuple[str, str | None]] = []
        self._memory_history = _empty_history()
        self._swap_history = _empty_history()
        self.memory_chart.set_data(self._memory_history)
        self.swap_chart.set_data(self._swap_history)
        self._action_worker = None
        self._action_slot = None

        self._update_cache_option()
        self._update_actions()

        self.worker = MemoryWorker(parent=self)
        self.worker.snapshotReady.connect(self._on_snapshot)
        self.worker.hardwareReady.connect(self._on_hardware)
        self.worker.start()

        # Runs while the tab is hidden too, so the graphs have a history when it is opened.
        self.chart_timer = QTimer(self)
        self.chart_timer.timeout.connect(self._on_chart_timer)
        self.chart_timer.start(_CHART_INTERVAL_MS)
        self._add_to_charts(read_overview())

    @staticmethod
    def _new_chart(color) -> LiveChart:
        chart = LiveChart(max_points=_CHART_POINTS, max_value=100.0, line_color=color)
        chart.setFixedHeight(_CHART_HEIGHT)
        return chart

    def set_theme(self, dark: bool, chart_background=None, chart_colors=None) -> None:
        self._chart_colors = dict(chart_colors or WIN10_CHART_COLORS)
        self.memory_chart.set_line_color(self._chart_colors["memory"])
        self.swap_chart.set_line_color(self._chart_colors["disk"])
        self.memory_chart.set_theme(dark, chart_background)
        self.swap_chart.set_theme(dark, chart_background)

    def _note_column_width(self) -> int:
        notes = (tr("this program"), tr("needed by the desktop"), tr("other user"),
                 tr("idle for {minutes} min", minutes=999))
        metrics = self.table.fontMetrics()
        return max(metrics.horizontalAdvance(note) for note in notes) + 32

    @staticmethod
    def _text_label(text: str = "") -> QLabel:
        # Plain text: program names come from outside and must not be read as markup.
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _add_tile(grid: QGridLayout, column: int, title: str) -> QLabel:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        value = QLabel("-")
        font = QFont(value.font())
        font.setBold(True)
        value.setFont(font)
        tile = QVBoxLayout(frame)
        tile.addWidget(QLabel(title))
        tile.addWidget(value)
        grid.addWidget(frame, 0, column)
        grid.setColumnStretch(column, 1)
        return value

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self.worker.set_visible(True)

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self.worker.set_visible(False)

    def _on_snapshot(self, snapshot: MemorySnapshot) -> None:
        self.worker.mark_consumed()
        if not self._remote:
            self._ram_sensors = snapshot.ram_sensors
            self._show_snapshot(snapshot)

    def _on_hardware(self, hardware) -> None:
        self._hardware = hardware
        self._update_data_table()

    def _on_chart_timer(self) -> None:
        if self._remote:
            return
        overview = read_overview()
        self._add_to_charts(overview)
        if self.isVisible():
            self._overview = overview
            self._show_overview(overview)
            self._update_data_table()

    def _clear_charts(self) -> None:
        self._memory_history = _empty_history()
        self._swap_history = _empty_history()
        self.memory_chart.set_data(self._memory_history)
        self.swap_chart.set_data(self._swap_history)

    def _add_to_charts(self, overview: MemoryOverview) -> None:
        percent = overview.used * 100 / overview.total if overview.total else 0.0
        self._memory_history.append(percent)
        self.memory_chart.set_data(self._memory_history)
        self.memory_chart_label.setText(tr(
            "Memory: {used} of {total} ({percent} %)", used=format_bytes(overview.used),
            total=format_bytes(overview.total), percent=round(percent)))

        if overview.swap_total:
            swap_used = overview.swap_used or 0
            swap_percent = swap_used * 100 / overview.swap_total
            self._swap_history.append(swap_percent)
            self.swap_chart.set_overlay_text("")
            self.swap_chart_label.setText(tr(
                "Swap: {used} of {total} ({percent} %)", used=format_bytes(swap_used),
                total=format_bytes(overview.swap_total), percent=round(swap_percent)))
        else:
            self._swap_history.append(0.0)
            self.swap_chart.set_overlay_text(
                tr("Unknown") if overview.swap_total is None else tr("Not set up"))
            self.swap_chart_label.setText(tr("Swap"))
        self.swap_chart.set_data(self._swap_history)

    def set_remote_data(self, payload: dict | None) -> None:
        was_remote = self._remote
        self._remote = payload is not None
        self.cache_box.setVisible(not self._remote)
        if self._remote != was_remote:
            self._clear_charts()
        if payload is None:
            if was_remote:
                self._remote_sensors = None
                self._ram_sensors = []
                self.worker.request_refresh()
                self._update_actions()
            return
        system, processes = payload.get("system"), payload.get("processes")
        self._remote_sensors = payload.get("ram_sensors")
        if not system or processes is None:
            self._update_actions()
            return
        overview = overview_from_remote(system)
        rows = rows_from_remote(processes)
        self._add_to_charts(overview)
        self._show_snapshot(MemorySnapshot(overview, rows, build_hints(rows, overview)))
        self._update_data_table()

    def _show_snapshot(self, snapshot: MemorySnapshot) -> None:
        self._overview = snapshot.overview
        self._show_overview(snapshot.overview)
        self.hints_label.setText(_hints_text(snapshot))
        self._show_rows(snapshot.rows)
        self._update_actions()

    def _update_data_table(self) -> None:
        if self._overview is None or self.tabs.currentWidget() is not self.data_table:
            return
        if self._remote:
            # An older server does not send this field, which says nothing about its sensors.
            sections = memory_data_sections(self._overview, {}, [], None, self._remote_sensors,
                                            remote=True)
        else:
            sections = memory_data_sections(self._overview, read_meminfo(), read_swaps(),
                                            self._hardware, self._ram_sensors)
        self._show_data(sections)

    def _show_data(self, sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
        rows: list[tuple[str, str | None]] = []
        for title, entries in sections:
            rows.append((title, None))
            rows.extend(entries)
        labels = [(label, None if value is None else "") for label, value in rows]
        # With the same rows only the values change, so the scroll position stays.
        rebuild = labels != self._data_labels
        table = self.data_table
        table.setUpdatesEnabled(False)
        if rebuild:
            self._data_labels = labels
            table.clearSpans()
            table.setRowCount(len(rows))
            bold = QFont(table.font())
            bold.setBold(True)
        for index, (label, value) in enumerate(rows):
            if value is None:
                if rebuild:
                    item = QTableWidgetItem(label)
                    item.setFont(bold)
                    table.setItem(index, 0, item)
                    table.setItem(index, 1, None)
                    table.setSpan(index, 0, 1, 2)
                continue
            if rebuild:
                table.setItem(index, 0, QTableWidgetItem(label))
            item = table.item(index, 1)
            if item is None:
                table.setItem(index, 1, QTableWidgetItem(value))
            elif item.text() != value:
                item.setText(value)
        table.setUpdatesEnabled(True)

    def _show_overview(self, overview: MemoryOverview) -> None:
        used = format_bytes(overview.used)
        available = format_bytes(overview.available)
        self.in_use_value.setText(used)
        self.available_value.setText(available)
        self.cache_value.setText(format_bytes(overview.cached))
        if overview.swap_total is None:
            self.swap_value.setText(tr("Unknown"))
        elif overview.swap_total == 0:
            self.swap_value.setText(tr("Not set up"))
        else:
            self.swap_value.setText(tr("{used} of {total}",
                                       used=format_bytes(overview.swap_used or 0),
                                       total=format_bytes(overview.swap_total)))
        self.pressure_value.setText(tr(_PRESSURE_TEXTS[pressure_level(overview)]))
        if is_tight(overview):
            self.rating_label.setText(tr(
                "Memory is tight: {available} available. "
                "Closing a program you do not need helps most.", available=available))
        else:
            self.rating_label.setText(tr(
                "Everything is fine: {available} available. The cache is released "
                "automatically as soon as programs need the memory.", available=available))

    def _show_rows(self, rows: list[ProgramRow]) -> None:
        self._rows = rows
        selected_row = -1
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            if row.key == self._selected_key:
                selected_row = index
            memory = format_bytes(row.memory_bytes)
            swap = "-" if row.swap_bytes is None else format_bytes(row.swap_bytes)
            cells = (row.name, row.username, str(len(row.processes)),
                     memory if row.exact else f"≈ {memory}", swap, _program_note(row))
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column in _NUMBER_COLUMNS:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(index, column, item)
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        else:
            self.table.clearSelection()
            self._selected_key = None
        self.table.blockSignals(False)
        self.table.setUpdatesEnabled(True)

    def _on_selection_changed(self) -> None:
        indexes = self.table.selectionModel().selectedRows()
        row = self._rows[indexes[0].row()] if indexes else None
        self._selected_key = row.key if row else None
        self._update_actions()

    def _selected_row(self) -> ProgramRow | None:
        for row in self._rows:
            if row.key == self._selected_key:
                return row
        return None

    def _end_block_reason(self, row: ProgramRow | None) -> str:
        if self._remote:
            return tr(_REMOTE_ACTION_HINT)
        if row is None:
            return tr("Select a program to end it or move it to swap.")
        if row.name == OWN_PROGRAM_NAME:
            return tr("Lintask cannot end itself from here.")
        if row.essential:
            return tr("The desktop needs this program, so it cannot be ended here.")
        if not row.own:
            return tr("This program belongs to another user.")
        return ""

    def _update_actions(self) -> None:
        row = self._selected_row()
        reason = self._end_block_reason(row)
        no_swap = self._overview is not None and self._overview.swap_total == 0
        if not reason and no_swap:
            swap_reason = tr("No swap is set up, so nothing can be moved out of memory.")
        else:
            swap_reason = reason
        idle = self._action_worker is None
        self.end_btn.setEnabled(idle and not reason)
        self.swap_btn.setEnabled(idle and not swap_reason)
        self.drop_btn.setEnabled(idle)
        self.action_hint_label.setText(reason or swap_reason)

    def _show_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def _start_action(self, worker, slot) -> None:
        self._stop_action_worker()
        self._show_status("")
        self._action_worker = worker
        self._action_slot = slot
        worker.resultReady.connect(slot)
        worker.start()
        self._update_actions()

    def _stop_action_worker(self) -> None:
        if self._action_worker is None:
            return
        self._action_worker.resultReady.disconnect(self._action_slot)
        self._action_worker.stop()
        self._action_worker.deleteLater()
        self._action_worker = None
        self._action_slot = None

    def _finish_action(self) -> None:
        self._stop_action_worker()
        self._update_actions()
        self.worker.request_refresh()

    def _on_end_clicked(self) -> None:
        row = self._selected_row()
        if row is None or self._end_block_reason(row):
            return
        answer = QMessageBox.question(
            self, tr("End program"),
            tr("End {name} ({processes})?\n\nUnsaved work in this program will be lost.",
               name=row.name, processes=_process_count(len(row.processes))),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._start_action(EndProgramWorker(row.name, row.processes, parent=self),
                               self._on_end_result)

    def _on_end_result(self, result: EndResult) -> None:
        self._finish_action()
        if result.survivors:
            answer = QMessageBox.question(
                self, tr("End program"),
                tr("{name} is still running. End it by force?\n\n"
                   "It gets no chance to save anything.", name=result.name),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer == QMessageBox.Yes:
                self._start_action(
                    EndProgramWorker(result.name, list(result.survivors), force=True, parent=self),
                    self._on_force_result)
            else:
                self._show_status(tr("{name} is still running.", name=result.name))
            return
        self._show_end_result(result)

    def _on_force_result(self, result: EndResult) -> None:
        self._finish_action()
        if result.survivors:
            self._show_status(tr("{name} is still running.", name=result.name))
        else:
            self._show_end_result(result)

    def _show_end_result(self, result: EndResult) -> None:
        if result.denied:
            self._show_status(tr("{name}: {count} processes could not be ended, rights are missing.",
                                 name=result.name, count=result.denied))
        else:
            self._show_status(tr("{name} was ended.", name=result.name))

    def _on_swap_clicked(self) -> None:
        row = self._selected_row()
        if row is None or self._end_block_reason(row):
            return
        answer = QMessageBox.question(
            self, tr("Move to swap"),
            tr("Move {name} ({processes}) to swap?\n\nThe program keeps running. It will react "
               "slowly the next time it is used, while it fetches its memory back.",
               name=row.name, processes=_process_count(len(row.processes))),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._start_action(PageOutWorker(row.name, row.processes, parent=self),
                               self._on_swap_result)

    def _on_swap_result(self, result: PageOutResult) -> None:
        self._finish_action()
        if result.error:
            self._show_status(tr(result.error))
            return
        if result.moved_bytes < _NOTICEABLE_BYTES:
            text = tr("{name}: hardly anything could be moved. It is in use, shared with other "
                      "programs or already in swap.", name=result.name)
        else:
            text = tr("{name}: {memory} moved to swap.",
                      name=result.name, memory=format_bytes(result.moved_bytes))
        if result.refused:
            text += " " + tr("{count} of its processes protect their memory and were left alone.",
                             count=result.refused)
        self._show_status(text)

    def _on_cache_option_toggled(self, checked: bool) -> None:
        self.settings.memory_cache_option = checked
        self._update_cache_option()

    def _update_cache_option(self) -> None:
        checked = self.cache_check.isChecked()
        self.cache_warning.setVisible(checked)
        self.drop_btn.setVisible(checked)

    def _on_drop_clicked(self) -> None:
        answer = QMessageBox.warning(
            self, tr("Empty cache now"),
            tr("Really empty the cache?\n\nThis does not make the PC faster. Programs and files "
               "load more slowly until the cache has filled up again."),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._start_action(DropCachesWorker(parent=self), self._on_drop_result)

    def _on_drop_result(self, result: DropCachesResult) -> None:
        self._finish_action()
        if result.error:
            self._show_status(tr(result.error))
        else:
            self._show_status(tr("Cache emptied: {before} before, {after} now.",
                                 before=format_bytes(result.before_bytes),
                                 after=format_bytes(result.after_bytes)))

    def shutdown(self) -> None:
        self.chart_timer.stop()
        self._stop_action_worker()
        self.worker.snapshotReady.disconnect(self._on_snapshot)
        self.worker.hardwareReady.disconnect(self._on_hardware)
        self.worker.stop()
        self.worker.deleteLater()
