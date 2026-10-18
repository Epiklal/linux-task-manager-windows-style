"""Performance tab - Windows 10 style: sidebar tiles + large detail chart."""
from __future__ import annotations

import math
import re
from collections import deque

import psutil
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from app.system_info import (
    CpuPowerMonitor,
    DiskSmartWorker,
    GPU_ENGINE_METRICS,
    GpuSystemWorker,
    disk_io_counters_per_device,
    disk_usage_for_mounts,
    get_cpu_model_name,
    get_cpu_temperature,
    get_primary_network_info,
    get_ram_speed_mhz,
    get_ram_temperature,
    list_physical_disks,
    system_snapshot,
)
from app.remote_client import RemoteSnapshotWorker
from app.themes import WIN10_CHART_COLORS
from widgets.live_chart import LiveChart
from widgets.resource_tile import ResourceTile

_DISK_PALETTE = ["#7fba00", "#3a96dd", "#ff8c00", "#e74856", "#00b294", "#767676"]
_AXIS_LABELS = {"cpu": "% Auslastung", "memory": "Speicherverwendung",
                "disk": "Aktive Zeit", "network": "Durchsatz", "gpu": "Auslastung"}
# Short values shown large (Windows 10 style) below the chart; the rest goes to the
# key/value list on the right.
_MAX_BIG_STATS = 6
_MAX_BIG_VALUE_LEN = 14
# Always listed on the right, like Windows 10's "Kerne: 16" / "Logische Prozessoren: 32".
_SIDE_KEYS = {"Kerne", "Logische Prozessoren", "Adapter", "IPv4-Adresse",
              "Verbindungsgeschwindigkeit", "Startvorgänge", "Betriebsstunden",
              "Treiber", "Treiberversion", "Physischer Speicherort", "PCIe",
              "Maximale Anbindung", "PCIe-Link", "PCIe-Status", "PCIe-Empfang",
              "PCIe-Senden", "Leistungsaufnahme"}
# Network graphs never scale below this (KBit/s), so idle noise doesn't fill the graph.
_NET_SCALE_FLOOR_KBIT = 100.0
_GPU_MAX_GRAPHS = 10
_GPU_GRAPH_COLUMNS = 2
_GPU_DEFAULT_METRIC_ORDER = [
    "3D", "Kopieren", "Video-Decode", "Video-Encode", "Compute", "JPEG",
    "Speicher-Controller", "PCIe-Empfang", "PCIe-Senden", "Auslastung",
]


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def _format_bits(kbit_s: float, unit_from: float | None = None) -> str:
    """Win10-style throughput: '304 KBit/s', '2,4 MBit/s'. The unit is chosen from
    `unit_from` (default: the value itself) so two values can share one unit."""
    reference = kbit_s if unit_from is None else unit_from
    if reference >= 1_000_000:
        return f"{kbit_s / 1_000_000:.1f}".replace(".", ",") + " GBit/s"
    if reference >= 1000:
        return f"{kbit_s / 1000:.1f}".replace(".", ",") + " MBit/s"
    return f"{kbit_s:.0f} KBit/s"


def _format_gb(mb: float | None) -> str | None:
    return None if mb is None else f"{mb / 1024:.1f}".replace(".", ",")


def _format_or_na(value: float | None, fmt: str, suffix: str = "") -> str:
    return f"{value:{fmt}}{suffix}" if value is not None else "Nicht verfügbar"


class GpuGraphSlot(QWidget):
    """One configurable GPU mini-graph row (like Windows' per-engine graphs).
    Right-click offers 'Graph anzeigen als' (metric) and 'Anzahl Graphen' (count).
    """

    def __init__(self, metric: str, color: str, fixed: bool = False, parent=None):
        super().__init__(parent)
        self.metric_name = metric
        self.fixed = fixed
        self.on_context_menu = None  # callback(slot, global_pos), set by owner

        self.title_label = QLabel()
        self.title_label.setObjectName("tileTitle")
        if not fixed:
            # Like Windows 10: click the engine name to choose what the graph shows.
            self.title_label.setCursor(Qt.PointingHandCursor)
            self.title_label.setToolTip("Klicken, um die angezeigte Engine zu wählen")
        self.value_label = QLabel("")
        self.value_label.setObjectName("tileValue")
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        title_row.addWidget(self.value_label)

        self.chart = LiveChart(max_points=60, line_color=color)
        self.chart.setMinimumHeight(70 if fixed else 60)
        self.set_metric(metric)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        layout.addLayout(title_row)
        layout.addWidget(self.chart, 1)

    def set_metric(self, metric: str) -> None:
        self.metric_name = metric
        self.title_label.setText(metric if self.fixed else f"{metric}  ▾")

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if (not self.fixed and self.on_context_menu and event.button() == Qt.LeftButton
                and self.title_label.geometry().contains(event.position().toPoint())):
            self.on_context_menu(self, event.globalPosition().toPoint())
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.on_context_menu:
            self.on_context_menu(self, event.globalPos())


