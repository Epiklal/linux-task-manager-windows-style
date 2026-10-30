"""The memory tab: rating, hints and the workers behind its actions. No real helper or sudo
is involved; the only process that gets ended is a child started by the test itself.
"""
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app import memory_info  # noqa: E402
from app.i18n import tr  # noqa: E402
from app.memory_info import (  # noqa: E402
    ERROR_DROP_REFUSED, ERROR_HELPER_FAILED, ERROR_HELPER_OLD, ERROR_HELPER_TIMEOUT,
    ERROR_NOT_SET_UP, ERROR_PAGEOUT_KERNEL, ERROR_PAGEOUT_NO_PYTHON, ERROR_PAGEOUT_REFUSED,
    GIB, MIB, ActivityTracker, DropCachesWorker, EndProgramWorker, EndResult, MemoryOverview,
    MemorySnapshot, PageOutResult, PageOutWorker, ProgramRow, build_hints, is_essential, is_tight,
    overview_from_remote, parse_pressure, parse_refused, pressure_level, rows_from_remote,
)
from pages import memory_page  # noqa: E402
from pages.memory_page import MemoryPage  # noqa: E402


def overview(total=16 * GIB, available=8 * GIB, some=None, swap_total=4 * GIB):
    return MemoryOverview(total=total, used=total - available, available=available,
                          cached=2 * GIB, swap_total=swap_total, swap_used=0, pressure_some=some)


def program(name="app", memory=2 * GIB, idle=0.0, own=True, essential=False, username="me"):
    return ProgramRow(key=(name, username), name=name, username=username, memory_bytes=memory,
                      swap_bytes=0, exact=True, processes=[(1234, 1.0)], essential=essential,
                      own=own, idle_seconds=idle)


class TestPressure(unittest.TestCase):
    TEXT = ("some avg10=0.50 avg60=1.25 avg300=0.00 total=99\n"
            "full avg10=0.10 avg60=0.75 avg300=0.00 total=12\n")

    def test_reads_the_one_minute_average_of_both_lines(self):
        self.assertEqual(parse_pressure(self.TEXT), (1.25, 0.75))

    def test_broken_or_empty_input_is_unknown(self):
        self.assertEqual(parse_pressure(""), (None, None))
        self.assertEqual(parse_pressure("\nsome avg60=abc\n"), (None, None))

    def test_a_missing_full_line_stays_unknown(self):
        self.assertEqual(parse_pressure("some avg60=2.00\n"), (2.0, None))

    def test_levels(self):
        for some, level in ((None, None), (0.0, "none"), (0.99, "none"), (1.0, "light"),
                            (9.99, "light"), (10.0, "high"), (80.0, "high")):
            with self.subTest(some=some):
                self.assertEqual(pressure_level(overview(some=some)), level)


class TestIsTight(unittest.TestCase):
    def test_much_cache_and_free_memory_is_not_tight(self):
        self.assertFalse(is_tight(overview(available=8 * GIB, some=0.0)))

    def test_little_available_memory_is_tight(self):
        self.assertTrue(is_tight(overview(available=GIB, some=0.0)))

    def test_high_pressure_is_tight_even_with_memory_left(self):
        self.assertTrue(is_tight(overview(available=8 * GIB, some=25.0)))

    def test_light_pressure_alone_is_not_tight(self):
        self.assertFalse(is_tight(overview(available=8 * GIB, some=5.0)))

    def test_unknown_total_is_not_tight(self):
        self.assertFalse(is_tight(overview(total=0, available=0)))


