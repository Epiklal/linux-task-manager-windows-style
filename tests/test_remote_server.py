"""Server mode: authentication, request limits and benchmark clamping, tested over real
HTTP on a loopback port. Everything that reads the system or runs a benchmark is a stub.
"""
import contextlib
import http.client
import importlib.util
import inspect
import io
import json
import os
import socket
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from app import remote_server
from app.i18n import current_language
from app.remote_server import (
    MAX_BENCHMARK_SECONDS,
    MAX_PRIME_LIMIT,
    MAX_REQUEST_BYTES,
    BenchmarkBusyError,
    MonitoringHandler,
    make_benchmark_runner,
)

TOKEN = "correct-token"
BENCHMARK = "/api/v1/benchmark"
SNAPSHOT = "/api/v1/snapshot"
DISKS = [{"mountpoints": ["/", "/data"]}, {"mountpoints": []}, {"mountpoints": None}]


class FakeWorker:
    """Stands in for pages.benchmarks_page.BenchmarkWorker and records what it was given."""
    created: list = []

    def __init__(self, kinds, cpu_threads, cpu_seconds, cpu_max_prime, disk_directory):
        self.cpu_threads = cpu_threads
        self.cpu_seconds = cpu_seconds
        self.cpu_max_prime = cpu_max_prime
        self.disk_directory = disk_directory
        type(self).created.append(self)

    def _run_cpu(self):
        return "1 score", "details"

    _run_memory = _run_gpu = _run_disk = _run_cpu


class ServerTestCase(unittest.TestCase):
    token = TOKEN
    handler_timeout = None

    def setUp(self):
        FakeWorker.created = []
        self.snapshot_calls = 0
        self.benchmark_calls = 0
        self.benchmark = make_benchmark_runner(lambda: DISKS, FakeWorker)

        def snapshot():
            self.snapshot_calls += 1
            return {"system": "stub"}

        def benchmark(request):
            self.benchmark_calls += 1
            return self.benchmark(request)

        attributes = {"token": self.token, "snapshot": staticmethod(snapshot),
                      "benchmark": staticmethod(benchmark)}
        if self.handler_timeout is not None:
            attributes["timeout"] = self.handler_timeout
        handler = type("TestHandler", (MonitoringHandler,), attributes)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, auth=f"Bearer {TOKEN}", headers=None):
        all_headers = dict(headers or {})
        if auth is not None:
            all_headers["Authorization"] = auth
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def post(self, request, **kwargs):
        return self.call("POST", BENCHMARK, json.dumps(request).encode(), **kwargs)


