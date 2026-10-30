"""The memory tab: rating, hints and the workers behind its actions. No real helper or sudo
is involved; the only process that gets ended is a child started by the test itself.
"""
import glob
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app import memory_info  # noqa: E402
from app.i18n import format_decimal, tr  # noqa: E402
from app.memory_info import (  # noqa: E402
    ERROR_DROP_REFUSED, ERROR_HELPER_FAILED, ERROR_HELPER_OLD, ERROR_HELPER_TIMEOUT,
    ERROR_NOT_SET_UP, ERROR_PAGEOUT_KERNEL, ERROR_PAGEOUT_NO_PYTHON, ERROR_PAGEOUT_REFUSED,
    GIB, MIB, ActivityTracker, DropCachesWorker, EndProgramWorker, EndResult, MemoryHardware,
    MemoryModule, MemoryOverview, MemorySnapshot, PageOutResult, PageOutWorker, ProgramRow,
    SwapDevice, build_hints, format_bytes, is_essential, is_tight, memory_data_sections,
    overview_from_remote, parse_dmi_memory, parse_meminfo, parse_pressure, parse_refused,
    parse_swaps, pressure_level, read_memory_hardware, rows_from_remote,
)
from app.system_info import list_hardware_sensors, list_ram_sensors  # noqa: E402
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


class TestMemoryFiles(unittest.TestCase):
    def test_meminfo_is_in_bytes_and_plain_counters_stay_as_they_are(self):
        values = parse_meminfo("MemTotal:    16 kB\nHugePages_Total:   3\nbroken\nOdd: abc kB\n")
        self.assertEqual(values, {"MemTotal": 16 * 1024, "HugePages_Total": 3})

    def test_swaps_are_in_bytes_and_keep_spaces_in_the_name(self):
        text = ("Filename\tType\tSize\tUsed\tPriority\n"
                "/my\\040swap file 1024 512 -2\n"
                "/dev/sda3 partition 2048 0 -3\n"
                "broken line\n"
                "/dev/sdb1 partition abc 0 -4\n")
        self.assertEqual(parse_swaps(text), [
            SwapDevice("/my swap", "file", 1024 * 1024, 512 * 1024),
            SwapDevice("/dev/sda3", "partition", 2048 * 1024, 0),
        ])

    def test_empty_files_mean_no_data(self):
        self.assertEqual(parse_meminfo(""), {})
        self.assertEqual(parse_swaps(""), [])


DMI_TEXT = """\
# dmidecode 3.6

Handle 0x0010, DMI type 16, 23 bytes
Physical Memory Array
\tLocation: System Board Or Motherboard
\tUse: System Memory
\tError Correction Type: None
\tMaximum Capacity: 128 GB
\tNumber Of Devices: 2

Handle 0x0020, DMI type 16, 23 bytes
Physical Memory Array
\tUse: Flash Memory
\tError Correction Type: Unknown
\tMaximum Capacity: 16 MB

Handle 0x0011, DMI type 17, 92 bytes
Memory Device
\tArray Handle: 0x0010
\tSize: 16 GB
\tForm Factor: DIMM
\tLocator: DIMM_A1
\tBank Locator: BANK 0
\tType: DDR4
\tType Detail: Synchronous Unbuffered
\tSpeed: 3200 MT/s
\tManufacturer: 00000000
\tSerial Number: SECRET123
\tPart Number: Not Specified
\tRank: 2
\tConfigured Memory Speed: 3000 MT/s
\tConfigured Voltage: 1.35 V
\tMemory Operating Mode Capability:
\t\tVolatile memory

Handle 0x0012, DMI type 17, 92 bytes
Memory Device
\tArray Handle: 0x0010
\tSize: No Module Installed
\tLocator: DIMM_A2
\tType: Unknown

Handle 0x0021, DMI type 17, 92 bytes
Memory Device
\tArray Handle: 0x0020
\tSize: 16 MB
\tLocator: FLASH
"""