class TestActivityTracker(unittest.TestCase):
    KEY = ("app", "me")

    def test_a_new_program_counts_as_just_active(self):
        self.assertEqual(ActivityTracker().update({self.KEY: 50.0}, 100.0)[self.KEY], 0.0)

    def test_idle_time_grows_without_cpu_use(self):
        tracker = ActivityTracker()
        tracker.update({self.KEY: 50.0}, 100.0)
        self.assertEqual(tracker.update({self.KEY: 50.0}, 130.0)[self.KEY], 30.0)
        self.assertEqual(tracker.update({self.KEY: 50.0}, 160.0)[self.KEY], 60.0)

    def test_background_ticks_do_not_count_as_activity(self):
        tracker = ActivityTracker()
        tracker.update({self.KEY: 50.0}, 100.0)
        # 0.1 s of CPU in 30 s is below the 1 % that counts as active.
        self.assertEqual(tracker.update({self.KEY: 50.1}, 130.0)[self.KEY], 30.0)

    def test_real_cpu_use_resets_the_idle_time(self):
        tracker = ActivityTracker()
        tracker.update({self.KEY: 50.0}, 100.0)
        tracker.update({self.KEY: 50.0}, 130.0)
        self.assertEqual(tracker.update({self.KEY: 55.0}, 160.0)[self.KEY], 0.0)

    def test_a_shrinking_sum_is_not_activity(self):
        tracker = ActivityTracker()
        tracker.update({self.KEY: 50.0}, 100.0)
        self.assertEqual(tracker.update({self.KEY: 10.0}, 130.0)[self.KEY], 30.0)

    def test_a_program_that_comes_back_starts_over(self):
        tracker = ActivityTracker()
        tracker.update({self.KEY: 50.0}, 100.0)
        tracker.update({}, 130.0)
        self.assertEqual(tracker.update({self.KEY: 50.0}, 160.0)[self.KEY], 0.0)

    def test_two_updates_at_the_same_moment_do_not_divide_by_zero(self):
        tracker = ActivityTracker()
        tracker.update({self.KEY: 50.0}, 100.0)
        self.assertEqual(tracker.update({self.KEY: 51.0}, 100.0)[self.KEY], 0.0)


class TestBuildHints(unittest.TestCase):
    def test_small_programs_are_not_mentioned(self):
        self.assertEqual(build_hints([program(memory=100 * MIB)], overview()), [])

    def test_large_program(self):
        hints = build_hints([program(memory=2 * GIB)], overview())
        self.assertEqual([(hint.kind, hint.name) for hint in hints], [("large", "app")])

    def test_large_threshold_follows_the_size_of_the_machine(self):
        self.assertEqual(build_hints([program(memory=2 * GIB)], overview(total=64 * GIB)), [])
        self.assertEqual(len(build_hints([program(memory=2 * GIB)], overview(total=4 * GIB))), 1)

    def test_idle_program_is_mentioned_below_the_large_threshold(self):
        hints = build_hints([program(memory=400 * MIB, idle=700.0)], overview())
        self.assertEqual([(hint.kind, hint.idle_seconds) for hint in hints], [("idle", 700.0)])

    def test_recently_used_program_is_not_idle(self):
        self.assertEqual(build_hints([program(memory=400 * MIB, idle=599.0)], overview()), [])

    def test_a_large_idle_program_is_reported_as_idle(self):
        self.assertEqual(build_hints([program(idle=700.0)], overview())[0].kind, "idle")

    def test_essential_and_foreign_programs_are_skipped(self):
        rows = [program("gnome-shell", essential=True), program("mysqld", own=False)]
        self.assertEqual(build_hints(rows, overview()), [])

    def test_at_most_three_hints_largest_first(self):
        rows = [program(f"app{size}", memory=size * GIB) for size in (2, 5, 3, 4)]
        self.assertEqual([hint.name for hint in build_hints(rows, overview())],
                         ["app5", "app4", "app3"])


class TestIsEssential(unittest.TestCase):
    def test_desktop_programs(self):
        for name in ("systemd", "gnome-shell", "Xorg", "plasmashell", "gnome-session-binary"):
            with self.subTest(name=name):
                self.assertTrue(is_essential(name))

    def test_cinnamon_session_is_found_under_its_cut_off_name(self):
        self.assertTrue(is_essential("xfce4-session"))
        self.assertTrue(is_essential("cinnamon-session"[:15]))

    def test_ordinary_programs(self):
        for name in ("firefox", "code", "systemd-resolved-but-longer", "python3"):
            with self.subTest(name=name):
                self.assertFalse(is_essential(name))


