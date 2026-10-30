"""Memory tab data: what uses RAM, what could be freed, and the actions behind it.

Nothing in here ends a program or empties a cache on its own. The hints are suggestions,
every action starts from an explicit click on the memory page.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

import psutil
from PySide6.QtCore import Signal

from app.hardware_setup import helper_path
from app.i18n import N_, format_decimal, tr
from app.system_info import StoppableWorker, WorkerStopped, list_ram_sensors, read_dmi_memory

GIB = 1024 ** 3
MIB = 1024 ** 2

MEMINFO_FILE = "/proc/meminfo"
SWAPS_FILE = "/proc/swaps"
PRESSURE_FILE = "/proc/pressure/memory"
PRESSURE_LIGHT = 1.0
PRESSURE_HIGH = 10.0
TIGHT_AVAILABLE_SHARE = 0.10

# A program counts as active above 1 % of one core, background ticks stay below that.
ACTIVE_CPU_SHARE = 0.01

LARGE_MIN_BYTES = GIB
LARGE_MIN_SHARE = 0.10
IDLE_MIN_BYTES = 300 * MIB
IDLE_MIN_SHARE = 0.01
IDLE_MIN_SECONDS = 600
MAX_HINTS = 3

VISIBLE_INTERVAL = 5.0
HIDDEN_INTERVAL = 30.0

# Process names are cut to 15 characters by the kernel, so the comparison is too.
_ESSENTIAL_NAMES = frozenset(name[:15] for name in (
    "systemd", "dbus-daemon", "dbus-broker", "Xorg", "Xwayland", "gnome-shell",
    "plasmashell", "kwin_x11", "kwin_wayland", "ksmserver", "cinnamon", "cinnamon-session",
    "muffin", "mutter", "xfwm4", "xfce4-session", "xfce4-panel", "mate-session", "marco",
    "sddm", "gdm", "lightdm", "pipewire", "wireplumber", "pulseaudio", "sway", "Hyprland",
    "lxqt-session", "budgie-wm",
))
_ESSENTIAL_PREFIXES = ("gnome-session",)
OWN_PROGRAM_NAME = "Lintask"

# Protection against a process with the same pid that started after the list was read.
_CREATE_TIME_TOLERANCE = 0.01
_END_WAIT_SECONDS = 5.0
_END_POLL_SECONDS = 0.25
_HELPER_TIMEOUT = 120
_SETTLE_SECONDS = 0.5

ERROR_NOT_SET_UP = N_(
    "Administrator rights are not set up yet. Open Settings and click "
    "\"Set up hardware data with sudo\"."
)
ERROR_HELPER_OLD = N_(
    "The helper is out of date. Open Settings and click "
    "\"Set up hardware data with sudo\" again."
)
ERROR_HELPER_FAILED = N_("The helper reported an error, nothing was changed.")
ERROR_HELPER_TIMEOUT = N_("The helper did not finish in time.")
ERROR_PAGEOUT_REFUSED = N_(
    "This program protects its memory or runs with other rights, "
    "so it cannot be moved to swap."
)
ERROR_PAGEOUT_KERNEL = N_("This kernel is too old to move a single program to swap.")
ERROR_PAGEOUT_NO_PYTHON = N_("Moving to swap needs python3 in /usr/bin, which is missing.")
ERROR_DROP_REFUSED = N_("The system refused to empty the cache.")


def format_bytes(value: int | float) -> str:
    if value >= GIB:
        return f"{format_decimal(value / GIB)} GB"
    return f"{format_decimal(value / MIB)} MB"


def is_essential(name: str) -> bool:
    """Programs the desktop session cannot live without: no hints, no actions."""
    return name[:15] in _ESSENTIAL_NAMES or name.startswith(_ESSENTIAL_PREFIXES)


@dataclass(frozen=True)
class MemoryOverview:
    total: int
    used: int
    available: int
    cached: int
    # None: not known, which is the case for a remote server.
    swap_total: int | None = None
    swap_used: int | None = None
    pressure_some: float | None = None
    pressure_full: float | None = None


@dataclass
class ProgramRow:
    key: tuple[str, str]
    name: str
    username: str
    memory_bytes: int
    # None unless every process could be measured exactly.
    swap_bytes: int | None
    exact: bool
    processes: list[tuple[int, float]]
    cpu_seconds: float = 0.0
    essential: bool = False
    own: bool = True
    idle_seconds: float = 0.0


@dataclass(frozen=True)
class Hint:
    kind: str  # "idle" or "large"
    name: str
    memory_bytes: int
    idle_seconds: float = 0.0


@dataclass(frozen=True)
class MemorySnapshot:
    overview: MemoryOverview
    rows: list[ProgramRow]
    hints: list[Hint]
    ram_sensors: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class SwapDevice:
    name: str
    type: str
    size: int
    used: int


@dataclass(frozen=True)
class MemoryModule:
    locator: str
    size: int
    form_factor: str = ""
    type: str = ""
    type_detail: str = ""
    speed: str = ""
    configured_speed: str = ""
    manufacturer: str = ""
    part_number: str = ""
    rank: str = ""
    voltage: str = ""


@dataclass(frozen=True)
class MemoryHardware:
    max_capacity: int | None = None
    error_correction: str = ""
    slots_total: int = 0
    modules: tuple[MemoryModule, ...] = ()


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo in bytes. Counters without a unit, like HugePages_Total, stay plain numbers."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            number = int(fields[0])
        except ValueError:
            continue
        values[name.strip()] = number * 1024 if fields[1:] == ["kB"] else number
    return values


def parse_swaps(text: str) -> list[SwapDevice]:
    """/proc/swaps, whose sizes are in KiB. The kernel writes spaces in a path as \\040."""
    devices = []
    for line in text.splitlines()[1:]:
        # From the right, as only the name can contain anything but digits.
        fields = line.rsplit(None, 4)
        if len(fields) != 5:
            continue
        try:
            size, used = int(fields[2]) * 1024, int(fields[3]) * 1024
        except ValueError:
            continue
        devices.append(SwapDevice(fields[0].replace("\\040", " "), fields[1], size, used))
    return devices


def read_meminfo() -> dict[str, int]:
    try:
        with open(MEMINFO_FILE, encoding="ascii") as handle:
            return parse_meminfo(handle.read())
    except OSError:
        return {}


def read_swaps() -> list[SwapDevice]:
    try:
        with open(SWAPS_FILE, encoding="utf-8", errors="replace") as handle:
            return parse_swaps(handle.read())
    except OSError:
        return []


_DMI_PLACEHOLDERS = frozenset(("", "unknown", "not specified", "not provided", "none",
                               "no module installed", "not installed", "undefined"))
_SIZE_UNITS = {"KB": 1024, "MB": MIB, "GB": GIB, "TB": 1024 * GIB}


def _dmi_text(value: str | None) -> str:
    """The value of a dmidecode field, empty for what firmware writes instead of nothing."""
    value = (value or "").strip()
    if value.lower() in _DMI_PLACEHOLDERS or not value.strip("0"):
        return ""
    return value


def _dmi_size(value: str | None) -> int | None:
    fields = (value or "").split()
    if len(fields) != 2 or fields[1].upper() not in _SIZE_UNITS:
        return None
    try:
        size = int(fields[0])
    except ValueError:
        return None
    return size * _SIZE_UNITS[fields[1].upper()] or None


def _dmi_blocks(text: str) -> list[tuple[str, str, dict[str, str]]]:
    """Every block as (title, handle, fields). Only single-tab lines are fields: deeper lines
    belong to a list under the field above them.
    """
    blocks: list[tuple[str, str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    handle = ""
    expect_title = False
    for line in text.splitlines():
        if line.startswith("Handle "):
            handle = line.split()[1].rstrip(",")
            expect_title = True
            current = None
        elif expect_title:
            current = {}
            blocks.append((line.strip(), handle, current))
            expect_title = False
        elif current is not None and line.startswith("\t") and not line.startswith("\t\t"):
            name, _, value = line.strip().partition(":")
            current[name] = value.strip()
    return blocks


def parse_dmi_memory(text: str) -> MemoryHardware:
    """The output of "dmidecode -t memory". The serial number is left out on purpose: it
    identifies the machine and tells nothing about its memory.
    """
    blocks = _dmi_blocks(text)
    max_capacity = 0
    corrections: list[str] = []
    system_arrays = set()
    for title, handle, fields in blocks:
        if title != "Physical Memory Array" or fields.get("Use") != "System Memory":
            continue
        system_arrays.add(handle)
        max_capacity += _dmi_size(fields.get("Maximum Capacity")) or 0
        correction = fields.get("Error Correction Type", "").strip()
        if correction and correction not in corrections:
            corrections.append(correction)

    slots = 0
    modules = []
    for title, _handle, fields in blocks:
        if title != "Memory Device":
            continue
        # Flash chips and the like have an array of their own, boards without any array data
        # still have memory.
        if system_arrays and fields.get("Array Handle") not in system_arrays:
            continue
        slots += 1
        size = _dmi_size(fields.get("Size"))
        if size is None:
            continue
        modules.append(MemoryModule(
            locator=_dmi_text(fields.get("Locator")) or _dmi_text(fields.get("Bank Locator")),
            size=size,
            form_factor=_dmi_text(fields.get("Form Factor")),
            type=_dmi_text(fields.get("Type")),
            type_detail=_dmi_text(fields.get("Type Detail")),
            speed=_dmi_text(fields.get("Speed")),
            configured_speed=_dmi_text(fields.get("Configured Memory Speed")
                                       or fields.get("Configured Clock Speed")),
            manufacturer=_dmi_text(fields.get("Manufacturer")),
            part_number=_dmi_text(fields.get("Part Number")),
            rank=_dmi_text(fields.get("Rank")),
            voltage=_dmi_text(fields.get("Configured Voltage")),
        ))
    # "None" is a real answer here, unlike in the other fields.
    known = [correction for correction in corrections
             if correction == "None" or _dmi_text(correction)]
    return MemoryHardware(max_capacity=max_capacity or None, error_correction=", ".join(known),
                          slots_total=slots, modules=tuple(modules))


def read_memory_hardware() -> MemoryHardware | None:
    """None if the module data cannot be read, which needs the setup under Settings."""
    text = read_dmi_memory()
    if text is None:
        return None
    hardware = parse_dmi_memory(text)
    return hardware if hardware.slots_total else None


def _module_title(module: MemoryModule, number: int) -> str:
    if module.locator:
        return tr("Module {number}: {slot}", number=number, slot=module.locator)
    return tr("Module {number}", number=number)


def _hardware_rows(hardware: MemoryHardware) -> list[tuple[str, str]]:
    rows = []
    if hardware.max_capacity:
        rows.append((tr("Maximum capacity"), format_bytes(hardware.max_capacity)))
    rows.append((tr("Slots in use"), tr("{used} of {total}", used=len(hardware.modules),
                                        total=hardware.slots_total)))
    if hardware.error_correction:
        correction = hardware.error_correction
        rows.append((tr("Error correction"), tr("None") if correction == "None" else correction))
    return rows


def _module_rows(module: MemoryModule) -> list[tuple[str, str]]:
    candidates = (
        (N_("Size"), format_bytes(module.size)),
        (N_("Type"), " ".join(part for part in (module.type, module.form_factor) if part)),
        (N_("Details"), module.type_detail),
        (N_("Maximum speed"), module.speed),
        (N_("Current speed"), module.configured_speed),
        (N_("Manufacturer"), module.manufacturer),
        (N_("Part number"), module.part_number),
        (N_("Rank"), module.rank),
        (N_("Voltage"), module.voltage),
    )
    return [(tr(label), value) for label, value in candidates if value]


def _temperature_rows(ram_sensors: list[dict] | None) -> list[tuple[str, str]]:
    if ram_sensors is None:
        return [(tr("Temperature"), tr("Unknown"))]
    temperatures = [sensor for sensor in ram_sensors if sensor.get("unit") == "°C"]
    if not temperatures:
        return [(tr("Temperature"), tr("No sensor found"))]
    # All sensors of a kind have the same name, so a number tells them apart.
    return [(tr("Sensor {number}", number=number),
             f"{format_decimal(sensor['value'])} °C")
            for number, sensor in enumerate(temperatures, 1)]


def memory_data_sections(
    overview: MemoryOverview,
    meminfo: dict[str, int],
    swaps: list[SwapDevice],
    hardware: MemoryHardware | None,
    ram_sensors: list[dict] | None,
    remote: bool = False,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Everything known about the memory as (section, [(label, value)]), already translated."""
    def from_meminfo(*items: tuple[str, str]) -> list[tuple[str, str]]:
        return [(tr(label), format_bytes(meminfo[key])) for label, key in items if key in meminfo]

    usage = [
        (tr("Total"), format_bytes(overview.total)),
        (tr("In use"), format_bytes(overview.used)),
        (tr("Available now"), format_bytes(overview.available)),
        *from_meminfo((N_("Free"), "MemFree")),
        (tr("Cache"), format_bytes(overview.cached)),
        *from_meminfo((N_("Buffers"), "Buffers"), (N_("Shared"), "Shmem"),
                      (N_("Active"), "Active"), (N_("Inactive"), "Inactive"),
                      (N_("Waiting to be written"), "Dirty"), (N_("Being written"), "Writeback"),
                      (N_("Mapped files"), "Mapped")),
    ]
    if overview.pressure_some is not None:
        usage.append((tr("Memory pressure (1 min)"),
                      f"{format_decimal(overview.pressure_some)} %"))

    kernel = from_meminfo((N_("Kernel caches (slab)"), "Slab"), (N_("Kernel stacks"), "KernelStack"),
                          (N_("Page tables"), "PageTables"), (N_("Promised to programs"), "Committed_AS"),
                          (N_("Commit limit"), "CommitLimit"))
    if meminfo.get("HugePages_Total"):
        kernel.append((tr("Huge pages"), tr("{used} of {total}",
                                            used=meminfo["HugePages_Total"] - meminfo.get("HugePages_Free", 0),
                                            total=meminfo["HugePages_Total"])))

    if overview.swap_total is None:
        swap = [(tr("Total"), tr("Unknown"))]
    elif overview.swap_total == 0:
        swap = [(tr("Total"), tr("Not set up"))]
    else:
        swap_used = overview.swap_used or 0
        swap = [(tr("Total"), format_bytes(overview.swap_total)),
                (tr("Used"), format_bytes(swap_used)),
                (tr("Free"), format_bytes(max(0, overview.swap_total - swap_used))),
                *from_meminfo((N_("Also still in memory"), "SwapCached"))]
        swap += [(device.name, tr("{used} of {total}", used=format_bytes(device.used),
                                  total=format_bytes(device.size)))
                 for device in swaps]

    sections = [(tr("Usage"), usage)]
    if kernel:
        sections.append((tr("Kernel"), kernel))
    sections.append((tr("Swap"), swap))
    if hardware is not None:
        sections.append((tr("Hardware"), _hardware_rows(hardware)))
        sections += [(_module_title(module, number), _module_rows(module))
                     for number, module in enumerate(hardware.modules, 1)]
    elif remote:
        sections.append((tr("Hardware"), [(tr("Modules"), tr("Only shown for this PC"))]))
    else:
        sections.append((tr("Hardware"), [(tr("Modules"), tr(
            "Needs \"Set up hardware data with sudo\" under Settings"))]))
    sections.append((tr("Temperature"), _temperature_rows(ram_sensors)))
    return sections