class TestDmiMemory(unittest.TestCase):
    def test_reads_the_array_and_its_slots(self):
        hardware = parse_dmi_memory(DMI_TEXT)
        self.assertEqual(hardware.max_capacity, 128 * GIB)
        self.assertEqual(hardware.error_correction, "None")
        # The flash chip has an array of its own and is no memory slot.
        self.assertEqual(hardware.slots_total, 2)
        self.assertEqual([module.locator for module in hardware.modules], ["DIMM_A1"])

    def test_reads_a_module_and_drops_placeholders(self):
        module = parse_dmi_memory(DMI_TEXT).modules[0]
        self.assertEqual(module.size, 16 * GIB)
        self.assertEqual((module.type, module.form_factor), ("DDR4", "DIMM"))
        self.assertEqual((module.speed, module.configured_speed), ("3200 MT/s", "3000 MT/s"))
        self.assertEqual((module.rank, module.voltage), ("2", "1.35 V"))
        self.assertEqual((module.manufacturer, module.part_number), ("", ""))

    def test_the_serial_number_is_not_kept(self):
        self.assertNotIn("SECRET123", repr(parse_dmi_memory(DMI_TEXT)))

    def test_boards_without_array_data_still_have_memory(self):
        text = "Handle 0x1, DMI type 17, 92 bytes\nMemory Device\n\tSize: 8192 MB\n\tLocator: A\n"
        hardware = parse_dmi_memory(text)
        self.assertEqual((hardware.max_capacity, hardware.error_correction), (None, ""))
        self.assertEqual([module.size for module in hardware.modules], [8 * GIB])

    def test_no_readable_data_means_no_hardware(self):
        for text in (None, "", "# dmidecode 3.6\n"):
            with self.subTest(text=text), mock.patch.object(memory_info, "read_dmi_memory",
                                                            return_value=text):
                self.assertIsNone(read_memory_hardware())
        with mock.patch.object(memory_info, "read_dmi_memory", return_value=DMI_TEXT):
            self.assertEqual(read_memory_hardware().slots_total, 2)