class TestProgramName(unittest.TestCase):
    def name_of(self, name, exe, cmdline):
        process = mock.Mock()
        process.name.return_value = name
        process.exe.return_value = exe
        process.cmdline.return_value = cmdline
        return memory_info._program_name(process)

    def test_renamed_main_thread_counts_as_its_program(self):
        firefox = "/usr/lib/firefox-esr/firefox-esr"
        self.assertEqual(self.name_of("Isolated Web Co", firefox, [firefox, "-contentproc"]),
                         "firefox-esr")
        self.assertEqual(self.name_of("MainThread", "/opt/node/bin/node", ["node", "index.js"]),
                         "node")

    def test_other_start_names_and_titles_are_kept(self):
        self.assertEqual(self.name_of("pipewire-pulse", "/usr/bin/pipewire", ["pipewire-pulse"]),
                         "pipewire-pulse")
        self.assertEqual(self.name_of("nginx", "/usr/sbin/nginx", ["nginx: worker process"]),
                         "nginx")
        self.assertEqual(self.name_of("python3", "/usr/bin/python3.13", ["python3", "x.py"]),
                         "python3")

    def test_without_details_the_plain_name_stays(self):
        self.assertEqual(self.name_of("kthreadd", "", []), "kthreadd")
        process = mock.Mock()
        process.name.return_value = "sshd"
        process.exe.side_effect = psutil.AccessDenied(1)
        self.assertEqual(memory_info._program_name(process), "sshd")

    def test_desktop_programs_keep_their_name(self):
        self.assertEqual(self.name_of("plasmashell", "/usr/bin/other", ["/usr/bin/other"]),
                         "plasmashell")


class TestRemoteData(unittest.TestCase):
    def test_overview(self):
        result = overview_from_remote(
            {"mem_total_gb": 16, "mem_available_gb": 4, "mem_cached_gb": 2})
        self.assertEqual((result.total, result.used, result.available, result.cached),
                         (16 * GIB, 12 * GIB, 4 * GIB, 2 * GIB))
        self.assertIsNone(result.swap_total)
        self.assertIsNone(pressure_level(result))

    def test_missing_fields_do_not_raise(self):
        self.assertEqual(overview_from_remote({}).total, 0)

    def test_rows_are_summed_per_program_and_marked_inexact(self):
        rows = rows_from_remote([
            {"name": "a", "username": "u", "pid": 1, "memory_mb": 100},
            {"name": "b", "username": "u", "pid": 2, "memory_mb": 500},
            {"name": "a", "username": "u", "pid": 3, "memory_mb": 50},
            {"name": "kthread", "username": "root", "pid": 4, "memory_mb": 0},
        ])
        self.assertEqual([(row.name, row.memory_bytes) for row in rows],
                         [("b", 500 * MIB), ("a", 150 * MIB)])
        self.assertFalse(rows[0].exact)
        self.assertIsNone(rows[0].swap_bytes)
        self.assertEqual(len(rows[1].processes), 2)