def parse_pressure(text: str) -> tuple[float | None, float | None]:
    """The avg60 values of the "some" and "full" lines of a PSI file."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        for field in fields[1:]:
            name, _, value = field.partition("=")
            if name == "avg60":
                try:
                    values[fields[0]] = float(value)
                except ValueError:
                    pass
    return values.get("some"), values.get("full")


def read_overview() -> MemoryOverview:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        with open(PRESSURE_FILE, encoding="ascii") as handle:
            some, full = parse_pressure(handle.read())
    except OSError:
        # Kernels without CONFIG_PSI, or with psi=0 on the command line.
        some, full = None, None
    return MemoryOverview(
        total=vm.total,
        used=vm.total - vm.available,
        available=vm.available,
        cached=getattr(vm, "cached", 0) + getattr(vm, "buffers", 0),
        swap_total=swap.total,
        swap_used=swap.used,
        pressure_some=some,
        pressure_full=full,
    )


def pressure_level(overview: MemoryOverview) -> str | None:
    """"none", "light" or "high"; None if the system does not report pressure."""
    if overview.pressure_some is None:
        return None
    if overview.pressure_some >= PRESSURE_HIGH:
        return "high"
    if overview.pressure_some >= PRESSURE_LIGHT:
        return "light"
    return "none"


def is_tight(overview: MemoryOverview) -> bool:
    if pressure_level(overview) == "high":
        return True
    return overview.total > 0 and overview.available < overview.total * TIGHT_AVAILABLE_SHARE


def _current_username() -> str:
    try:
        return psutil.Process().username()
    except psutil.Error:
        return ""


def _program_name(process: psutil.Process) -> str:
    """Name a process is listed under. Browsers and Node rename their main thread ("Isolated Web
    Co", "MainThread"), which is only visible as a difference to the program that was started.
    """
    name = process.name()
    if is_essential(name):
        return name
    try:
        program = os.path.basename(process.exe())
        if not program or program == name:
            return name
        cmdline = process.cmdline()
    except psutil.Error:
        return name
    # Programs started under another name (pipewire-pulse) or with a title of their own (nginx)
    # do not name their executable here and keep their name.
    if cmdline and os.path.basename(cmdline[0]) == program:
        return program
    return name


def _process_key(process: psutil.Process, own_pid: int) -> tuple[str, str]:
    """(name, username) of a process; this program is listed apart from other Python programs."""
    username = process.username()
    if process.pid == own_pid:
        return OWN_PROGRAM_NAME, username
    return _program_name(process), username


def _measure(process: psutil.Process) -> tuple[int, int | None]:
    """Memory of a process as (PSS, swap), or (RSS, None) if only that may be read."""
    try:
        info = process.memory_full_info()
        return info.pss, info.swap
    except psutil.AccessDenied:
        return process.memory_info().rss, None


def scan_programs() -> list[ProgramRow]:
    """All programs with memory, largest first. PSS keeps shared memory from counting twice."""
    own_pid = os.getpid()
    me = _current_username()
    rows: dict[tuple[str, str], ProgramRow] = {}
    for process in psutil.process_iter():
        try:
            key = _process_key(process, own_pid)
            memory, swap = _measure(process)
            # Kernel threads have no memory of their own.
            if not memory and not swap:
                continue
            times = process.cpu_times()
            created = process.create_time()
        except psutil.Error:
            continue
        row = rows.get(key)
        if row is None:
            row = rows[key] = ProgramRow(
                key=key, name=key[0], username=key[1], memory_bytes=0, swap_bytes=0, exact=True,
                processes=[], essential=process.pid == own_pid or is_essential(key[0]),
                own=key[1] == me)
        row.memory_bytes += memory
        if swap is None:
            row.exact = False
            row.swap_bytes = None
        elif row.swap_bytes is not None:
            row.swap_bytes += swap
        row.processes.append((process.pid, created))
        row.cpu_seconds += times.user + times.system
    return sorted(rows.values(), key=lambda row: row.memory_bytes, reverse=True)


def sample_cpu_times() -> dict[tuple[str, str], float]:
    """CPU seconds per program, the cheap scan that keeps idle times right in the background."""
    own_pid = os.getpid()
    samples: dict[tuple[str, str], float] = {}
    for process in psutil.process_iter():
        try:
            key = _process_key(process, own_pid)
            times = process.cpu_times()
        except psutil.Error:
            continue
        samples[key] = samples.get(key, 0.0) + times.user + times.system
    return samples


class ActivityTracker:
    """Observes since when programs have not used the CPU. Knows nothing before the app started."""

    def __init__(self) -> None:
        self._state: dict[tuple[str, str], tuple[float, float, float]] = {}

    def update(self, samples: dict[tuple[str, str], float], now: float) -> dict[tuple[str, str], float]:
        state: dict[tuple[str, str], tuple[float, float, float]] = {}
        idle: dict[tuple[str, str], float] = {}
        for key, cpu_seconds in samples.items():
            previous = self._state.get(key)
            if previous is None:
                last_active = now
            else:
                old_cpu, old_time, last_active = previous
                elapsed = now - old_time
                # The sum shrinks when one of the program's processes ends.
                used = max(0.0, cpu_seconds - old_cpu)
                if elapsed > 0 and used >= elapsed * ACTIVE_CPU_SHARE:
                    last_active = now
            state[key] = (cpu_seconds, now, last_active)
            idle[key] = now - last_active
        self._state = state
        return idle


def build_hints(rows: list[ProgramRow], overview: MemoryOverview) -> list[Hint]:
    """At most a few suggestions; an idle program is the better candidate than a busy one."""
    large_from = max(LARGE_MIN_BYTES, overview.total * LARGE_MIN_SHARE)
    idle_from = max(IDLE_MIN_BYTES, overview.total * IDLE_MIN_SHARE)
    hints: list[Hint] = []
    for row in sorted(rows, key=lambda row: row.memory_bytes, reverse=True):
        if not row.own or row.essential:
            continue
        if row.memory_bytes >= idle_from and row.idle_seconds >= IDLE_MIN_SECONDS:
            hints.append(Hint("idle", row.name, row.memory_bytes, row.idle_seconds))
        elif row.memory_bytes >= large_from:
            hints.append(Hint("large", row.name, row.memory_bytes))
        if len(hints) == MAX_HINTS:
            break
    return hints


def overview_from_remote(system: dict) -> MemoryOverview:
    total = int(system.get("mem_total_gb", 0) * GIB)
    available = int(system.get("mem_available_gb", 0) * GIB)
    return MemoryOverview(
        total=total, used=total - available, available=available,
        cached=int(system.get("mem_cached_gb", 0) * GIB))


def rows_from_remote(processes: list[dict]) -> list[ProgramRow]:
    """Server data only has the RSS of each process, so shared memory counts more than once.

    "own" stays set for hints: nothing can be done from here anyway, as the page locks all
    actions in remote mode, and a large program on the server is still worth a mention.
    """
    rows: dict[tuple[str, str], ProgramRow] = {}
    for process in processes:
        memory = int(process.get("memory_mb", 0) * MIB)
        if memory <= 0:
            continue
        key = (process.get("name", ""), process.get("username", ""))
        row = rows.get(key)
        if row is None:
            row = rows[key] = ProgramRow(
                key=key, name=key[0], username=key[1], memory_bytes=0, swap_bytes=None,
                exact=False, processes=[], essential=is_essential(key[0]))
        row.memory_bytes += memory
        row.processes.append((process.get("pid", 0), 0.0))
    return sorted(rows.values(), key=lambda row: row.memory_bytes, reverse=True)


class MemoryWorker(StoppableWorker):
    """Runs from the start of the app, otherwise idle times could only start with the first look."""

    snapshotReady = Signal(object)
    hardwareReady = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracker = ActivityTracker()
        self._visible = threading.Event()
        self._refresh = threading.Event()
        self._consumed = threading.Event()
        self._consumed.set()
        self._hardware_wanted = threading.Event()
        self._hardware_loaded = False

    def set_visible(self, visible: bool) -> None:
        if visible:
            self._visible.set()
            self._refresh.set()
            # Asked again on every look, so the setup under Settings works without a restart.
            if not self._hardware_loaded:
                self._hardware_wanted.set()
        else:
            self._visible.clear()

    def request_refresh(self) -> None:
        self._refresh.set()

    def mark_consumed(self) -> None:
        self._consumed.set()

    def run(self) -> None:
        last_scan = None
        while not self._stop_event.is_set():
            visible = self._visible.is_set()
            interval = VISIBLE_INTERVAL if visible else HIDDEN_INTERVAL
            now = time.monotonic()
            if self._refresh.is_set() or last_scan is None or now - last_scan >= interval:
                self._refresh.clear()
                last_scan = now
                self._scan(visible, now)
            if self._hardware_wanted.is_set():
                self._hardware_wanted.clear()
                self._load_hardware()
            self._stop_event.wait(0.5)

    def _load_hardware(self) -> None:
        hardware = read_memory_hardware()
        if hardware is not None and not self._stop_event.is_set():
            self._hardware_loaded = True
            self.hardwareReady.emit(hardware)

    def _scan(self, visible: bool, now: float) -> None:
        if not visible:
            self._tracker.update(sample_cpu_times(), now)
            return
        rows = scan_programs()
        idle = self._tracker.update({row.key: row.cpu_seconds for row in rows}, now)
        for row in rows:
            row.idle_seconds = idle.get(row.key, 0.0)
        # A page that has not drawn the last result yet does not need the next one.
        if self._stop_event.is_set() or not self._consumed.is_set():
            return
        overview = read_overview()
        self._consumed.clear()
        self.snapshotReady.emit(MemorySnapshot(overview, rows, build_hints(rows, overview),
                                               list_ram_sensors()))


def _live_processes(processes: list[tuple[int, float]]) -> list[psutil.Process]:
    """The listed processes that still exist, leaving out pids that were reused since."""
    live = []
    for pid, created in processes:
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) <= _CREATE_TIME_TOLERANCE:
                live.append(process)
        except psutil.Error:
            continue
    return live


def _is_running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _swapped_bytes(processes: list[psutil.Process]) -> int:
    # Not the drop in RSS: shared memory leaves RSS without leaving RAM, so that would
    # report memory as moved that is still there.
    total = 0
    for process in processes:
        try:
            with open(f"/proc/{process.pid}/status") as handle:
                for line in handle:
                    if line.startswith("VmSwap:"):
                        total += int(line.split()[1]) * 1024
                        break
        except (OSError, ValueError, IndexError):
            continue
    return total


@dataclass(frozen=True)
class EndResult:
    name: str
    ended: int = 0
    denied: int = 0
    # Still running after the wait; the page asks before ending them by force.
    survivors: tuple[tuple[int, float], ...] = ()


class EndProgramWorker(StoppableWorker):
    """Asks all processes of a program to end, or with force=True, ends them right away."""

    resultReady = Signal(object)

    def __init__(self, name: str, processes: list[tuple[int, float]], force: bool = False, parent=None):
        super().__init__(parent)
        self._name = name
        self._processes = processes
        self._force = force

    def run(self) -> None:
        signalled: list[psutil.Process] = []
        denied = 0
        for process in _live_processes(self._processes):
            try:
                if self._force:
                    process.kill()
                else:
                    process.terminate()
                signalled.append(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                denied += 1

        deadline = time.monotonic() + _END_WAIT_SECONDS
        waiting = [process for process in signalled if _is_running(process)]
        while waiting and time.monotonic() < deadline:
            if self._stop_event.wait(_END_POLL_SECONDS):
                return
            waiting = [process for process in waiting if _is_running(process)]
        if self._stop_event.is_set():
            return

        survivors = []
        for process in waiting:
            try:
                survivors.append((process.pid, process.create_time()))
            except psutil.Error:
                continue
        self.resultReady.emit(EndResult(
            self._name, ended=len(signalled) - len(survivors), denied=denied,
            survivors=tuple(survivors)))


def _helper_error(returncode: int) -> str:
    """Text for the exit codes every helper command has in common."""
    if returncode == 1:  # from sudo: no rule for this helper
        return ERROR_NOT_SET_UP
    if returncode == 2:  # a helper from before these commands sees an invalid device
        return ERROR_HELPER_OLD
    return ERROR_HELPER_FAILED


@dataclass(frozen=True)
class PageOutResult:
    name: str
    error: str = ""
    moved_bytes: int = 0
    refused: int = 0


class PageOutWorker(StoppableWorker):
    """Moves the memory of a program to swap. The program keeps running and fetches it back on use."""

    resultReady = Signal(object)

    def __init__(self, name: str, processes: list[tuple[int, float]], parent=None):
        super().__init__(parent)
        self._name = name
        self._processes = processes

    def run(self) -> None:
        try:
            result = self._page_out()
        except WorkerStopped:
            return
        if not self._stop_event.is_set():
            self.resultReady.emit(result)

    def _page_out(self) -> PageOutResult:
        helper = helper_path()
        if not helper:
            return PageOutResult(self._name, error=ERROR_NOT_SET_UP)
        live = _live_processes(self._processes)
        if not live:
            return PageOutResult(self._name)
        before = _swapped_bytes(live)
        command = ["sudo", "-n", helper, "pageout", *(str(process.pid) for process in live)]
        try:
            done = self._run_command(command, _HELPER_TIMEOUT)
        except subprocess.TimeoutExpired:
            return PageOutResult(self._name, error=ERROR_HELPER_TIMEOUT)
        except OSError:
            return PageOutResult(self._name, error=ERROR_NOT_SET_UP)
        if done.returncode == 3:
            return PageOutResult(self._name, error=ERROR_PAGEOUT_REFUSED)
        if done.returncode == 4:
            return PageOutResult(self._name, error=ERROR_PAGEOUT_KERNEL)
        if done.returncode == 127:
            return PageOutResult(self._name, error=ERROR_PAGEOUT_NO_PYTHON)
        if done.returncode != 0:
            return PageOutResult(self._name, error=_helper_error(done.returncode))
        # The numbers settle right after the kernel has written the pages.
        self._stop_event.wait(_SETTLE_SECONDS)
        return PageOutResult(self._name, moved_bytes=max(0, _swapped_bytes(live) - before),
                             refused=parse_refused(done.stdout))


def parse_refused(output: str) -> int:
    for field in output.split():
        name, _, value = field.partition("=")
        if name == "refused" and value.isdigit():
            return int(value)
    return 0


@dataclass(frozen=True)
class DropCachesResult:
    error: str = ""
    before_bytes: int = 0
    after_bytes: int = 0


class DropCachesWorker(StoppableWorker):
    resultReady = Signal(object)

    def run(self) -> None:
        try:
            result = self._drop()
        except WorkerStopped:
            return
        if not self._stop_event.is_set():
            self.resultReady.emit(result)

    def _drop(self) -> DropCachesResult:
        helper = helper_path()
        if not helper:
            return DropCachesResult(error=ERROR_NOT_SET_UP)
        before = read_overview().cached
        try:
            done = self._run_command(["sudo", "-n", helper, "drop-caches"], _HELPER_TIMEOUT)
        except subprocess.TimeoutExpired:
            return DropCachesResult(error=ERROR_HELPER_TIMEOUT)
        except OSError:
            return DropCachesResult(error=ERROR_NOT_SET_UP)
        if done.returncode == 3:
            return DropCachesResult(error=ERROR_DROP_REFUSED)
        if done.returncode != 0:
            return DropCachesResult(error=_helper_error(done.returncode))
        return DropCachesResult(before_bytes=before, after_bytes=read_overview().cached)