class StatsPanel(QWidget):
    """Win10-style statistics below the detail chart: short values as large numbers
    with a small grey caption, longer ones as a key/value list to the right. Fed with
    the same "Label: value<br>..." text the page already produces."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signature: tuple | None = None
        self._big_values: list[QLabel] = []
        self._kv_values: list[QLabel] = []
        self._notes: list[QLabel] = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 6, 0, 0)
        self._layout.setSpacing(28)
        self._big_grid = QGridLayout()
        self._big_grid.setHorizontalSpacing(24)
        self._big_grid.setVerticalSpacing(6)
        self._kv_grid = QGridLayout()
        self._kv_grid.setHorizontalSpacing(14)
        self._kv_grid.setVerticalSpacing(4)
        self._layout.addLayout(self._big_grid)
        self._layout.addLayout(self._kv_grid)
        self._layout.addStretch(1)

    @staticmethod
    def _parse(text: str) -> tuple[list[tuple[str, str]], list[str]]:
        pairs, notes = [], []
        for line in re.split(r"<br\s*/?>", text):
            line = re.sub(r"<[^>]+>", "", line).strip()
            if not line:
                continue
            key, sep, value = line.partition(": ")
            if sep:
                pairs.append((key.strip(), value.strip()))
            else:
                notes.append(line)
        # A leading line without "Label: value" is the device name, already shown
        # as the subtitle next to the title.
        lines = [l for l in re.split(r"<br\s*/?>", text) if l.strip()]
        if notes and len(lines) > 1 and ": " not in lines[0]:
            notes.pop(0)
        return pairs, notes

    def set_text(self, text: str) -> None:
        pairs, notes = self._parse(text)
        big_count = 0
        is_big = []
        for key, value in pairs:
            big = (big_count < _MAX_BIG_STATS and len(value) <= _MAX_BIG_VALUE_LEN
                   and key not in _SIDE_KEYS)
            big_count += big
            is_big.append(big)
        signature = (tuple(k for k, _ in pairs), tuple(is_big), len(notes))
        if signature != self._signature:
            self._rebuild(pairs, is_big, len(notes))
            self._signature = signature
        big_iter, kv_iter = iter(self._big_values), iter(self._kv_values)
        for (_key, value), big in zip(pairs, is_big):
            next(big_iter if big else kv_iter).setText(value)
        for label, note in zip(self._notes, notes):
            label.setText(note)

    def _rebuild(self, pairs: list[tuple[str, str]], is_big: list[bool], note_count: int) -> None:
        for grid in (self._big_grid, self._kv_grid):
            while grid.count():
                widget = grid.takeAt(0).widget()
                if widget is not None:
                    widget.deleteLater()
        self._big_values, self._kv_values, self._notes = [], [], []
        big_index = kv_row = 0
        for (key, _value), big in zip(pairs, is_big):
            if big:
                caption = QLabel(key)
                caption.setObjectName("statLabel")
                value_label = QLabel()
                value_label.setObjectName("statValue")
                row, col = divmod(big_index, 3)
                self._big_grid.addWidget(caption, row * 2, col)
                self._big_grid.addWidget(value_label, row * 2 + 1, col)
                self._big_values.append(value_label)
                big_index += 1
            else:
                key_label = QLabel(f"{key}:")
                key_label.setObjectName("statKey")
                value_label = QLabel()
                value_label.setObjectName("statKeyValue")
                value_label.setWordWrap(True)
                # Wide enough that values like "Kein PCIe-Link-Limit erkannt" fit on one line.
                value_label.setMinimumWidth(240)
                self._kv_grid.addWidget(key_label, kv_row, 0, Qt.AlignTop)
                self._kv_grid.addWidget(value_label, kv_row, 1, Qt.AlignTop)
                self._kv_values.append(value_label)
                kv_row += 1
        for _ in range(note_count):
            note = QLabel()
            note.setObjectName("statKeyValue")
            note.setWordWrap(True)
            self._kv_grid.addWidget(note, kv_row, 0, 1, 2)
            self._notes.append(note)
            kv_row += 1
        for grid in (self._big_grid, self._kv_grid):
            for row in range(grid.rowCount()):
                grid.setRowStretch(row, 0)
        # Pack rows to the top instead of spreading them over the panel height.
        self._kv_grid.setRowStretch(kv_row, 1)
        self._big_grid.setRowStretch(self._big_grid.rowCount(), 1)


class PerformancePage(QWidget):
    remoteSnapshotReady = Signal(object)
    def __init__(self, smart_sudo_enabled: bool = False, parent=None):
        super().__init__(parent)

        self._dark = False
        self._chart_bg: str | None = None
        self._chart_colors: dict[str, str] = dict(WIN10_CHART_COLORS)
        self._latest_gpu: dict | None = None
        self._remote_snapshot: dict | None = None
        self._remote_worker: RemoteSnapshotWorker | None = None
        self._remote_enabled = False
        self._gpu_worker = GpuSystemWorker(parent=self)
        self._gpu_worker.snapshotReady.connect(self._on_gpu_snapshot_ready)
        self._gpu_worker.start()
        self._cpu_power_monitor = CpuPowerMonitor()
        self._cpu_model = get_cpu_model_name()
        self._ram_speed_mhz = get_ram_speed_mhz()
        self._ram_temp_c, self._ram_temp_sensor_count = get_ram_temperature()
        self._disks = list_physical_disks() or [{
            "device": "root", "model": "Datenträger", "kind": "", "interface": "",
            "used_bytes": 0, "total_bytes": 0, "temp_c": None, "mountpoints": ["/"],
        }]
        self._smart_disk_stats: dict[str, dict] = {}
        self._smart_sudo_enabled = True
        self._smart_worker = DiskSmartWorker(
            [disk["device"] for disk in self._disks], use_sudo=True, parent=self
        )
        self._smart_worker.snapshotReady.connect(self._on_smart_disk_snapshot_ready)
        self._smart_worker.start()
        disk_keys = [f"disk:{d['device']}" for d in self._disks]

        self.tiles: dict[str, ResourceTile] = {
            "cpu": ResourceTile("CPU", "#00b7c3"),
            "memory": ResourceTile("Arbeitsspeicher", "#68217a"),
        }
        for i, d in enumerate(self._disks):
            color = _DISK_PALETTE[i % len(_DISK_PALETTE)]
            self.tiles[f"disk:{d['device']}"] = ResourceTile(
                f"Datenträger {i}", color, detail=d["device"]
            )
        self.tiles["network"] = ResourceTile("Netzwerk", "#e3a21a")
        self.tiles["gpu"] = ResourceTile("GPU 0", "#c239b3")

        self._tile_order = ["cpu", "memory", *disk_keys, "network", "gpu"]

        nav_side = QVBoxLayout()
        nav_side.setContentsMargins(0, 0, 0, 0)
        nav_side.setSpacing(1)
        for key in self._tile_order:
            nav_side.addWidget(self.tiles[key])
        nav_side.addStretch(1)
        nav_container = QWidget()
        nav_container.setObjectName("perfSidebar")
        nav_container.setAttribute(Qt.WA_StyledBackground, True)
        nav_container.setLayout(nav_side)
        nav_container.setFixedWidth(215)

        self.detail_title = QLabel("CPU")
        self.detail_title.setObjectName("detailTitle")
        self.detail_subtitle = QLabel("")
        self.detail_subtitle.setObjectName("detailSubtitle")
        self.core_toggle_btn = QPushButton("Pro Kern")
        self.core_toggle_btn.setCheckable(True)
        self.core_toggle_btn.toggled.connect(self._on_core_toggle)
        title_row = QHBoxLayout()
        title_row.addWidget(self.detail_title, 0, Qt.AlignBottom)
        title_row.addStretch(1)
        title_row.addWidget(self.detail_subtitle, 0, Qt.AlignBottom)
        title_row.addWidget(self.core_toggle_btn, 0, Qt.AlignBottom)

        self.axis_top_left = QLabel("% Auslastung")
        self.axis_top_right = QLabel("100%")
        self.axis_bottom_left = QLabel("60 Sekunden")
        self.axis_bottom_right = QLabel("0")
        for label in (self.axis_top_left, self.axis_top_right,
                      self.axis_bottom_left, self.axis_bottom_right):
            label.setObjectName("chartAxis")
        axis_top = QHBoxLayout()
        axis_top.addWidget(self.axis_top_left)
        axis_top.addStretch(1)
        axis_top.addWidget(self.axis_top_right)
        axis_bottom = QHBoxLayout()
        axis_bottom.addWidget(self.axis_bottom_left)
        axis_bottom.addStretch(1)
        axis_bottom.addWidget(self.axis_bottom_right)

        self.detail_chart = LiveChart(max_points=60, line_color="#00b7c3")
        self._core_count = psutil.cpu_count(logical=True) or 1
        self._core_histories = [deque([0.0] * 60, maxlen=60) for _ in range(self._core_count)]
        self._core_charts: list[LiveChart] = []
        self.cores_widget = QWidget()
        self._cores_grid = QGridLayout(self.cores_widget)
        # Tightly packed like Windows 10's "logical processors" view.
        self._cores_grid.setContentsMargins(0, 0, 0, 0)
        self._cores_grid.setSpacing(4)
        self._rebuild_core_charts(self._core_count)

        self.chart_stack = QStackedLayout()
        self.chart_stack.addWidget(self.detail_chart)
        self.chart_stack.addWidget(self.cores_widget)

        self._gpu_metric_order: list[str] = [
            "Auslastung", *GPU_ENGINE_METRICS, "PCIe-Empfang", "PCIe-Senden",
        ]
        self._gpu_history: dict[str, deque[float]] = {
            metric: deque([0.0] * 60, maxlen=60) for metric in self._gpu_metric_order
        }
        self._gpu_graph_count = 4
        self._gpu_slots: list[GpuGraphSlot] = []

        self.gpu_graphs_widget = QWidget()
        self.gpu_graphs_layout = QGridLayout(self.gpu_graphs_widget)
        self.gpu_graphs_layout.setContentsMargins(0, 0, 0, 0)
        self.gpu_graphs_layout.setSpacing(4)

        self.gpu_vram_history: deque[float] = deque([0.0] * 60, maxlen=60)
        self.gpu_vram_slot = GpuGraphSlot(
            "Nutzung des dedizierten GPU-Speichers", WIN10_CHART_COLORS["gpu"], fixed=True)
        self.gpu_vram_slot.chart.set_data(self.gpu_vram_history)
        self.gpu_vram_slot.on_context_menu = self._show_gpu_context_menu
        self.gpu_shared_history: deque[float] = deque([0.0] * 60, maxlen=60)
        self.gpu_shared_slot = GpuGraphSlot(
            "Nutzung des gemeinsam genutzten GPU-Speichers", WIN10_CHART_COLORS["gpu"], fixed=True)
        self.gpu_shared_slot.chart.set_data(self.gpu_shared_history)
        self.gpu_shared_slot.on_context_menu = self._show_gpu_context_menu
        self._rebuild_gpu_slots()

        self.chart_stack.addWidget(self.gpu_graphs_widget)
        chart_area = QWidget()
        chart_area.setLayout(self.chart_stack)

        self.detail_info = StatsPanel()

        detail_layout = QVBoxLayout()
        detail_layout.setContentsMargins(14, 8, 14, 10)
        detail_layout.setSpacing(2)
        detail_layout.addLayout(title_row)
        detail_layout.addSpacing(8)
        detail_layout.addLayout(axis_top)
        detail_layout.addWidget(chart_area, stretch=1)
        detail_layout.addLayout(axis_bottom)
        detail_layout.addWidget(self.detail_info)
        detail_container = QFrame()
        detail_container.setLayout(detail_layout)

        for key, tile in self.tiles.items():
            tile.mousePressEvent = lambda _e, k=key: self._select(k)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(nav_container)
        root.addWidget(detail_container, stretch=1)

        self._selected = "cpu"
        self._colors = {
            "cpu": "#00b7c3", "memory": "#68217a", "network": "#e3a21a", "gpu": "#c239b3",
        }
        self._labels = {
            "cpu": "CPU", "memory": "Arbeitsspeicher", "network": "Netzwerk", "gpu": "GPU",
        }
        for i, d in enumerate(self._disks):
            key = f"disk:{d['device']}"
            self._colors[key] = _DISK_PALETTE[i % len(_DISK_PALETTE)]
            self._labels[key] = f"Datenträger {i} ({d['device']})"

        self._history: dict[str, deque[float]] = {
            key: deque([0.0] * 60, maxlen=60) for key in self._tile_order
        }
        # Network: receive (solid) in _history["network"], send (dashed) here; KBit/s.
        self._net_send_history: deque[float] = deque([0.0] * 60, maxlen=60)
        net_tile_chart = self.tiles["network"].chart
        net_tile_chart.set_auto_scale(True, _NET_SCALE_FLOOR_KBIT)
        net_tile_chart.set_data(self._history["network"])
        net_tile_chart.set_secondary_data(self._net_send_history)
        self._last_text: dict[str, str] = {key: "" for key in self._tile_order}
        self._subtitles: dict[str, str] = {key: "" for key in self._tile_order}
        self.detail_chart.set_data(self._history["cpu"])
        self.tiles["cpu"].setProperty("selected", True)

        self._show_per_core = False
        self._prev_net = None
        self._prev_disk_io: dict[str, dict] = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self._tick()
        self.core_toggle_btn.setVisible(True)

    def _on_core_toggle(self, checked: bool) -> None:
        self._show_per_core = checked
        self._update_chart_stack()

    def _rebuild_core_charts(self, count: int) -> None:
        while self._cores_grid.count():
            item = self._cores_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        old_histories = self._core_histories
        self._core_count = count
        self._core_histories = [
            old_histories[i] if i < len(old_histories) else deque([0.0] * 60, maxlen=60)
            for i in range(count)
        ]
        self._core_charts = []
        columns = min(8, count)
        for i in range(count):
            chart = LiveChart(max_points=60, line_color=self._color_for("cpu"))
            chart.set_theme(self._dark, self._chart_bg)
            chart.set_data(self._core_histories[i])
            chart.set_overlay_text(f"CPU {i}")
            chart.setMinimumHeight(30)
            self._cores_grid.addWidget(chart, i // columns, i % columns)
            self._core_charts.append(chart)

    def _update_chart_stack(self) -> None:
        if self._selected == "gpu":
            self.chart_stack.setCurrentIndex(2)
        elif self._selected == "cpu" and self._show_per_core:
            self.chart_stack.setCurrentIndex(1)
        else:
            self.chart_stack.setCurrentIndex(0)

    def _rebuild_gpu_slots(self) -> None:
        while self.gpu_graphs_layout.count():
            item = self.gpu_graphs_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)

        old_metrics = [slot.metric_name for slot in self._gpu_slots]
        self._gpu_slots = []
        for i in range(self._gpu_graph_count):
            metric = old_metrics[i] if i < len(old_metrics) else (
                _GPU_DEFAULT_METRIC_ORDER[i % len(_GPU_DEFAULT_METRIC_ORDER)]
            )
            self._ensure_gpu_metric_history(metric)
            slot = GpuGraphSlot(metric, self._color_for("gpu"))
            slot.chart.set_theme(self._dark, self._chart_bg)
            slot.on_context_menu = self._show_gpu_context_menu
            slot.chart.set_data(self._gpu_history[metric])
            self.gpu_graphs_layout.addWidget(slot, i // _GPU_GRAPH_COLUMNS, i % _GPU_GRAPH_COLUMNS)
            self._gpu_slots.append(slot)
        vram_row = (self._gpu_graph_count + _GPU_GRAPH_COLUMNS - 1) // _GPU_GRAPH_COLUMNS
        # Engine graphs get 3 parts of the height, each memory graph 2 parts.
        for row in range(vram_row):
            self.gpu_graphs_layout.setRowStretch(row, 3)
        self.gpu_graphs_layout.setRowStretch(vram_row, 2)
        self.gpu_graphs_layout.setRowStretch(vram_row + 1, 2)
        self.gpu_graphs_layout.addWidget(self.gpu_vram_slot, vram_row, 0, 1, _GPU_GRAPH_COLUMNS)
        self.gpu_graphs_layout.addWidget(self.gpu_shared_slot, vram_row + 1, 0, 1,
                                         _GPU_GRAPH_COLUMNS)

    def _ensure_gpu_metric_history(self, metric: str) -> None:
        if metric not in self._gpu_history:
            self._gpu_history[metric] = deque([0.0] * 60, maxlen=60)
        if metric not in self._gpu_metric_order:
            self._gpu_metric_order.append(metric)

    def _set_gpu_slot_metric(self, slot: GpuGraphSlot, metric: str) -> None:
        slot.set_metric(metric)
        self._ensure_gpu_metric_history(metric)
        slot.chart.set_data(self._gpu_history[metric])

    def _set_gpu_graph_count(self, count: int) -> None:
        self._gpu_graph_count = count
        self._rebuild_gpu_slots()

    def _show_gpu_context_menu(self, slot: GpuGraphSlot, global_pos) -> None:
        menu = QMenu(self)
        if not slot.fixed:
            change_menu = menu.addMenu("Graph anzeigen als")
            for metric in self._gpu_metric_order:
                action = change_menu.addAction(metric)
                action.setCheckable(True)
                action.setChecked(metric == slot.metric_name)
                action.triggered.connect(
                    lambda _checked=False, m=metric, s=slot: self._set_gpu_slot_metric(s, m)
                )
        count_menu = menu.addMenu("Anzahl Graphen")
        for n in range(1, _GPU_MAX_GRAPHS + 1):
            action = count_menu.addAction(str(n))
            action.setCheckable(True)
            action.setChecked(n == self._gpu_graph_count)
            action.triggered.connect(
                lambda _checked=False, n=n: self._set_gpu_graph_count(n)
            )
        menu.exec(global_pos)

    def _select(self, key: str) -> None:
        self.tiles[self._selected].setProperty("selected", False)
        self.tiles[key].setProperty("selected", True)
        self.tiles[self._selected].style().unpolish(self.tiles[self._selected])
        self.tiles[self._selected].style().polish(self.tiles[self._selected])
        self.tiles[key].style().unpolish(self.tiles[key])
        self.tiles[key].style().polish(self.tiles[key])
        self._selected = key
        self.detail_title.setText(self._labels[key])
        self.detail_subtitle.setText(self._subtitles.get(key, ""))
        self.axis_top_left.setText(_AXIS_LABELS[self._kind(key)])
        self.detail_chart.set_max_value(100.0)
        self.detail_chart.set_line_color(self._color_for(key))
        self.detail_chart.set_data(self._history[key])
        is_network = key == "network"
        self.detail_chart.set_auto_scale(is_network, _NET_SCALE_FLOOR_KBIT)
        self.detail_chart.set_secondary_data(self._net_send_history if is_network else None)
        # The GPU view has its own captions per graph, like Windows 10.
        for label in (self.axis_top_left, self.axis_top_right,
                      self.axis_bottom_left, self.axis_bottom_right):
            label.setVisible(key != "gpu")
        self._update_axis_max()
        self.detail_info.set_text(self._last_text[key])
        self.core_toggle_btn.setVisible(key == "cpu")
        self._update_chart_stack()

    def _update_axis_max(self) -> None:
        if self._selected == "network":
            self.axis_top_right.setText(_format_bits(self.detail_chart.scale_max()))
        else:
            self.axis_top_right.setText("100%")

    @staticmethod
    def _kind(key: str) -> str:
        return "disk" if key.startswith("disk:") else key

    def _color_for(self, key: str) -> str:
        return self._chart_colors[self._kind(key)]

    def _on_gpu_snapshot_ready(self, gpu: dict | None) -> None:
        if not self._remote_enabled:
            self._latest_gpu = gpu

    def _on_remote_snapshot_ready(self, payload: dict) -> None:
        self._remote_snapshot = payload
        self._latest_gpu = payload.get("gpu")
        remote_disks = payload.get("disks")
        if remote_disks:
            for index, local_disk in enumerate(self._disks):
                tile = self.tiles.get(f"disk:{local_disk['device']}")
                if tile is not None:
                    tile.setVisible(index < len(remote_disks))
            selected_disk = self._selected.startswith("disk:")
            if selected_disk:
                selected_index = next(
                    (index for index, disk in enumerate(self._disks)
                     if f"disk:{disk['device']}" == self._selected),
                    len(remote_disks),
                )
                if selected_index >= len(remote_disks):
                    self._select("cpu")
        if self._latest_gpu and self._latest_gpu.get("name"):
            self.tiles["gpu"].set_detail(self._latest_gpu["name"])
        for index, disk in enumerate(payload.get("disks") or []):
            if index >= len(self._disks):
                break
            key = f"disk:{self._disks[index]['device']}"
            if key in self.tiles:
                label = disk.get("model") or disk.get("device") or "Datenträger"
                self.tiles[key].set_detail(label)
        remote_core_count = len((payload.get("system") or {}).get("cpu_per_core") or [])
        if remote_core_count and remote_core_count != self._core_count:
            self._rebuild_core_charts(remote_core_count)
        self.remoteSnapshotReady.emit(payload)

    def _on_remote_error(self, message: str) -> None:
        if self._selected == "cpu":
            self.detail_info.set_text(f"Remote-Server nicht erreichbar: {message}")

    def set_remote_connection(self, enabled: bool, host: str, port: int, token: str) -> None:
        self._remote_enabled = enabled
        if self._remote_worker is not None:
            self._remote_worker.stop()
            self._remote_worker.deleteLater()
            self._remote_worker = None
        self._remote_snapshot = None
        if not enabled:
            self._latest_gpu = None
            for disk in self._disks:
                tile = self.tiles.get(f"disk:{disk['device']}")
                if tile is not None:
                    tile.setVisible(True)
            self.remoteSnapshotReady.emit({"remote_disabled": True})
            return
        self._remote_worker = RemoteSnapshotWorker(host, port, token, parent=self)
        self._remote_worker.snapshotReady.connect(self._on_remote_snapshot_ready)
        self._remote_worker.connectionError.connect(self._on_remote_error)
        self._remote_worker.start()

    def _on_smart_disk_snapshot_ready(self, stats: dict[str, dict]) -> None:
        self._smart_disk_stats = stats

    def set_smart_sudo_enabled(self, enabled: bool) -> None:
        if enabled == self._smart_sudo_enabled:
            return
        self._smart_sudo_enabled = enabled
        self._smart_worker.stop()
        self._smart_worker = DiskSmartWorker(
            [disk["device"] for disk in self._disks], use_sudo=enabled, parent=self
        )
        self._smart_worker.snapshotReady.connect(self._on_smart_disk_snapshot_ready)
        self._smart_worker.start()

    def _tick(self) -> None:
        remote_system = (self._remote_snapshot or {}).get("system")
        snap = remote_system if remote_system is not None else system_snapshot()

        self.tiles["cpu"].update_value(snap["cpu_percent"])
        self.tiles["memory"].update_value(snap["mem_percent"])

        for i, pct in enumerate(snap["cpu_per_core"][: self._core_count]):
            self._core_histories[i].append(max(0.0, min(pct, 100.0)))
        if self._selected == "cpu" and self._show_per_core:
            for chart in self._core_charts:
                chart.advance_grid()
                chart.update()

        elapsed = self.timer.interval() / 1000

        sent_rate = recv_rate = None
        if self._prev_net is not None:
            # KBit/s (1000-based), as Task Manager shows network throughput.
            sent_rate = max(0.0, (snap["net_sent"] - self._prev_net[0]) * 8 / 1000 / elapsed)
            recv_rate = max(0.0, (snap["net_recv"] - self._prev_net[1]) * 8 / 1000 / elapsed)
            self._history["network"].append(recv_rate)
            self._net_send_history.append(sent_rate)
            net_tile = self.tiles["network"]
            shared = max(sent_rate, recv_rate)
            unit = _format_bits(shared).split(" ")[-1]
            net_tile.value_label.setText(
                f"S: {_format_bits(sent_rate, shared).split(' ')[0]} "
                f"E: {_format_bits(recv_rate, shared).split(' ')[0]} {unit}"
            )
            net_tile.chart.advance_grid()
            net_tile.chart.update()
        self._prev_net = (snap["net_sent"], snap["net_recv"])

        net_info = get_primary_network_info()

        gpu = self._latest_gpu
        gpu_percent = (gpu.get("percent") if gpu else None) or 0.0
        self.tiles["gpu"].update_value(gpu_percent)
        self._update_gpu_graphs(gpu, gpu_percent)

        cpu_freq_ghz = (snap["cpu_freq_mhz"] or 0) / 1000
        cpu_temp = get_cpu_temperature()
        cpu_power = self._cpu_power_monitor.poll_watts()
        cpu_model = (
            (self._remote_snapshot or {}).get("cpu_model") or self._cpu_model
        )
        values = {
            "cpu": (
                snap["cpu_percent"],
                f"{cpu_model}<br>"
                f"Auslastung: {snap['cpu_percent']:.0f}%<br>"
                f"Geschwindigkeit: {cpu_freq_ghz:.2f} GHz<br>"
                f"Kerne: {snap['cpu_cores_physical']}<br>"
                f"Logische Prozessoren: {snap['cpu_cores_logical']}<br>"
                f"Temperatur: {_format_or_na(cpu_temp, '.0f', '°C')}<br>"
                f"Leistungsaufnahme: "
                f"{_format_or_na(cpu_power, '.1f', ' W') if cpu_power is not None else self._cpu_power_monitor.unavailable_reason()}<br>"
                f"Prozesse: {snap['process_count']}<br>"
                f"Betriebszeit: {_format_uptime(snap['uptime_seconds'])}",
            ),
            "memory": (
                snap["mem_percent"],
                f"Auslastung: {snap['mem_percent']:.0f}%<br>"
                f"Verwendet: {snap['mem_used_gb']:.1f} GB<br>"
                f"Verfügbar: {snap['mem_available_gb']:.1f} GB<br>"
                f"Zwischenspeicher: {snap['mem_cached_gb']:.1f} GB<br>"
                f"Gesamt: {snap['mem_total_gb']:.1f} GB<br>"
                f"Geschwindigkeit: "
                f"{_format_or_na(self._ram_speed_mhz, '.0f', ' MT/s')}<br>"
                f"RAM-Temperatur: {_format_or_na(self._ram_temp_c, '.0f', '°C')}"
                f"{f' ({self._ram_temp_sensor_count} Module)' if self._ram_temp_sensor_count else ''}",
            ),
            "network": (
                None,  # history is appended above (unscaled KBit/s)
                (
                    f"Adapter: {net_info['name']}<br>"
                    f"Senden: {_format_bits(sent_rate)}<br>"
                    f"Empfangen: {_format_bits(recv_rate)}<br>"
                    f"Verbindungsgeschwindigkeit: "
                    f"{_format_or_na(net_info['speed_mbps'], '.0f', ' Mbit/s')}<br>"
                    f"IPv4-Adresse: {net_info['ipv4'] or 'Unbekannt'}<br>"
                    f"Gesamt gesendet: {snap['net_sent'] / (1024 ** 2):.0f} MB<br>"
                    f"Gesamt empfangen: {snap['net_recv'] / (1024 ** 2):.0f} MB"
                ) if sent_rate is not None and net_info else "",
            ),
            "gpu": (
                gpu_percent,
                self._format_gpu_text(gpu, gpu_percent),
            ),
        }
        for i, d in enumerate(self._disks):
            key = f"disk:{d['device']}"
            remote_disks = (self._remote_snapshot or {}).get("disks") or []
            remote_disk = remote_disks[i] if i < len(remote_disks) else None
            values[key] = self._disk_stat(key, remote_disk or d, elapsed)

        self._subtitles["cpu"] = cpu_model
        self._subtitles["memory"] = f"{snap['mem_total_gb']:.1f} GB".replace(".", ",")
        self._subtitles["network"] = net_info["name"] if net_info else ""
        self._subtitles["gpu"] = (gpu or {}).get("name", "")
        self.tiles["network"].set_detail(self._subtitles["network"])
        self.tiles["gpu"].set_detail(self._subtitles["gpu"])
        for i, d in enumerate(self._disks):
            kind = ", ".join(p for p in (d.get("interface", ""), d.get("kind", "")) if p)
            self._subtitles[f"disk:{d['device']}"] = d.get("model", "") + (f" ({kind})" if kind else "")
        self.detail_subtitle.setText(self._subtitles.get(self._selected, ""))

        for key, (value, text) in values.items():
            if value is not None:
                self._history[key].append(max(0.0, min(value, 100.0)))
            if text:
                self._last_text[key] = text

        _value, text = values[self._selected]
        if text:
            self.detail_info.set_text(text)
        self._update_axis_max()
        self.detail_chart.advance_grid()
        self.detail_chart.update()

    def _disk_stat(self, key: str, disk: dict, elapsed: float) -> tuple[float, str]:
        io_now = disk_io_counters_per_device().get(disk["device"])
        prev = self._prev_disk_io.get(disk["device"])
        active_percent = 0.0
        read_rate = write_rate = 0.0
        if io_now:
            if prev and prev.get("busy_time_ms") is not None and io_now.get("busy_time_ms") is not None:
                delta_busy = io_now["busy_time_ms"] - prev["busy_time_ms"]
                active_percent = max(0.0, min(100.0, delta_busy / (elapsed * 1000) * 100))
            if prev:
                read_rate = (io_now["read_bytes"] - prev["read_bytes"]) / (1024 * 1024) / elapsed
                write_rate = (io_now["write_bytes"] - prev["write_bytes"]) / (1024 * 1024) / elapsed
            self._prev_disk_io[disk["device"]] = io_now

        used_bytes, total_bytes = disk_usage_for_mounts(disk["mountpoints"])
        used_gb = used_bytes / (1024**3)
        total_gb = (total_bytes or disk["total_bytes"]) / (1024**3)

        self.tiles[key].update_value(active_percent)
        info = disk.get("interface", "")
        kind = disk.get("kind", "")
        subtitle = ", ".join(p for p in (info, kind) if p)
        smart = self._smart_disk_stats.get(disk["device"], {})
        temperature = smart.get("temp_c", disk.get("temp_c"))
        read_total = smart.get("read_bytes_total")
        write_total = smart.get("write_bytes_total")
        read_total_text = f"{read_total / (1024 ** 4):.2f} TB" if read_total is not None else "Nicht verfügbar"
        write_total_text = f"{write_total / (1024 ** 4):.2f} TB" if write_total is not None else "Nicht verfügbar"
        text = (
            f"{disk['model']}" + (f" ({subtitle})" if subtitle else "") + "<br>"
            f"Aktivität: {active_percent:.0f}%<br>"
            f"Lesen: {read_rate:.1f} MB/s<br>"
            f"Schreiben: {write_rate:.1f} MB/s<br>"
            f"Insgesamt gelesen: {read_total_text}<br>"
            f"Insgesamt geschrieben: {write_total_text}<br>"
            f"Startvorgänge: {_format_or_na(smart.get('power_cycles'), '.0f')}<br>"
            f"Betriebsstunden: {_format_or_na(smart.get('power_on_hours'), '.0f', ' h')}<br>"
            f"Belegt: {used_gb:.0f}/{total_gb:.0f} GB<br>"
            f"Temperatur: {_format_or_na(temperature, '.0f', '°C')}"
        )
        return active_percent, text

    def _format_gpu_text(self, gpu: dict | None, gpu_percent: float) -> str:
        if not gpu:
            return "Nicht verfügbar"
        vram_used = gpu.get("vram_used_mb")
        vram_total = gpu.get("vram_total_mb")
        gtt_used = gpu.get("gtt_used_mb")
        gtt_total = gpu.get("gtt_total_mb")
        dedicated = (f"{_format_gb(vram_used)}/{_format_gb(vram_total)} GB"
                     if vram_used is not None and vram_total is not None else "Nicht verfügbar")
        shared = (f"{_format_gb(gtt_used)}/{_format_gb(gtt_total)} GB"
                  if gtt_used is not None and gtt_total is not None else "Nicht verfügbar")
        known = [(u, t) for u, t in ((vram_used, vram_total), (gtt_used, gtt_total))
                 if u is not None and t is not None]
        total_memory = (
            f"{_format_gb(sum(u for u, _ in known))}/{_format_gb(sum(t for _, t in known))} GB"
            if known else "Nicht verfügbar"
        )
        pcie = gpu.get("pcie") or {}
        pcie_generation = pcie.get("generation") or "Nicht verfügbar"
        pcie_width = f"x{pcie['current_width']}" if pcie.get("current_width") else "Nicht verfügbar"
        pcie_max = pcie.get("max_generation")
        if pcie_max and pcie.get("max_width"):
            pcie_max = f"{pcie_max} x{pcie['max_width']}"
        else:
            pcie_max = "Nicht verfügbar"
        pcie_capacity = (
            f"{pcie['bandwidth_gb_s']:.2f} GB/s"
            if pcie.get("bandwidth_gb_s") is not None else "Nicht verfügbar"
        )
        current_speed = pcie.get("current_speed_gt_s")
        max_speed = pcie.get("max_speed_gt_s")
        current_width = pcie.get("current_width")
        max_width = pcie.get("max_width")
        link_reduced = (
            current_speed is not None and max_speed is not None
            and current_width is not None and max_width is not None
            and (current_speed < max_speed or current_width < max_width)
        )
        if link_reduced and gpu_percent >= 90:
            pcie_status = "Mögliche PCIe-Limitierung: GPU hoch ausgelastet, Link reduziert"
        elif current_speed is not None and current_width is not None:
            pcie_status = "Kein PCIe-Link-Limit erkannt"
        else:
            pcie_status = "PCIe-Limitierung nicht bestimmbar"
        return (
            f"{gpu.get('name', 'GPU')}<br>"
            f"Auslastung: {gpu_percent:.0f}%<br>"
            f"Dedizierter GPU-Speicher: {dedicated}<br>"
            f"GPU-Speicher: {total_memory}<br>"
            f"Gemeinsam genutzter GPU-Speicher: {shared}<br>"
            f"GPU-Temperatur: {_format_or_na(gpu.get('temp_c'), '.0f', ' °C')}<br>"
            f"Treiber: {gpu.get('driver') or 'Nicht verfügbar'}<br>"
            f"Treiberversion: {gpu.get('driver_version') or 'Nicht verfügbar'}<br>"
            f"Physischer Speicherort: {gpu.get('pci_location') or 'Nicht verfügbar'}<br>"
            f"PCIe: {pcie_generation}, {pcie_width}<br>"
            f"Maximale Anbindung: {pcie_max}<br>"
            f"PCIe-Link: {pcie_capacity} theoretisch<br>"
            f"PCIe-Status: {pcie_status}<br>"
            + (f"PCIe-Empfang: {pcie['rx_mb_s']:.1f} MB/s<br>"
               if pcie.get("rx_mb_s") is not None else "")
            + (f"PCIe-Senden: {pcie['tx_mb_s']:.1f} MB/s<br>"
               if pcie.get("tx_mb_s") is not None else "")
            + f"Leistungsaufnahme: {_format_or_na(gpu.get('power_w'), '.1f', ' W')}"
        )

    def _update_gpu_graphs(self, gpu: dict | None, gpu_percent: float) -> None:
        engines = (gpu or {}).get("engines") or {}
        for label in engines:
            self._ensure_gpu_metric_history(label)

        pcie = (gpu or {}).get("pcie") or {}
        current_values = {
            "Auslastung": gpu_percent,
            **engines,
            "PCIe-Empfang": pcie.get("rx_mb_s"),
            "PCIe-Senden": pcie.get("tx_mb_s"),
        }
        for metric, value in current_values.items():
            history = self._gpu_history.get(metric)
            if history is not None and value is not None:
                history.append(max(0.0, min(value, 100.0)))

        show_gpu = self._selected == "gpu"
        for slot in self._gpu_slots:
            value = current_values.get(slot.metric_name)
            slot.value_label.setText(f"{value:.0f}%" if value is not None else "–")
            # Scroll the grid with the data, like every other Task Manager graph.
            slot.chart.advance_grid()
            if show_gpu:
                slot.chart.update()

        vram_used = gpu.get("vram_used_mb") if gpu else None
        vram_total = gpu.get("vram_total_mb") if gpu else None
        vram_percent = (vram_used / vram_total * 100) if vram_used is not None and vram_total else 0.0
        self.gpu_vram_history.append(max(0.0, min(vram_percent, 100.0)))
        self.gpu_vram_slot.value_label.setText(
            f"{_format_gb(vram_total)} GB" if vram_total else "Nicht verfügbar")
        gtt_used = gpu.get("gtt_used_mb") if gpu else None
        gtt_total = gpu.get("gtt_total_mb") if gpu else None
        gtt_percent = (gtt_used / gtt_total * 100) if gtt_used is not None and gtt_total else 0.0
        self.gpu_shared_history.append(max(0.0, min(gtt_percent, 100.0)))
        self.gpu_shared_slot.value_label.setText(
            f"{_format_gb(gtt_total)} GB" if gtt_total else "Nicht verfügbar")
        for slot in (self.gpu_vram_slot, self.gpu_shared_slot):
            slot.chart.advance_grid()
            if show_gpu:
                slot.chart.update()

    def shutdown(self) -> None:
        self.timer.stop()
        if self._gpu_worker is not None:
            self._gpu_worker.stop()
            self._gpu_worker.deleteLater()
        if self._smart_worker is not None:
            self._smart_worker.stop()
            self._smart_worker.deleteLater()
        if self._remote_worker is not None:
            self._remote_worker.stop()
            self._remote_worker.deleteLater()

    def set_theme(self, dark: bool, chart_background: str | None = None,
                  chart_colors: dict[str, str] | None = None) -> None:
        self._dark = dark
        self._chart_bg = chart_background
        self._chart_colors = dict(chart_colors or WIN10_CHART_COLORS)
        for key, tile in self.tiles.items():
            tile.set_theme(dark, chart_background)
            tile.chart.set_line_color(self._color_for(key))
        self.detail_chart.set_line_color(self._color_for(self._selected))
        for chart in self._core_charts:
            chart.set_line_color(self._color_for("cpu"))
        self.detail_chart.set_theme(dark, chart_background)
        for chart in self._core_charts:
            chart.set_theme(dark, chart_background)
        for slot in [*self._gpu_slots, self.gpu_vram_slot, self.gpu_shared_slot]:
            slot.chart.set_line_color(self._color_for("gpu"))
            slot.chart.set_theme(dark, chart_background)

    def set_refresh_interval(self, milliseconds: int) -> None:
        self.timer.setInterval(milliseconds)

    def set_active(self, active: bool) -> None:
        """Pause sampling while the window is minimized. The history graphs keep
        running while only another tab is shown, like Windows does."""
        if active and not self.timer.isActive():
            self._prev_net = None  # avoid a rate spike from the paused gap
            self._prev_disk_io.clear()
            self.timer.start()
        elif not active:
            self.timer.stop()
        if self._gpu_worker is not None:
            self._gpu_worker.set_paused(not active)