class TestHelperResults(unittest.TestCase):
    def test_parse_refused(self):
        self.assertEqual(parse_refused("paged=2 refused=3\n"), 3)
        self.assertEqual(parse_refused(""), 0)
        self.assertEqual(parse_refused("refused=x"), 0)

    def run_page_out(self, returncode=0, stdout="", raises=None, helper="/usr/bin/helper"):
        worker = PageOutWorker("app", [(1, 1.0)])
        run = mock.Mock(return_value=subprocess.CompletedProcess([], returncode, stdout, ""),
                        side_effect=raises)
        with mock.patch.object(memory_info, "helper_path", return_value=helper), \
                mock.patch.object(memory_info, "_live_processes", return_value=[psutil.Process()]), \
                mock.patch.object(memory_info, "_SETTLE_SECONDS", 0), \
                mock.patch.object(worker, "_run_command", run):
            return worker._page_out(), run

    def test_page_out_errors(self):
        for returncode, error in ((1, ERROR_NOT_SET_UP), (2, ERROR_HELPER_OLD),
                                  (3, ERROR_PAGEOUT_REFUSED), (4, ERROR_PAGEOUT_KERNEL),
                                  (5, ERROR_HELPER_FAILED), (127, ERROR_PAGEOUT_NO_PYTHON)):
            with self.subTest(returncode=returncode):
                self.assertEqual(self.run_page_out(returncode)[0].error, error)

    def test_page_out_without_sudo_or_helper(self):
        self.assertEqual(self.run_page_out(raises=OSError())[0].error, ERROR_NOT_SET_UP)
        self.assertEqual(self.run_page_out(helper=None)[0].error, ERROR_NOT_SET_UP)

    def test_page_out_timeout(self):
        result, _ = self.run_page_out(raises=subprocess.TimeoutExpired("sudo", 1))
        self.assertEqual(result.error, ERROR_HELPER_TIMEOUT)

    def test_page_out_success_reports_refused_processes(self):
        result, run = self.run_page_out(0, "paged=1 refused=2\n")
        self.assertEqual((result.error, result.refused), ("", 2))
        self.assertEqual(run.call_args.args[0],
                         ["sudo", "-n", "/usr/bin/helper", "pageout", str(os.getpid())])

    def test_page_out_counts_only_what_reached_swap(self):
        for swapped, moved in (((10 * MIB, 60 * MIB), 50 * MIB), ((60 * MIB, 10 * MIB), 0)):
            with self.subTest(swapped=swapped), \
                    mock.patch.object(memory_info, "_swapped_bytes", side_effect=swapped):
                self.assertEqual(self.run_page_out(0, "paged=1 refused=0\n")[0].moved_bytes, moved)

    def test_swapped_bytes_of_a_process_that_is_gone_count_as_nothing(self):
        gone = mock.Mock(pid=2 ** 30)
        with mock.patch("builtins.open", mock.mock_open(read_data="Name:\tapp\nVmSwap:\t  2 kB\n")):
            self.assertEqual(memory_info._swapped_bytes([gone, gone]), 4096)
        self.assertEqual(memory_info._swapped_bytes([gone]), 0)

    def test_page_out_of_a_program_that_is_gone_does_nothing(self):
        worker = PageOutWorker("app", [(1, 1.0)])
        run = mock.Mock()
        with mock.patch.object(memory_info, "helper_path", return_value="/usr/bin/helper"), \
                mock.patch.object(memory_info, "_live_processes", return_value=[]), \
                mock.patch.object(worker, "_run_command", run):
            self.assertEqual(worker._page_out(), PageOutResult("app"))
        run.assert_not_called()

    def run_drop(self, returncode=0, raises=None, helper="/usr/bin/helper"):
        worker = DropCachesWorker()
        run = mock.Mock(return_value=subprocess.CompletedProcess([], returncode, "", ""),
                        side_effect=raises)
        with mock.patch.object(memory_info, "helper_path", return_value=helper), \
                mock.patch.object(worker, "_run_command", run):
            return worker._drop(), run

    def test_drop_errors(self):
        for returncode, error in ((1, ERROR_NOT_SET_UP), (2, ERROR_HELPER_OLD),
                                  (3, ERROR_DROP_REFUSED), (5, ERROR_HELPER_FAILED)):
            with self.subTest(returncode=returncode):
                self.assertEqual(self.run_drop(returncode)[0].error, error)
        self.assertEqual(self.run_drop(raises=OSError())[0].error, ERROR_NOT_SET_UP)
        self.assertEqual(self.run_drop(helper=None)[0].error, ERROR_NOT_SET_UP)

    def test_drop_success_calls_only_the_fixed_command(self):
        result, run = self.run_drop()
        self.assertEqual(result.error, "")
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "/usr/bin/helper", "drop-caches"])


