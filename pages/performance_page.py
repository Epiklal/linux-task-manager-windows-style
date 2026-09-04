"""Performance tab - Windows 10 style: sidebar tiles + large detail chart."""
from __future__ import annotations

import math
from collections import deque

import psutil
from PySide6.QtCore import Qt, QTimer
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
from widgets.live_chart import LiveChart
from widgets.resource_tile import ResourceTile

_DISK_PALETTE = ["#7fba00", "#3a96dd", "#ff8c00", "#e74856", "#00b294", "#767676"]
_GPU_PALETTE = [
    "#c239b3", "#00b7c3", "#7fba00", "#ff8c00", "#e74856",
    "#68217a", "#3a96dd", "#00cc6a", "#767676", "#ffb900",
]
_GPU_MAX_GRAPHS = 10
_GPU_GRAPH_COLUMNS = 2
_GPU_DEFAULT_METRIC_ORDER = [
    "3D", "Kopieren", "Video-Decode", "Video-Encode", "Compute", "JPEG",
    "Speicher-Controller", "Auslastung",
]


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


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

        self.title_label = QLabel(metric)
        self.title_label.setObjectName("tileTitle")
        self.value_label = QLabel("")
        self.value_label.setObjectName("tileValue")
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        title_row.addWidget(self.value_label)

        self.chart = LiveChart(max_points=60, line_color=color)
        self.chart.setFixedHeight(105)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        layout.addLayout(title_row)
        layout.addWidget(self.chart)

    def set_metric(self, metric: str) -> None:
        self.metric_name = metric
        self.title_label.setText(metric)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.on_context_menu:
            self.on_context_menu(self, event.globalPos())


