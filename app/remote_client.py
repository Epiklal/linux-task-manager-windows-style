"""Background client for the headless WinTaskManager server."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal


def _http_error_text(error: urllib.error.HTTPError) -> str:
    """Readable message for an error answer of the monitoring server."""
    if error.code == 401:
        return "Zugriffsschlüssel fehlt oder ist falsch"
    try:
        return json.loads(error.read().decode("utf-8"))["error"]
    except (OSError, ValueError, KeyError, TypeError):
        return f"HTTP-Fehler {error.code}"


class RemoteSnapshotWorker(QThread):
    snapshotReady = Signal(object)
    connectionError = Signal(str)

    def __init__(self, host: str, port: int, token: str = "", interval: float = 0.5, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.token = token
        self.interval = interval
        self._running = True

    def stop(self) -> None:
        self._running = False
        self.wait(1000)

    def run(self) -> None:
        while self._running:
            try:
                request = urllib.request.Request(
                    f"http://{self.host}:{self.port}/api/v1/snapshot",
                    headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                )
                with urllib.request.urlopen(request, timeout=min(2.0, self.interval + 1.0)) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.snapshotReady.emit(payload)
            except urllib.error.HTTPError as exc:
                self.connectionError.emit(_http_error_text(exc))
            except (OSError, urllib.error.URLError, ValueError) as exc:
                self.connectionError.emit(str(exc))
            deadline = time.monotonic() + self.interval
            while self._running and time.monotonic() < deadline:
                time.sleep(0.05)


class RemoteBenchmarkWorker(QThread):
    resultReady = Signal(str, str, str)
    finished = Signal()

    def __init__(self, host: str, port: int, token: str, benchmarks: list[str],
                 cpu_threads: int, cpu_seconds: int, cpu_max_prime: int,
                 disk_directory: str | None, parent=None):
        super().__init__(parent)
        self.host, self.port, self.token = host, port, token
        self.benchmarks = benchmarks
        self.options = {
            "cpu_threads": cpu_threads, "cpu_seconds": cpu_seconds,
            "cpu_max_prime": cpu_max_prime, "disk_directory": disk_directory,
        }

    def run(self) -> None:
        labels = {"cpu": "CPU", "memory": "Arbeitsspeicher", "gpu": "GPU", "disk": "Datenträger"}
        try:
            for kind in self.benchmarks:
                self.resultReady.emit(labels[kind], "Läuft", "Server-Benchmark")
                body = json.dumps({"kind": kind, **self.options}).encode()
                request = urllib.request.Request(
                    f"http://{self.host}:{self.port}/api/v1/benchmark",
                    data=body, method="POST",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
                )
                with urllib.request.urlopen(request, timeout=360) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.resultReady.emit(result["label"], result["result"], result["details"])
        except urllib.error.HTTPError as error:
            self.resultReady.emit("Server", "Fehlgeschlagen", _http_error_text(error))
        except (OSError, urllib.error.URLError, ValueError, KeyError) as error:
            self.resultReady.emit("Server", "Fehlgeschlagen", str(error))
        self.finished.emit()
