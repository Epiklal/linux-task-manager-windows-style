"""Hardware benchmark page backed by established Linux benchmark tools."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.system_info import list_physical_disks

_BENCHMARKS = {
    "cpu": ("CPU", "sysbench"),
    "memory": ("Arbeitsspeicher", "sysbench"),
    "gpu": ("GPU", "glmark2"),
    "disk": ("Datenträger", "fio"),
}


class BenchmarkWorker(QThread):
    resultReady = Signal(str, str, str)
    finished = Signal()

    def __init__(self, benchmarks: list[str], cpu_threads: int, cpu_seconds: int,
                 cpu_max_prime: int, disk_directory: str | None, parent=None):
        super().__init__(parent)
        self._benchmarks = benchmarks
        self._cpu_threads = cpu_threads
        self._cpu_seconds = cpu_seconds
        self._cpu_max_prime = cpu_max_prime
        self._disk_directory = disk_directory

    def run(self) -> None:
        for benchmark in self._benchmarks:
            label, command = _BENCHMARKS[benchmark]
            if not shutil.which(command):
                self.resultReady.emit(label, "Nicht verfügbar", f"{command} ist nicht installiert")
                continue
            self.resultReady.emit(label, "Läuft", "")
            try:
                result, details = getattr(self, f"_run_{benchmark}")()
                self.resultReady.emit(label, result, details)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
                self.resultReady.emit(label, "Fehlgeschlagen", str(error))
        self.finished.emit()

    def _run_cpu(self) -> tuple[str, str]:
        output = subprocess.check_output(
            [
                "sysbench", "cpu", f"--threads={self._cpu_threads}",
                f"--cpu-max-prime={self._cpu_max_prime}", "--time=" + str(self._cpu_seconds),
                "run",
            ], text=True, timeout=self._cpu_seconds + 30,
        )
        match = re.search(r"events per second:\s+([\d.]+)", output)
        if not match:
            raise ValueError("CPU-Ergebnis konnte nicht gelesen werden")
        return (f"{float(match.group(1)):.1f} Ereignisse/s",
            f"sysbench CPU, {self._cpu_threads} Threads, {self._cpu_seconds} s, "
            f"Primzahlen bis {self._cpu_max_prime:,}")

    @staticmethod
    def _run_memory() -> tuple[str, str]:
        output = subprocess.check_output(
            ["sysbench", "memory", "--memory-block-size=1M", "--memory-total-size=1G", "run"],
            text=True, timeout=60,
        )
        match = re.search(r"\(([\d.]+) MiB/sec\)", output)
        if not match:
            raise ValueError("RAM-Ergebnis konnte nicht gelesen werden")
        return f"{float(match.group(1)):.0f} MiB/s", "sysbench Speicher, 1 GiB"

    @staticmethod
    def _run_gpu() -> tuple[str, str]:
        output = subprocess.check_output(
            ["glmark2", "--off-screen", "--benchmark", "refract"], text=True, timeout=40,
        )
        match = re.search(r"glmark2 Score:\s*(\d+)", output)
        if not match:
            raise ValueError("GPU-Ergebnis konnte nicht gelesen werden")
        return f"Punktzahl {match.group(1)}", "glmark2 Offscreen, Szene Refract"

    def _run_disk(self) -> tuple[str, str]:
        if not self._disk_directory:
            raise ValueError("Kein eingebundenes Datenträger-Ziel ausgewählt")
        with tempfile.TemporaryDirectory(
            prefix="wintaskmanager-benchmark-", dir=self._disk_directory,
        ) as temp_dir:
            output = subprocess.check_output(
                [
                    "fio", "--name=read-test", "--directory=" + temp_dir,
                    "--rw=read", "--bs=1M", "--size=256M", "--time_based", "--runtime=10",
                    "--direct=0", "--group_reporting", "--output-format=json",
                ],
                text=True, timeout=45,
            )
        data = json.loads(output)
        bandwidth = data["jobs"][0]["read"]["bw_bytes"]
        return (f"{bandwidth / (1024 ** 2):.0f} MiB/s",
            f"fio Lesen auf {self._disk_directory}, temporäre 256-MiB-Datei")


class BenchmarksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: BenchmarkWorker | None = None

        self.checkboxes = {}
        controls = QHBoxLayout()
        for key, (label, _) in _BENCHMARKS.items():
            checkbox = QCheckBox(label)
            checkbox.setChecked(True)
            self.checkboxes[key] = checkbox
            controls.addWidget(checkbox)
        self.cpu_threads = QSpinBox()
        self.cpu_threads.setRange(1, os.cpu_count() or 1)
        self.cpu_threads.setValue(os.cpu_count() or 1)
        self.cpu_threads.setSuffix(" CPU-Threads")
        controls.addWidget(self.cpu_threads)
        self.cpu_seconds = QSpinBox()
        self.cpu_seconds.setRange(5, 300)
        self.cpu_seconds.setValue(30)
        self.cpu_seconds.setSuffix(" s CPU-Test")
        controls.addWidget(self.cpu_seconds)
        self.cpu_max_prime = QSpinBox()
        self.cpu_max_prime.setRange(10_000, 10_000_000)
        self.cpu_max_prime.setSingleStep(100_000)
        self.cpu_max_prime.setValue(1_000_000)
        self.cpu_max_prime.setSuffix(" Primzahlgrenze")
        controls.addWidget(self.cpu_max_prime)
        self.disk_target = QComboBox()
        self._populate_disk_targets()
        controls.addWidget(self.disk_target)
        controls.addStretch(1)
        self.start_button = QPushButton("Ausgewählte starten")
        self.start_button.clicked.connect(self.start_selected)
        controls.addWidget(self.start_button)

        self.status = QLabel("Bereit")
        self.status.setObjectName("detailInfo")
        self.results = QTableWidget(0, 3)
        self.results.setHorizontalHeaderLabels(["Bereich", "Ergebnis", "Test"])
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.verticalHeader().setVisible(False)
        self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.results.setColumnWidth(0, 180)
        self.results.setColumnWidth(1, 200)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.status)
        layout.addWidget(self.results)

    def start_selected(self) -> None:
        selected = [key for key, checkbox in self.checkboxes.items() if checkbox.isChecked()]
        if not selected:
            QMessageBox.information(self, "Benchmarks", "Bitte mindestens einen Test auswählen.")
            return
        self.results.setRowCount(0)
        self.start_button.setEnabled(False)
        self.status.setText("Benchmark läuft. Die Oberfläche bleibt bedienbar.")
        self.worker = BenchmarkWorker(
            selected, self.cpu_threads.value(), self.cpu_seconds.value(),
            self.cpu_max_prime.value(), self.disk_target.currentData(), self,
        )
        self.worker.resultReady.connect(self._add_result)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _add_result(self, area: str, result: str, details: str) -> None:
        row = self.results.rowCount()
        self.results.insertRow(row)
        for column, value in enumerate((area, result, details)):
            self.results.setItem(row, column, QTableWidgetItem(value))

    def _on_finished(self) -> None:
        self.status.setText("Benchmark abgeschlossen")
        self.start_button.setEnabled(True)

    def _populate_disk_targets(self) -> None:
        self.disk_target.addItem("Datenträger-Ziel auswählen", None)
        for disk in list_physical_disks():
            for mountpoint in disk["mountpoints"]:
                label = f"{disk['model']} ({disk['device']}) - {mountpoint}"
                self.disk_target.addItem(label, mountpoint)

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.terminate()
            self.worker.deleteLater()
            self.worker = None