class PerformancePage(QWidget):
    def __init__(self, smart_sudo_enabled: bool = False, parent=None):
        super().__init__(parent)

        self._latest_gpu: dict | None = None
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
                f"Datenträger {i} ({d['device']})", color
            )
        self.tiles["network"] = ResourceTile("Netzwerk", "#e3a21a")
        self.tiles["gpu"] = ResourceTile("GPU", "#c239b3")

        self._tile_order = ["cpu", "memory", *disk_keys, "network", "gpu"]

        nav_side = QVBoxLayout()
        nav_side.setContentsMargins(0, 0, 0, 0)
        nav_side.setSpacing(1)
        for key in self._tile_order:
            nav_side.addWidget(self.tiles[key])
        nav_container = QWidget()
        nav_container.setLayout(nav_side)
        nav_container.setFixedWidth(205)

        self.detail_title = QLabel("CPU")
        self.detail_title.setObjectName("detailTitle")
        self.core_toggle_btn = QPushButton("Pro Kern")
        self.core_toggle_btn.setCheckable(True)
        self.core_toggle_btn.toggled.connect(self._on_core_toggle)
        title_row = QHBoxLayout()
        title_row.addWidget(self.detail_title)
        title_row.addStretch(1)
        title_row.addWidget(self.core_toggle_btn)

        self.detail_chart = LiveChart(max_points=60, line_color="#00b7c3")
        self._core_count = psutil.cpu_count(logical=True) or 1
        self._core_histories = [deque([0.0] * 60, maxlen=60) for _ in range(self._core_count)]
        self._core_charts: list[LiveChart] = []
        self.cores_widget = QWidget()
        cores_grid = QGridLayout(self.cores_widget)
        cores_grid.setContentsMargins(0, 4, 0, 4)
        cores_grid.setHorizontalSpacing(5)
        cores_grid.setVerticalSpacing(10)
        columns = min(8, self._core_count)
        for i in range(self._core_count):
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            label = QLabel(f"CPU {i}")
            label.setObjectName("tileTitle")
            chart = LiveChart(max_points=60, line_color="#00b7c3")
            chart.set_data(self._core_histories[i])
            chart.setFixedHeight(78)
            cell_layout.addWidget(label)
            cell_layout.addWidget(chart)
            cores_grid.addWidget(cell, i // columns, i % columns)
            self._core_charts.append(chart)

        self.chart_stack = QStackedLayout()
        self.chart_stack.addWidget(self.detail_chart)
        self.chart_stack.addWidget(self.cores_widget)

        self._gpu_metric_order: list[str] = ["Auslastung", *GPU_ENGINE_METRICS]
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
        self.gpu_vram_slot = GpuGraphSlot("Grafikspeicher (VRAM)", "#c239b3", fixed=True)
        self.gpu_vram_slot.chart.set_data(self.gpu_vram_history)
        self.gpu_vram_slot.on_context_menu = self._show_gpu_context_menu
        self._rebuild_gpu_slots()

        self.chart_stack.addWidget(self.gpu_graphs_widget)
        chart_area = QWidget()
        chart_area.setLayout(self.chart_stack)

        self.detail_info = QLabel("")
        self.detail_info.setObjectName("detailInfo")
        self.detail_info.setTextFormat(Qt.RichText)

        detail_layout = QVBoxLayout()
        detail_layout.addLayout(title_row)
        detail_layout.addWidget(chart_area, stretch=1)
        detail_layout.addWidget(self.detail_info)
        detail_container = QFrame()
        detail_container.setLayout(detail_layout)

        for key, tile in self.tiles.items():
            tile.mousePressEvent = lambda _e, k=key: self._select(k)

        root = QHBoxLayout(self)
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
        self._last_text: dict[str, str] = {key: "" for key in self._tile_order}
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
            slot = GpuGraphSlot(metric, _GPU_PALETTE[i % len(_GPU_PALETTE)])
            slot.on_context_menu = self._show_gpu_context_menu
            slot.chart.set_data(self._gpu_history[metric])
            self.gpu_graphs_layout.addWidget(slot, i // _GPU_GRAPH_COLUMNS, i % _GPU_GRAPH_COLUMNS)
            self._gpu_slots.append(slot)
        vram_row = (self._gpu_graph_count + _GPU_GRAPH_COLUMNS - 1) // _GPU_GRAPH_COLUMNS
        self.gpu_graphs_layout.addWidget(self.gpu_vram_slot, vram_row, 0, 1, _GPU_GRAPH_COLUMNS)

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
        self.detail_chart.set_max_value(100.0)
        self.detail_chart.set_line_color(self._colors[key])
        self.detail_chart.set_data(self._history[key])
        self.detail_info.setText(self._last_text[key])
        self.core_toggle_btn.setVisible(key == "cpu")
        self._update_chart_stack()

    def _on_gpu_snapshot_ready(self, gpu: dict | None) -> None:
        self._latest_gpu = gpu

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
        snap = system_snapshot()

        self.tiles["cpu"].update_value(snap["cpu_percent"])
        self.tiles["memory"].update_value(snap["mem_percent"])

        for i, pct in enumerate(snap["cpu_per_core"][: self._core_count]):
            self._core_histories[i].append(max(0.0, min(pct, 100.0)))
        if self._selected == "cpu" and self._show_per_core:
            for chart in self._core_charts:
                chart.advance_grid()
                chart.update()

        elapsed = self.timer.interval() / 1000

        total_rate = sent_rate = recv_rate = None
        if self._prev_net is not None:
            sent_rate = (snap["net_sent"] - self._prev_net[0]) / 1024 / elapsed
            recv_rate = (snap["net_recv"] - self._prev_net[1]) / 1024 / elapsed
            total_rate = sent_rate + recv_rate
            self.tiles["network"].update_value(
                min(total_rate, 100), f"{total_rate:.0f} KB/s"
            )
        self._prev_net = (snap["net_sent"], snap["net_recv"])

        net_info = get_primary_network_info()

        gpu = self._latest_gpu
        gpu_percent = gpu["percent"] if gpu else 0.0
        self.tiles["gpu"].update_value(gpu_percent)
        self._update_gpu_graphs(gpu, gpu_percent)

        cpu_freq_ghz = (snap["cpu_freq_mhz"] or 0) / 1000
        cpu_temp = get_cpu_temperature()
        cpu_power = self._cpu_power_monitor.poll_watts()
        values = {
            "cpu": (
                snap["cpu_percent"],
                f"{self._cpu_model}<br>"
                f"Auslastung: {snap['cpu_percent']:.0f}%<br>"
                f"Geschwindigkeit: {cpu_freq_ghz:.2f} GHz<br>"
                f"Kerne: {snap['cpu_cores_physical']}  "
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
                min(total_rate, 100) if total_rate is not None else None,
                (
                    f"Adapter: {net_info['name']}<br>"
                    f"Senden: {sent_rate:.0f} KB/s<br>"
                    f"Empfangen: {recv_rate:.0f} KB/s<br>"
                    f"Verbindungsgeschwindigkeit: "
                    f"{_format_or_na(net_info['speed_mbps'], '.0f', ' Mbit/s')}<br>"
                    f"IPv4-Adresse: {net_info['ipv4'] or 'Unbekannt'}<br>"
                    f"Gesamt gesendet: {snap['net_sent'] / (1024 ** 2):.0f} MB<br>"
                    f"Gesamt empfangen: {snap['net_recv'] / (1024 ** 2):.0f} MB"
                ) if total_rate is not None and net_info else "",
            ),
            "gpu": (
                gpu_percent,
                self._format_gpu_text(gpu, gpu_percent),
            ),
        }
        for i, d in enumerate(self._disks):
            key = f"disk:{d['device']}"
            values[key] = self._disk_stat(key, d, elapsed)

        for key, (value, text) in values.items():
            if value is not None:
                self._history[key].append(max(0.0, min(value, 100.0)))
                self._last_text[key] = text

        value, text = values[self._selected]
        if value is not None:
            self.detail_info.setText(text)
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
        dedicated = (f"{vram_used:.0f}/{vram_total:.0f} MB"
                     if vram_used is not None and vram_total is not None else "Nicht verfügbar")
        shared = (f"{gtt_used:.0f}/{gtt_total:.0f} MB"
                  if gtt_used is not None and gtt_total is not None else "Nicht verfügbar")
        return (
            f"{gpu.get('name', 'GPU')}<br>"
            f"Auslastung: {gpu_percent:.0f}%<br>"
            f"Dedizierter Grafikspeicher: {dedicated}<br>"
            f"Gemeinsam genutzter Grafikspeicher (RAM): {shared}<br>"
            f"Temperatur: {_format_or_na(gpu.get('temp_c'), '.0f', '°C')}<br>"
            f"Leistungsaufnahme: {_format_or_na(gpu.get('power_w'), '.1f', ' W')}"
        )

    def _update_gpu_graphs(self, gpu: dict | None, gpu_percent: float) -> None:
        engines = (gpu or {}).get("engines") or {}
        for label in engines:
            self._ensure_gpu_metric_history(label)

        current_values = {"Auslastung": gpu_percent, **engines}
        for metric, value in current_values.items():
            history = self._gpu_history.get(metric)
            if history is not None:
                history.append(max(0.0, min(value, 100.0)))

        show_gpu = self._selected == "gpu"
        for slot in self._gpu_slots:
            value = current_values.get(slot.metric_name)
            slot.value_label.setText(f"{value:.0f}%" if value is not None else "–")
            if show_gpu:
                slot.chart.update()

        vram_used = gpu.get("vram_used_mb") if gpu else None
        vram_total = gpu.get("vram_total_mb") if gpu else None
        vram_percent = (vram_used / vram_total * 100) if vram_used is not None and vram_total else 0.0
        self.gpu_vram_history.append(max(0.0, min(vram_percent, 100.0)))
        if vram_used is not None and vram_total is not None:
            self.gpu_vram_slot.value_label.setText(f"{vram_used:.0f}/{vram_total:.0f} MB")
        else:
            self.gpu_vram_slot.value_label.setText("Nicht verfügbar")
        if show_gpu:
            self.gpu_vram_slot.chart.update()

    def shutdown(self) -> None:
        self.timer.stop()
        if self._gpu_worker is not None:
            self._gpu_worker.stop()
            self._gpu_worker.deleteLater()
        if self._smart_worker is not None:
            self._smart_worker.stop()
            self._smart_worker.deleteLater()

    def set_theme(self, dark: bool) -> None:
        for tile in self.tiles.values():
            tile.set_theme(dark)
        self.detail_chart.set_theme(dark)
        for chart in self._core_charts:
            chart.set_theme(dark)
        for slot in self._gpu_slots:
            slot.chart.set_theme(dark)
        self.gpu_vram_slot.chart.set_theme(dark)

    def set_refresh_interval(self, milliseconds: int) -> None:
        self.timer.setInterval(milliseconds)

