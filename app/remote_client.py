"""Background client for the headless Lintask server."""
from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal

from app.i18n import current_language, tr


def _http_error_text(error: urllib.error.HTTPError) -> str:
    """Readable message for an error answer of the monitoring server."""
    if error.code == 401:
        return tr("Access token is missing or wrong")
    try:
        return tr(json.loads(error.read().decode("utf-8"))["error"])
    except (OSError, ValueError, KeyError, TypeError):
        return tr("HTTP error {code}", code=error.code)


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
            # The server words its result in this language.
            "language": current_language(),
        }
        self._stop_event = threading.Event()
        self._conn_lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None

    def stop(self) -> None:
        """End the thread without terminate(): shutting the socket down wakes the
        blocked read, which otherwise waits up to 360 s for the server's result."""
        self._stop_event.set()
        with self._conn_lock:
            if self._conn is not None and self._conn.sock is not None:
                try:
                    self._conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.wait()

    def _post(self, body: bytes) -> dict:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.connect()
            conn.sock.settimeout(360)
            with self._conn_lock:
                if self._stop_event.is_set():
                    return {}
                self._conn = conn
            conn.request("POST", "/api/v1/benchmark", body=body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self.token}",
            })
            response = conn.getresponse()
            data = response.read()
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    f"http://{self.host}:{self.port}/api/v1/benchmark", response.status,
                    response.reason, response.headers, io.BytesIO(data),
                )
            return json.loads(data.decode("utf-8"))
        finally:
            with self._conn_lock:
                self._conn = None
            conn.close()

    def run(self) -> None:
        labels = {"cpu": tr("CPU"), "memory": tr("Memory"), "gpu": tr("GPU"), "disk": tr("Disk")}
        try:
            for kind in self.benchmarks:
                if self._stop_event.is_set():
                    return
                self.resultReady.emit(labels[kind], tr("Running"), tr("Server benchmark"))
                result = self._post(json.dumps({"kind": kind, **self.options}).encode())
                if self._stop_event.is_set():
                    return
                # Our own label, not the server's: it has to match the "Running" row.
                self.resultReady.emit(labels[kind], result["result"], result["details"])
        except urllib.error.HTTPError as error:
            if self._stop_event.is_set():
                return
            self.resultReady.emit(tr("Server"), tr("Failed"), _http_error_text(error))
        except (OSError, http.client.HTTPException, ValueError, KeyError) as error:
            if self._stop_event.is_set():
                return
            self.resultReady.emit(tr("Server"), tr("Failed"), str(error))
        self.finished.emit()
