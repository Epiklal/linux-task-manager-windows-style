"""Hardware benchmark page backed by established Linux benchmark tools."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

from PySide6.QtCore import Signal
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

from app.i18n import N_, format_decimal, tr
from app.system_info import StoppableWorker, WorkerStopped, list_physical_disks
from app.remote_client import RemoteBenchmarkWorker

_BENCHMARKS = {
    "cpu": (N_("CPU"), "sysbench"),
    "memory": (N_("Memory"), "sysbench"),
    "gpu": (N_("GPU"), "glmark2"),
    "disk": (N_("Disk"), "fio"),
}


def _writable_dir_on(mountpoint: str) -> str:
    """A directory the user may write to on the same filesystem as `mountpoint`.

    Mount points like "/" or "/boot/efi" usually belong to root, so the test file
    goes into e.g. ~/.cache or /var/tmp on that disk instead. /tmp only counts if it
    really is on that disk (it is often a RAM disk, which would measure memory)."""
    try:
        device = os.stat(mountpoint).st_dev
    except OSError as error:
        raise ValueError(tr("Target not reachable: {mountpoint}", mountpoint=mountpoint)) from error
    home = os.path.expanduser("~")
    candidates = [mountpoint, os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"),
                  home, "/var/tmp", "/tmp"]
    for candidate in candidates:
        try:
            if os.stat(candidate).st_dev == device and os.access(candidate, os.W_OK):
                return candidate
        except OSError:
            continue
    raise ValueError(tr("No write permission on {mountpoint} - please choose another target", mountpoint=mountpoint))


class BenchmarkWorker(StoppableWorker):
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
            if self._stop_event.is_set():
                return
            label, command = _BENCHMARKS[benchmark]
            label = tr(label)
            if not shutil.which(command):
                self.resultReady.emit(label, tr("Not available"), tr("{command} is not installed", command=command))
                continue
            self.resultReady.emit(label, tr("Running"), "")
            try:
                result, details = getattr(self, f"_run_{benchmark}")()
                self.resultReady.emit(label, result, details)
            except WorkerStopped:
                return
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
                self.resultReady.emit(label, tr("Failed"), str(error))
        self.finished.emit()

    def _run_cpu(self) -> tuple[str, str]:
        output = self._run_command(
            [
                "sysbench", "cpu", f"--threads={self._cpu_threads}",
                f"--cpu-max-prime={self._cpu_max_prime}", "--time=" + str(self._cpu_seconds),
                "run",
            ], timeout=self._cpu_seconds + 30, check=True,
        ).stdout
        match = re.search(r"events per second:\s+([\d.]+)", output)
        if not match:
            raise ValueError(tr("Could not read the CPU result"))
        return (tr("{value} events/s", value=format_decimal(float(match.group(1)))),
            tr("sysbench CPU, {threads} threads, {seconds} s, primes up to {prime}",
               threads=self._cpu_threads, seconds=self._cpu_seconds,
               prime=f"{self._cpu_max_prime:,}"))

    def _run_memory(self) -> tuple[str, str]:
        output = self._run_command(
            ["sysbench", "memory", "--memory-block-size=1M", "--memory-total-size=1G", "run"],
            timeout=60, check=True,
        ).stdout
        match = re.search(r"\(([\d.]+) MiB/sec\)", output)
        if not match:
            raise ValueError(tr("Could not read the RAM result"))
        return f"{float(match.group(1)):.0f} MiB/s", tr("sysbench memory, 1 GiB")

    def _run_gpu(self) -> tuple[str, str]:
        output = self._run_command(
            ["glmark2", "--off-screen", "--benchmark", "refract"], timeout=40, check=True,
        ).stdout
        match = re.search(r"glmark2 Score:\s*(\d+)", output)
        if not match:
            raise ValueError(tr("Could not read the GPU result"))
        return tr("Score {score}", score=match.group(1)), tr("glmark2 offscreen, Refract scene")

    def _run_disk(self) -> tuple[str, str]:
        if not self._disk_directory:
            raise ValueError(tr("No mounted disk target selected"))
        work_dir = _writable_dir_on(self._disk_directory)
        with tempfile.TemporaryDirectory(
            prefix="wintaskmanager-benchmark-", dir=work_dir,
        ) as temp_dir:
            output = self._run_command(
                [
                    "fio", "--name=read-test", "--directory=" + temp_dir,
                    "--rw=read", "--bs=1M", "--size=256M", "--time_based", "--runtime=10",
                    "--direct=0", "--group_reporting", "--output-format=json",
                ],
                timeout=45, check=True,
            ).stdout
        data = json.loads(output)
        bandwidth = data["jobs"][0]["read"]["bw_bytes"]
        return (f"{bandwidth / (1024 ** 2):.0f} MiB/s",
            tr("fio read on {directory}, temporary 256 MiB file", directory=self._disk_directory))


class BenchmarksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: BenchmarkWorker | None = None
        self._remote_config = None

        self.checkboxes = {}
        controls = QHBoxLayout()
        for key, (label, _) in _BENCHMARKS.items():
            checkbox = QCheckBox(tr(label))
            checkbox.setChecked(True)
            self.checkboxes[key] = checkbox
            controls.addWidget(checkbox)
        self.cpu_threads = QSpinBox()
        self.cpu_threads.setRange(1, os.cpu_count() or 1)
        self.cpu_threads.setValue(os.cpu_count() or 1)
        self.cpu_threads.setSuffix(tr(" CPU threads"))
        controls.addWidget(self.cpu_threads)
        self.cpu_seconds = QSpinBox()
        self.cpu_seconds.setRange(5, 300)
        self.cpu_seconds.setValue(30)
        self.cpu_seconds.setSuffix(tr(" s CPU test"))
        controls.addWidget(self.cpu_seconds)
        self.cpu_max_prime = QSpinBox()
        self.cpu_max_prime.setRange(10_000, 10_000_000)
        self.cpu_max_prime.setSingleStep(100_000)
        self.cpu_max_prime.setValue(1_000_000)
        self.cpu_max_prime.setSuffix(tr(" prime limit"))
        controls.addWidget(self.cpu_max_prime)
        self.disk_target = QComboBox()
        self._populate_disk_targets()
        controls.addWidget(self.disk_target)
        controls.addStretch(1)
        self.start_button = QPushButton(tr("Start selected"))
        self.start_button.clicked.connect(self.start_selected)
        controls.addWidget(self.start_button)

        self.status = QLabel(tr("Ready"))
        self.status.setObjectName("detailInfo")
        self.results = QTableWidget(0, 3)
        self.results.setHorizontalHeaderLabels([tr("Area"), tr("Result"), tr("Test")])
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
            QMessageBox.information(self, tr("Benchmarks"), tr("Please select at least one test."))
            return
        self.results.setRowCount(0)
        self.start_button.setEnabled(False)
        self.status.setText(tr("Benchmark running. The interface stays usable."))
        if self._remote_config:
            host, port, token = self._remote_config
            self.worker = RemoteBenchmarkWorker(
                host, port, token, selected, self.cpu_threads.value(),
                self.cpu_seconds.value(), self.cpu_max_prime.value(),
                None, self,
            )
            self.worker.resultReady.connect(self._add_result)
            self.worker.finished.connect(self._on_finished)
            self.worker.start()
            return
        self.worker = BenchmarkWorker(
            selected, self.cpu_threads.value(), self.cpu_seconds.value(),
            self.cpu_max_prime.value(), self.disk_target.currentData(), self,
        )
        self.worker.resultReady.connect(self._add_result)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def set_remote_connection(self, enabled: bool, host: str, port: int, token: str) -> None:
        self._remote_config = (host, port, token) if enabled else None
        self.status.setText(tr("Remote benchmark active") if enabled else tr("Local benchmarks active"))

    def _add_result(self, area: str, result: str, details: str) -> None:
        # One row per test: the "Running" row is replaced by the result.
        row = next((r for r in range(self.results.rowCount())
                    if self.results.item(r, 0).text() == area
                    and self.results.item(r, 1).text() == tr("Running")), None)
        if row is None:
            row = self.results.rowCount()
            self.results.insertRow(row)
        for column, value in enumerate((area, result, details)):
            self.results.setItem(row, column, QTableWidgetItem(value))

    def _on_finished(self) -> None:
        self.status.setText(tr("Benchmark finished"))
        self.start_button.setEnabled(True)

    def _populate_disk_targets(self) -> None:
        self.disk_target.addItem(tr("Select disk target"), None)
        for disk in list_physical_disks():
            for mountpoint in disk["mountpoints"]:
                label = f"{disk['model']} ({disk['device']}) - {mountpoint}"
                self.disk_target.addItem(label, mountpoint)

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.resultReady.disconnect(self._add_result)
            self.worker.finished.disconnect(self._on_finished)
            self.worker.stop()
            self.worker.deleteLater()
            self.worker = None