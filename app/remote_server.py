"""Headless monitoring server for the desktop Task Manager client."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
# Benchmark requests are tiny JSON objects; anything bigger is refused.
MAX_REQUEST_BYTES = 16 * 1024
MAX_BENCHMARK_SECONDS = 300
MAX_PRIME_LIMIT = 10_000_000


class BenchmarkBusyError(Exception):
    """Another benchmark is still running on this server."""


class MonitoringHandler(BaseHTTPRequestHandler):
    monitor: object
    snapshot: object
    token: str | None
    benchmark: object

    def _authorized(self) -> bool:
        token = type(self).token
        if not token:
            return True  # only when started with --no-auth
        supplied = self.headers.get("Authorization", "")
        # Constant-time comparison, so the token can't be guessed byte by byte.
        return secrets.compare_digest(supplied.encode("utf-8", "replace"),
                                      f"Bearer {token}".encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 (HTTP handler override)
        if self.path != "/api/v1/snapshot":
            self._send_json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, 401)
            return
        snapshot = type(self).snapshot()
        payload = {
            "version": 1,
            "timestamp": time.time(),
            "hostname": socket.gethostname(),
        }
        payload.update(snapshot)
        self._send_json(payload)

    def do_POST(self) -> None:  # noqa: N802 (HTTP handler override)
        if self.path != "/api/v1/benchmark":
            self._send_json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._send_json({"error": "invalid request size"}, 413)
            return
        try:
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("Anfrage muss ein JSON-Objekt sein")
            result = type(self).benchmark(request)
            self._send_json(result)
        except BenchmarkBusyError as error:
            self._send_json({"error": str(error)}, 409)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
            self._send_json({"error": str(error)}, 400)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
               token: str | None = None) -> None:
    from app.system_info import (
        AutostartEntry,
        CpuPowerMonitor,
        DockerWorker,
        GpuSystemMonitor,
        ProcessWorker,
        get_cpu_model_name,
        get_cpu_temperature,
        list_physical_disks,
        list_autostart_entries,
        list_hardware_sensors,
        system_snapshot,
    )
    from pages.services_page import ServiceListWorker
    from pages.benchmarks_page import BenchmarkWorker

    MonitoringHandler.monitor = GpuSystemMonitor()
    MonitoringHandler.snapshot = system_snapshot
    process_worker = ProcessWorker()
    docker_worker = DockerWorker()
    service_worker = ServiceListWorker()
    cpu_power_monitor = CpuPowerMonitor()
    # Power is the energy delta between two polls; parallel requests must not interleave.
    power_lock = threading.Lock()

    def full_snapshot() -> dict:
        process_worker._sync_process_table()
        processes = [asdict(row) for row in process_worker._collect_rows()]
        docker_rows, docker_error = docker_worker._collect_rows()
        with power_lock:
            gpu = MonitoringHandler.monitor.poll()
            cpu_power = cpu_power_monitor.poll_watts()
            cpu_power_reason = cpu_power_monitor.unavailable_reason()
        return {
            "system": system_snapshot(),
            "cpu_model": get_cpu_model_name(),
            "cpu_temp_c": get_cpu_temperature(),
            "cpu_power_w": cpu_power,
            "cpu_power_reason": cpu_power_reason,
            "gpu": gpu,
            "processes": processes,
            "sensors": list_hardware_sensors(),
            "services": service_worker._fetch(),
            "docker": [asdict(row) for row in docker_rows],
            "docker_error": docker_error,
            "autostart": [asdict(entry) for entry in list_autostart_entries()],
            "disks": list_physical_disks(),
        }

    MonitoringHandler.snapshot = full_snapshot

    benchmark_lock = threading.Lock()

    def run_benchmark(request: dict) -> dict:
        kind = request.get("kind")
        if kind not in {"cpu", "memory", "gpu", "disk"}:
            raise ValueError("Unbekannter Benchmark")
        # Clamp everything a client can send, so a request can't keep the server
        # busy for days or spawn thousands of threads.
        cpu_count = os.cpu_count() or 1
        cpu_threads = min(max(int(request.get("cpu_threads", 1)), 1), cpu_count)
        cpu_seconds = min(max(int(request.get("cpu_seconds", 30)), 1), MAX_BENCHMARK_SECONDS)
        cpu_max_prime = min(max(int(request.get("cpu_max_prime", 1_000_000)), 100),
                            MAX_PRIME_LIMIT)
        # The disk test writes a 256 MiB file: only into a mount point of a
        # physical disk, never into an arbitrary client-chosen directory.
        allowed_dirs = [mount for disk in list_physical_disks()
                        for mount in disk.get("mountpoints") or []]
        disk_directory = request.get("disk_directory")
        if disk_directory not in allowed_dirs:
            if kind == "disk" and disk_directory:
                raise ValueError(f"Ziel nicht erlaubt: {disk_directory}")
            disk_directory = allowed_dirs[0] if allowed_dirs else None
        if not benchmark_lock.acquire(blocking=False):
            raise BenchmarkBusyError("Auf dem Server läuft bereits ein Benchmark")
        try:
            worker = BenchmarkWorker(
                [kind], cpu_threads, cpu_seconds, cpu_max_prime, disk_directory)
            result, details = getattr(worker, f"_run_{kind}")()
        finally:
            benchmark_lock.release()
        return {"label": {"cpu": "CPU", "memory": "Arbeitsspeicher",
                           "gpu": "GPU", "disk": "Datenträger"}[kind],
                "result": result, "details": details}

    MonitoringHandler.benchmark = run_benchmark
    MonitoringHandler.token = token
    server = ThreadingHTTPServer((host, port), MonitoringHandler)
    display_host = host if host not in ("0.0.0.0", "::") else _local_ip()
    print(f"WinTaskManager Server läuft auf http://{display_host}:{port}", flush=True)
    if token:
        print(f"Zugriffsschlüssel: {token}", flush=True)
    else:
        print("WARNUNG: Server läuft OHNE Zugriffsschlüssel (--no-auth). Jeder, der "
              "diesen Port erreicht, kann alle Daten lesen und Benchmarks starten.",
              flush=True)
    print("Beenden mit Strg+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer wird beendet.", flush=True)
    finally:
        server.server_close()


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def server_from_args(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="WinTaskManager Monitoring-Server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", help="Eigener Bearer-Schlüssel (sonst wird einer erzeugt)")
    parser.add_argument("--generate-token", action="store_true",
                        help="Zufälligen Zugriffsschlüssel erzeugen (Standard)")
    parser.add_argument("--no-auth", action="store_true",
                        help="Ohne Zugriffsschlüssel starten (nur in vertrauenswürdigen Netzen!)")
    args = parser.parse_args(argv)
    if args.no_auth and args.token:
        parser.error("--no-auth und --token schließen sich aus")
    # A token is always required unless explicitly disabled.
    token = None if args.no_auth else (args.token or secrets.token_urlsafe(24))
    run_server(args.host, args.port, token)