class TestRamSensors(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        chips = {"hwmon0": ("jc42", {"temp1_input": "41500"}),
                 "hwmon1": ("SPD5118", {"temp1_input": "39000"}),
                 "hwmon2": ("nct6687", {"temp1_input": "50000", "temp1_label": "CPU"})}
        for folder, (chip, files) in chips.items():
            os.mkdir(os.path.join(directory.name, folder))
            for name, content in {"name": chip, **files}.items():
                with open(os.path.join(directory.name, folder, name), "w") as handle:
                    handle.write(content)
        real_glob = glob.glob
        patcher = mock.patch("glob.glob", side_effect=lambda pattern: real_glob(
            pattern.replace("/sys/class/hwmon", directory.name)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_memory_tab_gets_the_sensors_on_the_modules(self):
        sensors = list_ram_sensors()
        self.assertEqual([sensor["name"] for sensor in sensors],
                         ["jc42: temp1", "SPD5118: temp1"])
        self.assertEqual((sensors[0]["value"], sensors[0]["unit"]), (41.5, "°C"))

    def test_the_sensors_tab_no_longer_gets_them(self):
        names = [sensor["name"] for sensor in list_hardware_sensors()]
        self.assertIn("nct6687: CPU", names)
        self.assertFalse([name for name in names if name.lower().startswith(("jc42", "spd"))])


class TestMemoryData(unittest.TestCase):
    MEMINFO = {"MemFree": GIB, "Buffers": MIB, "Slab": 2 * MIB, "SwapCached": 3 * MIB,
               "HugePages_Total": 4, "HugePages_Free": 1}

    @staticmethod
    def sections(data=None, meminfo=None, swaps=(), hardware=None, sensors=None, remote=False):
        return dict(memory_data_sections(data or overview(), meminfo or {}, list(swaps), hardware,
                                         sensors, remote=remote))

    def test_usage_lists_the_overview_and_the_kernel_counters(self):
        sections = self.sections(meminfo=self.MEMINFO)
        usage = dict(sections[tr("Usage")])
        self.assertEqual(usage[tr("Total")], format_bytes(16 * GIB))
        self.assertEqual(usage[tr("Free")], format_bytes(GIB))
        kernel = dict(sections[tr("Kernel")])
        self.assertEqual(kernel[tr("Kernel caches (slab)")], format_bytes(2 * MIB))
        self.assertEqual(kernel[tr("Huge pages")], tr("{used} of {total}", used=3, total=4))

    def test_without_kernel_counters_the_section_is_left_out(self):
        self.assertNotIn(tr("Kernel"), self.sections())

    def test_pressure_is_only_listed_if_known(self):
        label = tr("Memory pressure (1 min)")
        self.assertNotIn(label, dict(self.sections()[tr("Usage")]))
        usage = dict(self.sections(data=overview(some=1.5))[tr("Usage")])
        self.assertEqual(usage[label], f"{format_decimal(1.5)} %")

    def test_swap_states(self):
        for total, text in ((None, tr("Unknown")), (0, tr("Not set up"))):
            with self.subTest(total=total):
                swap = self.sections(data=overview(swap_total=total))[tr("Swap")]
                self.assertEqual(swap, [(tr("Total"), text)])
        device = SwapDevice("/swapfile", "file", 4 * GIB, GIB)
        swap = dict(self.sections(meminfo=self.MEMINFO, swaps=[device])[tr("Swap")])
        self.assertEqual(swap[tr("Total")], format_bytes(4 * GIB))
        self.assertEqual(swap[tr("Also still in memory")], format_bytes(3 * MIB))
        self.assertEqual(swap["/swapfile"], tr("{used} of {total}", used=format_bytes(GIB),
                                               total=format_bytes(4 * GIB)))

    def test_hardware_lists_every_module(self):
        hardware = MemoryHardware(
            max_capacity=64 * GIB, error_correction="None", slots_total=4,
            modules=(MemoryModule("DIMM_A1", 16 * GIB, type="DDR4", form_factor="DIMM"),
                     MemoryModule("", 8 * GIB)))
        sections = self.sections(hardware=hardware)
        rows = dict(sections[tr("Hardware")])
        self.assertEqual(rows[tr("Slots in use")], tr("{used} of {total}", used=2, total=4))
        self.assertEqual(rows[tr("Error correction")], tr("None"))
        first = dict(sections[tr("Module {number}: {slot}", number=1, slot="DIMM_A1")])
        self.assertEqual(first[tr("Type")], "DDR4 DIMM")
        # Fields that are not known get no empty row.
        self.assertNotIn(tr("Manufacturer"), first)
        self.assertEqual(sections[tr("Module {number}", number=2)],
                         [(tr("Size"), format_bytes(8 * GIB))])

    def test_without_hardware_the_reason_is_shown(self):
        local = dict(self.sections()[tr("Hardware")])
        self.assertEqual(local[tr("Modules")],
                         tr("Needs \"Set up hardware data with sudo\" under Settings"))
        remote = dict(self.sections(remote=True)[tr("Hardware")])
        self.assertEqual(remote[tr("Modules")], tr("Only shown for this PC"))

    def test_temperature_states(self):
        self.assertEqual(self.sections(sensors=None)[tr("Temperature")],
                         [(tr("Temperature"), tr("Unknown"))])
        self.assertEqual(self.sections(sensors=[])[tr("Temperature")],
                         [(tr("Temperature"), tr("No sensor found"))])
        sensors = [{"unit": "°C", "value": 41.5}, {"unit": "V", "value": 1.2},
                   {"unit": "°C", "value": 40.0}]
        self.assertEqual(self.sections(sensors=sensors)[tr("Temperature")],
                         [(tr("Sensor {number}", number=1), f"{format_decimal(41.5)} °C"),
                          (tr("Sensor {number}", number=2), f"{format_decimal(40.0)} °C")])


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

    def data_rows(self):
        """The rows of the data table as {label: value}; section titles have the value None."""
        table = self.page.data_table
        rows = {}
        for index in range(table.rowCount()):
            value = table.item(index, 1)
            rows[table.item(index, 0).text()] = value.text() if value else None
        return rows

    def open_data_tab(self):
        with mock.patch.object(memory_page, "read_meminfo", return_value={"Buffers": GIB}), \
                mock.patch.object(memory_page, "read_swaps", return_value=[]):
            self.page.tabs.setCurrentWidget(self.page.data_table)

    def test_both_graphs_show_the_current_share(self):
        self.page._add_to_charts(MemoryOverview(
            total=16 * GIB, used=4 * GIB, available=12 * GIB, cached=0,
            swap_total=4 * GIB, swap_used=GIB))
        self.assertEqual(self.page._memory_history[-1], 25.0)
        self.assertEqual(self.page._swap_history[-1], 25.0)
        self.assertEqual(self.page.memory_chart_label.text(), tr(
            "Memory: {used} of {total} ({percent} %)", used=format_bytes(4 * GIB),
            total=format_bytes(16 * GIB), percent=25))
        self.assertEqual(self.page.swap_chart_label.text(), tr(
            "Swap: {used} of {total} ({percent} %)", used=format_bytes(GIB),
            total=format_bytes(4 * GIB), percent=25))

    def test_the_swap_graph_says_why_it_is_empty(self):
        for total, text in ((0, tr("Not set up")), (None, tr("Unknown"))):
            with self.subTest(total=total), \
                    mock.patch.object(self.page.swap_chart, "set_overlay_text") as overlay:
                self.page._add_to_charts(overview(swap_total=total))
                overlay.assert_called_once_with(text)
                self.assertEqual(self.page.swap_chart_label.text(), tr("Swap"))
                self.assertEqual(self.page._swap_history[-1], 0.0)

    def test_no_total_does_not_divide_by_zero(self):
        self.page._add_to_charts(overview(total=0, available=0))
        self.assertEqual(self.page._memory_history[-1], 0.0)

    def test_the_data_tab_is_filled_when_it_is_opened(self):
        self.show(program())
        self.assertEqual(self.page.data_table.rowCount(), 0)
        self.open_data_tab()
        rows = self.data_rows()
        self.assertIsNone(rows[tr("Usage")])
        self.assertIsNone(rows[tr("Hardware")])
        self.assertEqual(rows[tr("Buffers")], format_bytes(GIB))
        self.assertEqual(rows[tr("Temperature")], tr("No sensor found"))

    def test_the_data_tab_shows_sensors_and_modules_when_they_arrive(self):
        self.page._on_snapshot(MemorySnapshot(overview(), [], [],
                                              [{"unit": "°C", "value": 41.5}]))
        self.open_data_tab()
        self.assertEqual(self.data_rows()[tr("Sensor {number}", number=1)],
                         f"{format_decimal(41.5)} °C")
        with mock.patch.object(memory_page, "read_meminfo", return_value={}), \
                mock.patch.object(memory_page, "read_swaps", return_value=[]):
            self.page._on_hardware(MemoryHardware(slots_total=2, modules=(
                MemoryModule("DIMM_A1", 16 * GIB),)))
        rows = self.data_rows()
        self.assertEqual(rows[tr("Slots in use")], tr("{used} of {total}", used=1, total=2))
        self.assertIn(tr("Module {number}: {slot}", number=1, slot="DIMM_A1"), rows)

    def test_a_new_value_keeps_the_rows_and_with_them_the_scroll_position(self):
        self.show(program())
        self.open_data_tab()
        table = self.page.data_table
        item = table.item(1, 1)
        self.show(program(), data=overview(available=4 * GIB))
        self.open_data_tab()
        self.page._update_data_table()
        self.assertIs(table.item(1, 1), item)

    def test_the_remote_data_tab_only_shows_what_the_server_sent(self):
        self.page.tabs.setCurrentWidget(self.page.data_table)
        payload = {"system": {"mem_total_gb": 16, "mem_available_gb": 8}, "processes": [],
                   "ram_sensors": [{"unit": "°C", "value": 30.0}]}
        self.page.set_remote_data(payload)
        rows = self.data_rows()
        self.assertEqual(rows[tr("Modules")], tr("Only shown for this PC"))
        self.assertEqual(rows[tr("Sensor {number}", number=1)], f"{format_decimal(30.0)} °C")

        # A server from before this field knows nothing about its sensors, which is not "none".
        del payload["ram_sensors"]
        self.page.set_remote_data(payload)
        self.assertEqual(self.data_rows()[tr("Temperature")], tr("Unknown"))

    def test_switching_to_a_server_starts_the_graphs_anew(self):
        self.page._add_to_charts(overview())
        self.page.set_remote_data({"system": {"mem_total_gb": 16, "mem_available_gb": 12},
                                   "processes": []})
        self.assertEqual(list(self.page._memory_history)[:-1], [0.0] * 59)
        self.assertEqual(self.page._memory_history[-1], 25.0)
        # The timer reads this PC, so it must not draw into the server's graph.
        self.page._on_chart_timer()
        self.assertEqual(list(self.page._memory_history)[-2:], [0.0, 25.0])


if __name__ == "__main__":
    unittest.main()