class TestAuthentication(ServerTestCase):
    def test_snapshot_with_correct_token(self):
        status, payload = self.call("GET", SNAPSHOT)
        self.assertEqual(status, 200)
        self.assertEqual(payload["system"], "stub")
        self.assertEqual(payload["version"], 1)

    def test_wrong_credentials_get_no_data(self):
        for auth in (None, "", "Bearer", "Bearer wrong", TOKEN, f"bearer {TOKEN}",
                     f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", f"Basic {TOKEN}"):
            with self.subTest(auth=auth):
                status, payload = self.call("GET", SNAPSHOT, auth=auth)
                self.assertEqual(status, 401)
                self.assertEqual(payload, {"error": "unauthorized"})
        self.assertEqual(self.snapshot_calls, 0)

    def test_non_ascii_credentials_are_refused_not_crashing(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(f"GET {SNAPSHOT} HTTP/1.0\r\n".encode()
                         + "Authorization: Bearer äöü\r\n\r\n".encode("latin-1"))
            self.assertIn(b" 401 ", sock.makefile("rb").readline())
        self.assertEqual(self.snapshot_calls, 0)

    def test_benchmark_needs_the_token_too(self):
        status, _ = self.post({"kind": "cpu"}, auth="Bearer wrong")
        self.assertEqual(status, 401)
        self.assertEqual(self.benchmark_calls, 0)

    def test_auth_is_checked_before_the_request_size(self):
        status, _ = self.call("POST", BENCHMARK, b"", auth=None,
                              headers={"Content-Length": str(MAX_REQUEST_BYTES + 1)})
        self.assertEqual(status, 401)


class TestNoAuth(ServerTestCase):
    token = None

    def test_no_token_needed_only_with_no_auth(self):
        status, _ = self.call("GET", SNAPSHOT, auth=None)
        self.assertEqual(status, 200)


class TestRouting(ServerTestCase):
    def test_unknown_paths(self):
        for method, path in (("GET", "/"), ("GET", f"{SNAPSHOT}/"), ("GET", f"{SNAPSHOT}?x=1"),
                             ("GET", BENCHMARK), ("GET", "/../../etc/passwd"),
                             ("POST", SNAPSHOT), ("POST", "/")):
            with self.subTest(method=method, path=path):
                status, _ = self.call(method, path, b"{}" if method == "POST" else None)
                self.assertEqual(status, 404)
        self.assertEqual(self.snapshot_calls + self.benchmark_calls, 0)


class TestRequestLimits(ServerTestCase):
    def test_invalid_content_length(self):
        for length in ("0", "-1", "abc", str(MAX_REQUEST_BYTES + 1), "99999999999999"):
            with self.subTest(length=length):
                status, _ = self.call("POST", BENCHMARK, b"",
                                      headers={"Content-Length": length})
                self.assertEqual(status, 413)
        self.assertEqual(self.benchmark_calls, 0)

    def test_largest_allowed_request_is_read(self):
        body = json.dumps({"kind": "cpu", "padding": ""}).encode()
        body = body.replace(b'""', b'"' + b"x" * (MAX_REQUEST_BYTES - len(body)) + b'""'[1:])
        self.assertEqual(len(body), MAX_REQUEST_BYTES)
        status, _ = self.call("POST", BENCHMARK, body)
        self.assertEqual(status, 200)

    def test_malformed_requests_get_a_clean_400(self):
        bodies = {
            "invalid json": b"{not json",
            "invalid utf-8": b'{"kind": "\xff\xfe"}',
            "array": b'["cpu"]',
            "null": b"null",
            "unknown kind": b'{"kind": "rm -rf"}',
            "no kind": b"{}",
            "unhashable kind": b'{"kind": ["cpu"]}',
            "text as number": b'{"kind": "cpu", "cpu_threads": "many"}',
            "null as number": b'{"kind": "cpu", "cpu_seconds": null}',
            "infinity": b'{"kind": "cpu", "cpu_threads": Infinity}',
            "exponent overflow": b'{"kind": "cpu", "cpu_seconds": 1e999}',
            "nan": b'{"kind": "cpu", "cpu_max_prime": NaN}',
            "huge integer": b'{"kind": "cpu", "cpu_threads": ' + b"9" * 5000 + b"}",
            "deep nesting": b"[" * MAX_REQUEST_BYTES,
        }
        for name, body in bodies.items():
            with self.subTest(name):
                status, payload = self.call("POST", BENCHMARK, body)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)
        self.assertEqual(FakeWorker.created, [])


class TestSlowClients(ServerTestCase):
    handler_timeout = 0.2

    def test_handler_has_a_timeout_by_default(self):
        self.assertGreater(MonitoringHandler.timeout or 0, 0)

    def test_idle_connection_is_closed(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            self.assertEqual(sock.recv(1), b"")

    def test_body_that_never_arrives_does_not_block_forever(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(f"POST {BENCHMARK} HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n"
                         "Content-Length: 100\r\n\r\n{".encode())
            self.assertIn(b" 400 ", sock.makefile("rb").readline())
        self.assertEqual(FakeWorker.created, [])


class TestBenchmarkClamping(unittest.TestCase):
    def setUp(self):
        FakeWorker.created = []
        self.run_benchmark = make_benchmark_runner(lambda: DISKS, FakeWorker)

    def worker_for(self, **request):
        self.run_benchmark({"kind": "cpu", **request})
        return FakeWorker.created[-1]

    def test_values_above_the_limits(self):
        worker = self.worker_for(cpu_threads=10**9, cpu_seconds=10**9, cpu_max_prime=10**12)
        self.assertEqual(worker.cpu_threads, os.cpu_count() or 1)
        self.assertEqual(worker.cpu_seconds, MAX_BENCHMARK_SECONDS)
        self.assertEqual(worker.cpu_max_prime, MAX_PRIME_LIMIT)

    def test_values_below_the_limits(self):
        worker = self.worker_for(cpu_threads=-5, cpu_seconds=0, cpu_max_prime=-1)
        self.assertEqual(worker.cpu_threads, 1)
        self.assertEqual(worker.cpu_seconds, 1)
        self.assertEqual(worker.cpu_max_prime, 100)

    def test_values_within_the_limits_stay(self):
        worker = self.worker_for(cpu_seconds=42, cpu_max_prime=5000)
        self.assertEqual(worker.cpu_seconds, 42)
        self.assertEqual(worker.cpu_max_prime, 5000)

    def test_disk_target_must_be_a_physical_mount_point(self):
        for target in ("/tmp", "/etc", "/data/../etc", "/data/sub", "//", ["/"], 1):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.run_benchmark({"kind": "disk", "disk_directory": target})
        self.assertEqual(FakeWorker.created, [])

    def test_allowed_disk_targets(self):
        self.run_benchmark({"kind": "disk", "disk_directory": "/data"})
        self.assertEqual(FakeWorker.created[-1].disk_directory, "/data")
        self.run_benchmark({"kind": "disk"})
        self.assertEqual(FakeWorker.created[-1].disk_directory, "/")

    def test_other_kinds_ignore_a_foreign_disk_target(self):
        self.assertEqual(self.worker_for(disk_directory="/etc").disk_directory, "/")

    def test_no_physical_disk_at_all(self):
        run_benchmark = make_benchmark_runner(lambda: [], FakeWorker)
        run_benchmark({"kind": "disk"})
        self.assertIsNone(FakeWorker.created[-1].disk_directory)


class TestBenchmarkLanguage(unittest.TestCase):
    def setUp(self):
        self.run_benchmark = make_benchmark_runner(lambda: DISKS, FakeWorker)

    def test_clients_without_a_language_keep_getting_german(self):
        self.assertEqual(self.run_benchmark({"kind": "memory"})["label"], "Arbeitsspeicher")
        with self.assertRaisesRegex(ValueError, "Unbekannter Benchmark"):
            self.run_benchmark({"kind": "nonsense"})

    def test_the_requested_language_is_used(self):
        request = {"kind": "memory", "language": "en"}
        self.assertEqual(self.run_benchmark(request)["label"], "Memory")
        with self.assertRaisesRegex(ValueError, "Unknown benchmark"):
            self.run_benchmark({"kind": "nonsense", "language": "en"})

    def test_a_request_leaves_the_server_language_alone(self):
        before = current_language()
        self.run_benchmark({"kind": "memory", "language": "de" if before == "en" else "en"})
        self.assertEqual(current_language(), before)


class TestBenchmarkLock(ServerTestCase):
    def test_second_benchmark_is_refused_while_one_runs(self):
        started, release = threading.Event(), threading.Event()

        class SlowWorker(FakeWorker):
            def _run_cpu(self):
                started.set()
                release.wait(10)
                return "done", ""

        self.benchmark = make_benchmark_runner(lambda: DISKS, SlowWorker)
        results = []
        first = threading.Thread(target=lambda: results.append(self.post({"kind": "cpu"})))
        first.start()
        self.addCleanup(first.join)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(10))

        status, _ = self.post({"kind": "cpu"})
        self.assertEqual(status, 409)

        release.set()
        first.join()
        self.assertEqual(results[0][0], 200)
        self.assertEqual(self.post({"kind": "cpu"})[0], 200)

    def test_failed_benchmark_releases_the_lock(self):
        class BrokenWorker(FakeWorker):
            def _run_cpu(self):
                raise OSError("disk full")

        run_benchmark = make_benchmark_runner(lambda: DISKS, BrokenWorker)
        for _ in range(2):  # the second one would be BenchmarkBusyError if the lock leaked
            with self.assertRaises(OSError):
                run_benchmark({"kind": "cpu"})
        self.assertNotIsInstance(OSError("x"), BenchmarkBusyError)


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 fehlt")
class TestRealWorker(unittest.TestCase):
    def test_fake_matches_the_real_benchmark_worker(self):
        from pages.benchmarks_page import BenchmarkWorker

        # The arguments make_benchmark_runner passes; raises TypeError if they stop fitting.
        inspect.signature(BenchmarkWorker).bind(["cpu"], 1, 1, 100, None)
        for kind in ("cpu", "memory", "gpu", "disk"):
            self.assertTrue(callable(getattr(BenchmarkWorker, f"_run_{kind}")))


class TestArguments(unittest.TestCase):
    def started_with(self, *argv):
        with mock.patch.object(remote_server, "run_server") as run_server:
            remote_server.server_from_args(list(argv))
        return run_server.call_args.args

    def test_token_is_generated_by_default(self):
        _, _, first = self.started_with()
        _, _, second = self.started_with()
        self.assertGreaterEqual(len(first), 32)
        self.assertNotEqual(first, second)

    def test_empty_token_still_generates_one(self):
        _, _, token = self.started_with("--token", "")
        self.assertTrue(token)

    def test_own_token(self):
        self.assertEqual(self.started_with("--token", "abc")[2], "abc")

    def test_no_auth_must_be_explicit(self):
        self.assertIsNone(self.started_with("--no-auth")[2])

    def test_no_auth_with_token_is_an_error(self):
        with mock.patch.object(remote_server, "run_server") as run_server, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            remote_server.server_from_args(["--no-auth", "--token", "abc"])
        self.assertEqual(caught.exception.code, 2)
        run_server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