class TestEndProgramWorker(unittest.TestCase):
    STUBBORN = ("import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)")

    def start_child(self, code="print('ready', flush=True); import time; time.sleep(60)"):
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        child.stdout.readline()  # the signal handler is in place from here on
        return child, [(child.pid, psutil.Process(child.pid).create_time())]

    @staticmethod
    def run_worker(*args, **kwargs) -> EndResult:
        worker = EndProgramWorker("child", *args, **kwargs)
        results = []
        worker.resultReady.connect(results.append)
        worker.run()
        return results[0]

    def test_ends_a_program_that_cooperates(self):
        child, processes = self.start_child()
        result = self.run_worker(processes)
        self.assertEqual((result.ended, result.denied, result.survivors), (1, 0, ()))
        self.assertEqual(child.wait(timeout=5), -15)

    def test_a_program_that_ignores_the_request_is_reported_not_killed(self):
        child, processes = self.start_child(self.STUBBORN)
        with mock.patch.object(memory_info, "_END_WAIT_SECONDS", 0.3):
            result = self.run_worker(processes)
        self.assertEqual(result.survivors, tuple(processes))
        self.assertEqual(result.ended, 0)
        self.assertIsNone(child.poll())

    def test_force_ends_a_program_that_ignores_the_request(self):
        child, processes = self.start_child(self.STUBBORN)
        result = self.run_worker(processes, force=True)
        self.assertEqual((result.ended, result.survivors), (1, ()))
        self.assertEqual(child.wait(timeout=5), -9)

    def test_a_reused_pid_is_left_alone(self):
        child, processes = self.start_child()
        pid, created = processes[0]
        result = self.run_worker([(pid, created - 100.0)])
        self.assertEqual((result.ended, result.survivors), (0, ()))
        self.assertIsNone(child.poll())

    def test_a_process_that_is_gone_is_no_error(self):
        child, processes = self.start_child()
        child.kill()
        child.wait()
        result = self.run_worker(processes)
        self.assertEqual((result.ended, result.denied, result.survivors), (0, 0, ()))


class TestMemoryPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = SimpleNamespace(memory_cache_option=False)
        self.page = MemoryPage(self.settings)
        self.addCleanup(self.page.deleteLater)
        self.addCleanup(self.page.shutdown)

    def show(self, *rows, data=None):
        rows = list(rows)
        self.page._show_snapshot(MemorySnapshot(data or overview(), rows, []))

    def select(self, name):
        for index, row in enumerate(self.page._rows):
            if row.name == name:
                self.page.table.selectRow(index)

    def buttons(self):
        return self.page.end_btn.isEnabled(), self.page.swap_btn.isEnabled()

    def test_nothing_selected_locks_the_actions(self):
        self.show(program())
        self.assertEqual(self.buttons(), (False, False))

    def test_own_program_can_be_ended_and_moved(self):
        self.show(program())
        self.select("app")
        self.assertEqual(self.buttons(), (True, True))

    def test_essential_foreign_and_own_programs_are_locked(self):
        self.show(program("gnome-shell", essential=True),
                  program("mysqld", own=False, username="mysql"),
                  program(memory_info.OWN_PROGRAM_NAME, essential=True))
        for name in ("gnome-shell", "mysqld", memory_info.OWN_PROGRAM_NAME):
            with self.subTest(name=name):
                self.select(name)
                self.assertEqual(self.buttons(), (False, False))
                self.assertTrue(self.page.action_hint_label.text())

    def test_without_swap_only_ending_is_possible(self):
        self.show(program(), data=overview(swap_total=0))
        self.select("app")
        self.assertEqual(self.buttons(), (True, False))

    def test_selection_survives_a_refresh_and_ends_with_the_program(self):
        self.show(program("a"), program("b"))
        self.select("b")
        self.show(program("b"), program("a"))
        self.assertEqual(self.page._selected_row().name, "b")
        self.show(program("a"))
        self.assertIsNone(self.page._selected_row())
        self.assertEqual(self.buttons(), (False, False))

    def test_answering_no_starts_nothing(self):
        self.show(program())
        self.select("app")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No), \
                mock.patch.object(self.page, "_start_action") as start:
            self.page._on_end_clicked()
            self.page._on_swap_clicked()
        start.assert_not_called()

    def test_ending_asks_first_and_never_starts_with_force(self):
        self.show(program())
        self.select("app")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as ask, \
                mock.patch.object(memory_page, "EndProgramWorker") as worker, \
                mock.patch.object(self.page, "_start_action"):
            self.page._on_end_clicked()
        ask.assert_called_once()
        self.assertFalse(worker.call_args.kwargs.get("force", False))

    def test_a_survivor_is_only_forced_after_asking(self):
        result = EndResult("app", survivors=((5, 1.0),))
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No), \
                mock.patch.object(memory_page, "EndProgramWorker") as worker:
            self.page._on_end_result(result)
        worker.assert_not_called()
        self.assertEqual(self.page.status_label.text(), tr("{name} is still running.", name="app"))

        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                mock.patch.object(memory_page, "EndProgramWorker") as worker, \
                mock.patch.object(self.page, "_start_action"):
            self.page._on_end_result(result)
        self.assertTrue(worker.call_args.kwargs["force"])

    def test_swap_results(self):
        self.page._on_swap_result(PageOutResult("app", error=ERROR_NOT_SET_UP))
        self.assertEqual(self.page.status_label.text(), tr(ERROR_NOT_SET_UP))
        self.page._on_swap_result(PageOutResult("app", moved_bytes=MIB))
        self.assertIn(tr("{name}: hardly anything could be moved. It is in use, shared with other "
                         "programs or already in swap.",
                         name="app"), self.page.status_label.text())
        self.page._on_swap_result(PageOutResult("app", moved_bytes=GIB, refused=2))
        self.assertIn("1,0 GB" if tr("In use") != "In use" else "1.0 GB",
                      self.page.status_label.text())

    def test_cache_button_is_off_by_default_and_warns_when_switched_on(self):
        self.assertTrue(self.page.drop_btn.isHidden())
        self.assertTrue(self.page.cache_warning.isHidden())
        self.page.cache_check.setChecked(True)
        self.assertFalse(self.page.drop_btn.isHidden())
        self.assertFalse(self.page.cache_warning.isHidden())
        self.assertTrue(self.settings.memory_cache_option)

    def test_emptying_the_cache_asks_first(self):
        with mock.patch.object(QMessageBox, "warning", return_value=QMessageBox.No) as ask, \
                mock.patch.object(self.page, "_start_action") as start:
            self.page._on_drop_clicked()
        ask.assert_called_once()
        start.assert_not_called()

    def test_markup_in_a_program_name_stays_text(self):
        self.page._show_status(tr("{name} was ended.", name="<img src=x>"))
        self.assertEqual(self.page.status_label.textFormat(), memory_page.Qt.PlainText)
        self.assertEqual(self.page.hints_label.textFormat(), memory_page.Qt.PlainText)

    def test_remote_mode_shows_server_data_and_locks_everything(self):
        payload = {"system": {"mem_total_gb": 16, "mem_available_gb": 8},
                   "processes": [{"name": "db", "username": "u", "pid": 1, "memory_mb": 4096}]}
        self.page.set_remote_data(payload)
        self.assertEqual([row.name for row in self.page._rows], ["db"])
        self.assertTrue(self.page.cache_box.isHidden())
        self.select("db")
        self.assertEqual(self.buttons(), (False, False))
        self.assertEqual(self.page.swap_value.text(), tr("Unknown"))

        # Local results that are still on their way must not replace the server's list.
        self.page._on_snapshot(MemorySnapshot(overview(), [program("local")], []))
        self.assertEqual([row.name for row in self.page._rows], ["db"])

        self.page.set_remote_data(None)
        self.assertFalse(self.page.cache_box.isHidden())
        self.assertEqual(self.buttons(), (True, True))


if __name__ == "__main__":
    unittest.main()
