"""psutil helpers. All per-process CPU sampling is non-blocking (interval=None)
so the process list never stalls the UI thread, no matter how many processes exist.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

import psutil
from PySide6.QtCore import QThread, Signal

_NVIDIA_SMI = shutil.which("nvidia-smi")
_SMART_HELPER = "/usr/local/libexec/wintaskmanager-smartctl"

_DRM_CLIENT_ID_RE = re.compile(r"^drm-client-id:\s+(\d+)")
_DRM_VRAM_RE = re.compile(r"^drm-memory-vram:\s+(\d+)\s+KiB")
_DRM_ENGINE_RE = re.compile(r"^drm-engine-(\w+):\s+(\d+)\s+ns")
_GPU_ENGINE_LABELS = {
    "gfx": "3D", "compute": "Compute", "sdma": "Kopieren", "dma": "Kopieren",
    "enc": "Video-Encode", "dec": "Video-Decode", "jpeg": "JPEG",
}
GPU_ENGINE_METRICS = ["3D", "Compute", "Kopieren", "Video-Encode", "Video-Decode",
                      "JPEG", "Speicher-Controller"]


class GpuMonitor:
    """Best-effort per-process GPU usage (core % + VRAM in MB).

    Uses `nvidia-smi` when available (proprietary NVIDIA driver). Otherwise falls
    back to the generic Linux DRM "fdinfo" interface exposed by the kernel for
    amdgpu/i915/xe/nouveau, which is what tools like nvtop rely on for non-NVIDIA
    GPUs. GPU % is derived from the delta of the engine's cumulative busy time
    between two polls, so it only becomes meaningful after the first poll.
    """

    def __init__(self):
        self._nvidia = _NVIDIA_SMI
        self._prev_busy: dict[int, tuple[float, int]] = {}
        self._backend_available = bool(self._nvidia) or self._probe_drm_fdinfo()

    def available(self) -> bool:
        return self._backend_available

    @staticmethod
    def _probe_drm_fdinfo() -> bool:
        try:
            for pid_str in os.listdir("/proc"):
                if not pid_str.isdigit():
                    continue
                fdinfo_dir = f"/proc/{pid_str}/fdinfo"
                try:
                    fd_names = os.listdir(fdinfo_dir)
                except OSError:
                    continue
                for fd in fd_names:
                    try:
                        with open(f"{fdinfo_dir}/{fd}") as f:
                            if "drm-driver:" in f.read():
                                return True
                    except OSError:
                        continue
        except OSError:
            pass
        return False

    def poll(self) -> dict[int, dict]:
        if self._nvidia:
            return self._poll_nvidia()
        return self._poll_drm_fdinfo()

    def _poll_nvidia(self) -> dict[int, dict]:
        usage: dict[int, dict] = {}
        try:
            out = subprocess.check_output(
                [self._nvidia, "pmon", "-c", "1", "-s", "u"],
                text=True, encoding="utf-8", errors="replace", timeout=3, stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    pid = int(parts[1])
                except ValueError:
                    continue
                sm = parts[3]
                usage.setdefault(pid, {})["gpu_percent"] = (
                    float(sm) if sm.replace(".", "", 1).isdigit() else 0.0
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass

        try:
            out = subprocess.check_output(
                [self._nvidia, "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                text=True, encoding="utf-8", errors="replace", timeout=3, stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 2:
                    continue
                try:
                    pid = int(parts[0])
                    vram_mb = float(parts[1])
                except ValueError:
                    continue
                usage.setdefault(pid, {})["vram_mb"] = vram_mb
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
        return usage

    def _poll_drm_fdinfo(self) -> dict[int, dict]:
        now = time.monotonic()
        result: dict[int, dict] = {}

        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            fdinfo_dir = f"/proc/{pid_str}/fdinfo"
            try:
                fd_names = os.listdir(fdinfo_dir)
            except OSError:
                continue

            vram_kib = 0
            busy_ns = 0
            found_drm = False
            seen_clients: set[str] = set()  # dedupe fds pointing at the same DRM client
            for fd in fd_names:
                try:
                    with open(f"{fdinfo_dir}/{fd}") as f:
                        content = f.read()
                except OSError:
                    continue
                if "drm-driver:" not in content:
                    continue

                client_match = _DRM_CLIENT_ID_RE.search(content)
                client_id = client_match.group(1) if client_match else fd
                if client_id in seen_clients:
                    continue
                seen_clients.add(client_id)
                found_drm = True

                for line in content.splitlines():
                    vram_match = _DRM_VRAM_RE.match(line)
                    if vram_match:
                        vram_kib += int(vram_match.group(1))
                        continue
                    engine_match = _DRM_ENGINE_RE.match(line)
                    if engine_match and engine_match.group(1) in ("gfx", "compute"):
                        busy_ns += int(engine_match.group(2))

            if not found_drm:
                continue

            gpu_percent = 0.0
            prev = self._prev_busy.get(pid)
            if prev is not None:
                prev_time, prev_busy = prev
                dt = now - prev_time
                if dt > 0:
                    gpu_percent = max(0.0, min(100.0, (busy_ns - prev_busy) / (dt * 1e9) * 100))
            self._prev_busy[pid] = (now, busy_ns)
            result[pid] = {"vram_mb": vram_kib / 1024, "gpu_percent": gpu_percent}

        return result


class GpuSystemMonitor:
    """Best-effort overall (not per-process) GPU stats: utilization/VRAM/temp/power.

    Uses `nvidia-smi` if present, else the AMD/Intel sysfs interface under
    /sys/class/drm/cardN/device (gpu_busy_percent, mem_info_vram_*, hwmon
    temp/power). Cheap enough (plain file reads) to poll from the UI thread.
    """

    def __init__(self):
        self._nvidia = _NVIDIA_SMI
        self._card_path = None if self._nvidia else self._find_card()
        self._prev_engine_busy: dict[str, int] = {}
        self._prev_engine_time: float | None = None

    def available(self) -> bool:
        return bool(self._nvidia or self._card_path)

    @staticmethod
    def _find_card() -> str | None:
        base = "/sys/class/drm"
        try:
            for name in sorted(os.listdir(base)):
                if not re.match(r"^card\d+$", name):
                    continue
                device = f"{base}/{name}/device"
                if os.path.exists(f"{device}/gpu_busy_percent"):
                    return device
        except OSError:
            pass
        return None

    def poll(self) -> dict | None:
        if self._nvidia:
            return self._poll_nvidia()
        if self._card_path:
            result = self._poll_sysfs()
            if result is not None:
                result["engines"] = self._poll_sysfs_engines()
            return result
        return None

    def _poll_nvidia(self) -> dict | None:
        try:
            out = subprocess.check_output(
                [self._nvidia,
                 "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                 "temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
            name, util, mem_used, mem_total, temp, power = (
                p.strip() for p in out.splitlines()[0].split(",")
            )
            result = {
                "name": name,
                "percent": float(util),
                "vram_used_mb": float(mem_used),
                "vram_total_mb": float(mem_total),
                "temp_c": float(temp),
                "power_w": float(power),
            }
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
                IndexError, ValueError):
            return None
            result["engines"] = self._poll_nvidia_engines()
        return result | {"gtt_used_mb": None, "gtt_total_mb": None}

    def _poll_nvidia_engines(self) -> dict[str, float]:
        try:
            out = subprocess.check_output(
                [self._nvidia, "dmon", "-c", "1", "-s", "u"],
                text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                _gpu, sm, mem, enc, dec = parts[:5]
                return {
                    "3D": float(sm), "Speicher-Controller": float(mem),
                    "Video-Encode": float(enc), "Video-Decode": float(dec),
                }
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return {}

    def _poll_sysfs_engines(self) -> dict[str, float]:
        now = time.monotonic()
        busy = self._collect_engine_busy_ns()
        engines: dict[str, float] = {}
        if self._prev_engine_time is not None:
            dt = now - self._prev_engine_time
            if dt > 0:
                for engine, ns in busy.items():
                    prev_ns = self._prev_engine_busy.get(engine, ns)
                    delta = ns - prev_ns
                    if delta >= 0:
                        percent = max(0.0, min(100.0, delta / (dt * 1e9) * 100))
                        label = _GPU_ENGINE_LABELS.get(engine, engine)
                        engines[label] = engines.get(label, 0.0) + percent
        self._prev_engine_busy = busy
        self._prev_engine_time = now
        return engines

    @staticmethod
    def _collect_engine_busy_ns() -> dict[str, int]:
        """Sum drm-engine-* busy-ns across all processes, deduped by DRM client."""
        busy: dict[str, int] = {}
        try:
            pid_list = os.listdir("/proc")
        except OSError:
            return busy
        for pid_str in pid_list:
            if not pid_str.isdigit():
                continue
            fdinfo_dir = f"/proc/{pid_str}/fdinfo"
            try:
                fd_names = os.listdir(fdinfo_dir)
            except OSError:
                continue
            seen_clients: set[str] = set()
            for fd in fd_names:
                try:
                    with open(f"{fdinfo_dir}/{fd}") as f:
                        content = f.read()
                except OSError:
                    continue
                if "drm-driver:" not in content:
                    continue
                client_match = _DRM_CLIENT_ID_RE.search(content)
                client_id = f"{pid_str}:{client_match.group(1) if client_match else fd}"
                if client_id in seen_clients:
                    continue
                seen_clients.add(client_id)
                for line in content.splitlines():
                    engine_match = _DRM_ENGINE_RE.match(line)
                    if engine_match:
                        engine = engine_match.group(1)
                        busy[engine] = busy.get(engine, 0) + int(engine_match.group(2))
        return busy

    def _poll_sysfs(self) -> dict | None:
        percent = self._read_float(f"{self._card_path}/gpu_busy_percent") or 0.0
        vram_used = self._read_float(f"{self._card_path}/mem_info_vram_used")
        vram_total = self._read_float(f"{self._card_path}/mem_info_vram_total")
        gtt_used = self._read_float(f"{self._card_path}/mem_info_gtt_used")
        gtt_total = self._read_float(f"{self._card_path}/mem_info_gtt_total")
        temp_c = power_w = None
        hwmon_base = f"{self._card_path}/hwmon"
        try:
            hwmon_dirs = os.listdir(hwmon_base)
        except OSError:
            hwmon_dirs = []
        for hwmon in hwmon_dirs:
            hwmon_dir = f"{hwmon_base}/{hwmon}"
            temp_c = self._read_float(f"{hwmon_dir}/temp1_input")
            temp_c = temp_c / 1000 if temp_c is not None else None
            power_raw = (self._read_float(f"{hwmon_dir}/power1_average")
                         or self._read_float(f"{hwmon_dir}/power1_input"))
            power_w = power_raw / 1_000_000 if power_raw is not None else None
            break
        return {
            "name": self._read_name(),
            "percent": percent,
            "vram_used_mb": (vram_used / (1024 * 1024)) if vram_used is not None else None,
            "vram_total_mb": (vram_total / (1024 * 1024)) if vram_total is not None else None,
            "gtt_used_mb": (gtt_used / (1024 * 1024)) if gtt_used is not None else None,
            "gtt_total_mb": (gtt_total / (1024 * 1024)) if gtt_total is not None else None,
            "temp_c": temp_c,
            "power_w": power_w,
        }

    @staticmethod
    def _read_float(path: str) -> float | None:
        try:
            with open(path) as f:
                return float(f.read().strip())
        except (OSError, ValueError):
            return None

    def _read_name(self) -> str:
        try:
            with open(f"{self._card_path}/uevent") as f:
                match = re.search(r"DRIVER=(\w+)", f.read())
            if match:
                return f"GPU ({match.group(1)})"
        except OSError:
            pass
        return "GPU"


class GpuSystemWorker(QThread):
    """Poll overall GPU statistics without blocking the Qt UI thread."""

    snapshotReady = Signal(object)

    def __init__(self, interval: float = 0.5, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._running = True
        self._monitor = GpuSystemMonitor()

    def stop(self) -> None:
        self._running = False
        self.wait(200)
        if self.isRunning():
            self.terminate()
            self.wait(200)

    def run(self) -> None:
        while self._running:
            self.snapshotReady.emit(self._monitor.poll())
            time.sleep(self._interval)


class CpuPowerMonitor:
    """Best-effort CPU package power draw via Linux RAPL (/sys/class/powercap).
    Mainly available on Intel (and some AMD kernels via amd_energy). Returns
    None when unavailable so the UI can show 'Nicht verfügbar' instead of 0.
    """

    def __init__(self):
        self._paths = self._find_rapl_energy_paths()
        self._hwmon_power_paths = self._find_hwmon_power_paths()
        self._prev: dict[str, tuple[float, int]] = {}

    def available(self) -> bool:
        return bool(self._paths or self._hwmon_power_paths)

    def unavailable_reason(self) -> str:
        return "Kein CPU-Leistungssensor vom Kernel bereitgestellt"

    @staticmethod
    def _find_rapl_energy_paths() -> list[str]:
        base = "/sys/class/powercap"
        paths = []
        try:
            for name in os.listdir(base):
                if not re.match(r"^(?:intel-rapl|amd_energy):\d+$", name):
                    continue  # skip sub-zones like intel-rapl:0:0 (cores/uncore)
                energy_path = f"{base}/{name}/energy_uj"
                if os.path.exists(energy_path):
                    paths.append(energy_path)
        except OSError:
            pass
        return paths

    @staticmethod
    def _find_hwmon_power_paths() -> list[str]:
        paths = []
        for hwmon_dir in glob.glob("/sys/class/hwmon/hwmon*"):
            name = _read_sysfs_str(f"{hwmon_dir}/name")
            if name not in ("zenpower", "zenpower3"):
                continue
            for filename in ("power1_average", "power1_input"):
                path = f"{hwmon_dir}/{filename}"
                if os.path.exists(path):
                    paths.append(path)
                    break
        return paths

    def poll_watts(self) -> float | None:
        if not self._paths and not self._hwmon_power_paths:
            return None
        now = time.monotonic()
        total_watts = 0.0
        got_any = False
        for path in self._paths:
            try:
                with open(path) as f:
                    energy_uj = int(f.read().strip())
            except (OSError, ValueError):
                continue
            prev = self._prev.get(path)
            self._prev[path] = (now, energy_uj)
            if prev is None:
                continue
            prev_time, prev_energy = prev
            dt = now - prev_time
            delta = energy_uj - prev_energy
            if dt <= 0 or delta < 0:  # counter wrapped or no time passed
                continue
            total_watts += (delta / 1_000_000) / dt
            got_any = True
        for path in self._hwmon_power_paths:
            try:
                with open(path) as f:
                    total_watts += int(f.read().strip()) / 1_000_000
                got_any = True
            except (OSError, ValueError):
                continue
        return total_watts if got_any else None


def get_cpu_model_name() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "Unbekannte CPU"


def get_cpu_temperature() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for key in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
        entries = temps.get(key)
        if not entries:
            continue
        for entry in entries:
            if entry.label in ("Tctl", "Package id 0", "Tdie"):
                return entry.current
        return entries[0].current
    for entries in temps.values():
        if entries:
            return entries[0].current
    return None


def get_ram_speed_mhz() -> int | None:
    """Configured DIMM speed via the restricted hardware helper when available."""
    commands = []
    if os.path.exists(_SMART_HELPER):
        commands.append(["sudo", "-n", _SMART_HELPER, "memory"])
    dmidecode = shutil.which("dmidecode")
    if dmidecode:
        commands.append([dmidecode, "-t", "memory"])
    if not commands:
        return None
    out = None
    for command in commands:
        try:
            out = subprocess.check_output(
                command, text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            continue
    if out is None:
        return None
    configured_speeds = []
    reported_speeds = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Configured Memory Speed:"):
            match = re.search(r"(\d+)\s*(MT/s|MHz)", line)
            if match:
                configured_speeds.append(int(match.group(1)))
        elif line.startswith("Speed:"):
            match = re.search(r"(\d+)\s*(MT/s|MHz)", line)
            if match:
                reported_speeds.append(int(match.group(1)))
    speeds = configured_speeds or reported_speeds
    return max(speeds) if speeds else None


def get_ram_temperature() -> tuple[float | None, int]:
    """Return average temperature and detected DIMM sensor count (JC42/SPD)."""
    try:
        temperatures = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None, 0
    values = [
        entry.current
        for chip, entries in temperatures.items()
        if chip.lower().startswith(("jc42", "spd"))
        for entry in entries
        if entry.current is not None
    ]
    return (sum(values) / len(values), len(values)) if values else (None, 0)


def _read_sysfs_str(path: str) -> str | None:
    try:
        with open(path) as f:
            value = f.read().strip()
        return value or None
    except OSError:
        return None


def _read_disk_temp_c(dev_path: str) -> float | None:
    for hwmon_base in (f"{dev_path}/device/hwmon", f"{dev_path}/device/device/hwmon"):
        try:
            hwmon_dirs = os.listdir(hwmon_base)
        except OSError:
            continue
        for hwmon in hwmon_dirs:
            try:
                with open(f"{hwmon_base}/{hwmon}/temp1_input") as f:
                    return int(f.read().strip()) / 1000
            except (OSError, ValueError):
                continue
    return None


def _smart_value(data: dict, *keys: str) -> float | None:
    """Read the numeric SMART value stored directly or beneath ``value``."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            for nested_key in ("value", "hours", "current", "temperature"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, (int, float)):
                    value = nested_value
                    break
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _smartctl_disk_stats(device: str, use_sudo: bool = False) -> dict:
    """Return best-effort lifetime SMART metrics for one Linux block device."""
    smartctl = shutil.which("smartctl")
    commands = []
    if os.path.exists(_SMART_HELPER):
        commands.append(["sudo", "-n", _SMART_HELPER, device])
    if smartctl:
        command = [smartctl, "--all", "--json=o", f"/dev/{device}"]
        commands.append(command)
    if use_sudo and smartctl:
        commands.append(["sudo", "-n", *command])
    if not commands:
        return {}
    data = None
    for candidate in commands:
        try:
            completed = subprocess.run(
                candidate, text=True, capture_output=True, timeout=12, check=False,
            )
            data = json.loads(completed.stdout)
            break
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            continue
    if data is None:
        return {}

    temperature = _smart_value(data.get("temperature", {}), "current", "drive_temperature")
    power_cycles = _smart_value(data, "power_cycle_count", "power_cycles")
    power_on_hours = _smart_value(data, "power_on_time", "power_on_hours")
    read_bytes = write_bytes = None

    # NVMe data units are defined as 512,000 bytes by the NVMe specification.
    nvme = data.get("nvme_smart_health_information_log", {})
    nvme_read_units = _smart_value(nvme, "data_units_read")
    nvme_write_units = _smart_value(nvme, "data_units_written")
    if nvme_read_units is not None:
        read_bytes = nvme_read_units * 512_000
    if nvme_write_units is not None:
        write_bytes = nvme_write_units * 512_000
    if power_cycles is None:
        power_cycles = _smart_value(nvme, "power_cycles")
    if power_on_hours is None:
        power_on_hours = _smart_value(nvme, "power_on_hours")
    if temperature is None:
        temperature = _smart_value(nvme, "temperature", "composite_temperature")

    ata = data.get("ata_smart_attributes", {}).get("table", [])
    for attribute in ata:
        name = attribute.get("name")
        raw = _smart_value(attribute.get("raw", {}), "value")
        if raw is None:
            continue
        if name == "Power_Cycle_Count" and power_cycles is None:
            power_cycles = raw
        elif name == "Power_On_Hours" and power_on_hours is None:
            power_on_hours = raw
        elif name == "Temperature_Celsius" and temperature is None:
            temperature = raw
        elif name in ("Total_LBAs_Read", "Host_Reads_32MiB") and read_bytes is None:
            read_bytes = raw * (32 * 1024 * 1024 if name == "Host_Reads_32MiB" else 512)
        elif name in ("Total_LBAs_Written", "Host_Writes_32MiB") and write_bytes is None:
            write_bytes = raw * (32 * 1024 * 1024 if name == "Host_Writes_32MiB" else 512)
    return {
        "temp_c": temperature,
        "power_cycles": power_cycles,
        "power_on_hours": power_on_hours,
        "read_bytes_total": read_bytes,
        "write_bytes_total": write_bytes,
    }


class DiskSmartWorker(QThread):
    """Read potentially slow SMART data away from the UI thread."""

    snapshotReady = Signal(dict)

    def __init__(self, devices: list[str], use_sudo: bool = False,
                 interval: float = 60.0, parent=None):
        super().__init__(parent)
        self._devices = devices
        self._use_sudo = use_sudo
        self._interval = interval
        self._running = True

    def stop(self) -> None:
        self._running = False
        self.wait(200)
        if self.isRunning():
            self.terminate()
            self.wait(200)

    def run(self) -> None:
        while self._running:
            self.snapshotReady.emit({
                device: _smartctl_disk_stats(device, self._use_sudo) for device in self._devices
            })
            wait_seconds = 5.0 if not shutil.which("smartctl") else self._interval
            for _ in range(int(wait_seconds * 10)):
                if not self._running:
                    return
                time.sleep(0.1)


def list_physical_disks() -> list[dict]:
    """Enumerate physical block devices, like Windows Task Manager's Disk 0/1/...."""
    disks: list[dict] = []
    base = "/sys/block"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return disks

    partitions_by_disk: dict[str, list[str]] = {}
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError:
        partitions = []
    for part in partitions:
        dev = part.device.replace("/dev/", "")
        for name in names:
            if dev == name or dev.startswith(name):
                partitions_by_disk.setdefault(name, []).append(part.mountpoint)
                break

    for name in names:
        if re.match(r"^(loop|ram|zram|sr\d*|dm-)", name):
            continue
        dev_path = f"{base}/{name}"
        size_str = _read_sysfs_str(f"{dev_path}/size")
        size_bytes = int(size_str) * 512 if size_str and size_str.isdigit() else 0
        if size_bytes <= 0:
            continue

        model = _read_sysfs_str(f"{dev_path}/device/model") or name
        rotational = _read_sysfs_str(f"{dev_path}/queue/rotational")
        is_nvme = name.startswith("nvme")
        is_usb = "usb" in os.path.realpath(dev_path)

        if is_nvme:
            kind = "NVMe SSD"
        elif rotational == "0":
            kind = "SSD"
        else:
            kind = "HDD"
        interface = "USB" if is_usb else ("NVMe" if is_nvme else "SATA")

        used_bytes = total_bytes = 0
        for mount in partitions_by_disk.get(name, []):
            try:
                usage = psutil.disk_usage(mount)
            except OSError:
                continue
            used_bytes += usage.used
            total_bytes += usage.total

        disks.append({
            "device": name,
            "model": model,
            "kind": kind,
            "interface": interface,
            "size_bytes": size_bytes,
            "used_bytes": used_bytes,
            "total_bytes": total_bytes or size_bytes,
            "temp_c": _read_disk_temp_c(dev_path),
            "mountpoints": partitions_by_disk.get(name, []),
        })
    return disks


def disk_usage_for_mounts(mountpoints: list[str]) -> tuple[int, int]:
    """Sum (used_bytes, total_bytes) across the given mountpoints."""
    used = total = 0
    for mount in mountpoints:
        try:
            usage = psutil.disk_usage(mount)
        except OSError:
            continue
        used += usage.used
        total += usage.total
    return used, total


def disk_io_counters_per_device() -> dict[str, dict]:
    """Per-physical-disk IO counters (read/write bytes + busy time for activity %)."""
    try:
        counters = psutil.disk_io_counters(perdisk=True)
    except OSError:
        return {}
    return {
        name: {
            "read_bytes": c.read_bytes,
            "write_bytes": c.write_bytes,
            "busy_time_ms": getattr(c, "busy_time", None),
        }
        for name, c in counters.items()
    }


def get_primary_network_info() -> dict | None:
    """Pick the busiest non-virtual, up network interface and report its details."""
    ignore_prefixes = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap")
    try:
        io_counters = psutil.net_io_counters(pernic=True)
        if_stats = psutil.net_if_stats()
        if_addrs = psutil.net_if_addrs()
    except OSError:
        return None

    candidates = [
        name for name in io_counters
        if not name.startswith(ignore_prefixes)
        and if_stats.get(name) and if_stats[name].isup
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda n: io_counters[n].bytes_sent + io_counters[n].bytes_recv)

    ipv4 = None
    for addr in if_addrs.get(best, []):
        if addr.family == socket.AF_INET:
            ipv4 = addr.address
            break

    speed = if_stats[best].speed
    return {"name": best, "speed_mbps": speed if speed > 0 else None, "ipv4": ipv4}


def _read_number(path: str) -> float | None:
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _sensor_value(filename: str, value: float) -> tuple[float, str]:
    if filename.startswith("temp"):
        return value / 1000, "°C"
    if filename.startswith("fan"):
        return value, "RPM"
    if filename.startswith("in"):
        return value / 1000, "V"
    if filename.startswith("curr"):
        return value / 1000, "A"
    if filename.startswith("power"):
        return value / 1_000_000, "W"
    if filename.startswith("energy"):
        return value / 1_000_000, "J"
    if filename.startswith("humidity"):
        return value / 1000, "%"
    return value, ""


def list_hardware_sensors() -> list[dict[str, str | float]]:
    """Read every available Linux hardware sensor endpoint without root access."""
    sensors: list[dict[str, str | float]] = []

    for hwmon_path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = _read_sysfs_str(f"{hwmon_path}/name") or os.path.basename(hwmon_path)
        for input_path in sorted(glob.glob(f"{hwmon_path}/*_input")):
            value = _read_number(input_path)
            if value is None:
                continue
            filename = os.path.basename(input_path)
            prefix = filename.removesuffix("_input")
            label = _read_sysfs_str(f"{hwmon_path}/{prefix}_label") or prefix
            scaled, unit = _sensor_value(prefix, value)
            sensors.append({
                "category": "Hardware-Monitor", "name": f"{chip}: {label}",
                "value": scaled, "unit": unit, "source": input_path,
            })

    for zone_path in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        value = _read_number(f"{zone_path}/temp")
        if value is None:
            continue
        zone_type = _read_sysfs_str(f"{zone_path}/type") or os.path.basename(zone_path)
        sensors.append({
            "category": "Thermal-Zone", "name": zone_type,
            "value": value / 1000, "unit": "°C", "source": f"{zone_path}/temp",
        })

    for energy_path in sorted(glob.glob("/sys/class/powercap/*/energy_uj")):
        value = _read_number(energy_path)
        if value is None:
            continue
        zone = os.path.dirname(energy_path)
        name = _read_sysfs_str(f"{zone}/name") or os.path.basename(zone)
        sensors.append({
            "category": "Energie", "name": name,
            "value": value / 1_000_000, "unit": "J", "source": energy_path,
        })

    for supply_path in sorted(glob.glob("/sys/class/power_supply/*")):
        supply_type = _read_sysfs_str(f"{supply_path}/type") or "Stromversorgung"
        for filename in ("capacity", "voltage_now", "current_now", "power_now", "temp"):
            path = f"{supply_path}/{filename}"
            value = _read_number(path)
            if value is None:
                continue
            if filename == "capacity":
                scaled, unit = value, "%"
            elif filename == "voltage_now":
                scaled, unit = value / 1_000_000, "V"
            elif filename == "current_now":
                scaled, unit = value / 1_000_000, "A"
            elif filename == "power_now":
                scaled, unit = value / 1_000_000, "W"
            else:
                scaled, unit = value / 10, "°C"
            sensors.append({
                "category": supply_type, "name": f"{os.path.basename(supply_path)}: {filename}",
                "value": scaled, "unit": unit, "source": path,
            })

    return sensors


@dataclass
class ProcessRow:
    pid: int
    name: str
    username: str
    status: str
    cpu_percent: float
    memory_mb: float
    num_threads: int
    gpu_percent: float | None = None  # None = no supported GPU monitoring backend found
    gpu_vram_mb: float | None = None


class ProcessWorker(QThread):
    """Polls all processes in a background thread and emits ready-to-display rows."""

    rowsReady = Signal(list)

    def __init__(self, interval: float = 1.0, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._running = True
        self._procs: dict[int, psutil.Process] = {}
        self._gpu_monitor = GpuMonitor()

    def stop(self) -> None:
        self._running = False
        self.wait(200)
        if self.isRunning():
            self.terminate()
            self.wait(200)

    def run(self) -> None:
        # Prime cpu_percent() for every currently running process so the first
        # real reading is accurate instead of blocking. A short warm-up delay
        # (well under the refresh interval) lets the first real values show up
        # almost immediately instead of waiting a full interval for nothing.
        self._sync_process_table()
        for proc in self._procs.values():
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(0.25)

        while self._running:
            self._sync_process_table()
            rows = self._collect_rows()
            self.rowsReady.emit(rows)
            time.sleep(self._interval)

    def _sync_process_table(self) -> None:
        current_pids = set(psutil.pids())
        known_pids = set(self._procs.keys())

        for pid in known_pids - current_pids:
            self._procs.pop(pid, None)

        for pid in current_pids - known_pids:
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(interval=None)  # prime, non-blocking
                self._procs[pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _collect_rows(self) -> list[ProcessRow]:
        gpu_usage = self._gpu_monitor.poll()
        has_gpu_backend = self._gpu_monitor.available()
        rows: list[ProcessRow] = []
        for pid, proc in list(self._procs.items()):
            try:
                with proc.oneshot():
                    name = proc.name()
                    username = proc.username() if proc.is_running() else ""
                    status = proc.status()
                    cpu = proc.cpu_percent(interval=None)
                    mem = proc.memory_info().rss / (1024 * 1024)
                    threads = proc.num_threads()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._procs.pop(pid, None)
                continue
            gpu_info = gpu_usage.get(pid)
            rows.append(
                ProcessRow(
                    pid=pid,
                    name=name,
                    username=username,
                    status=status,
                    cpu_percent=cpu,
                    memory_mb=mem,
                    num_threads=threads,
                    gpu_percent=(gpu_info or {}).get("gpu_percent", 0.0) if has_gpu_backend else None,
                    gpu_vram_mb=(gpu_info or {}).get("vram_mb", 0.0) if has_gpu_backend else None,
                )
            )
        return rows


def system_snapshot() -> dict:
    """Cheap aggregate system stats safe to call directly on the UI thread."""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    disk_io = psutil.disk_io_counters()
    net = psutil.net_io_counters()
    freq = psutil.cpu_freq()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_per_core": psutil.cpu_percent(interval=None, percpu=True),
        "cpu_freq_mhz": freq.current if freq else None,
        "cpu_freq_max_mhz": freq.max if freq else None,
        "cpu_cores_physical": psutil.cpu_count(logical=False) or 0,
        "cpu_cores_logical": psutil.cpu_count(logical=True) or 0,
        "process_count": len(psutil.pids()),
        "uptime_seconds": time.time() - psutil.boot_time(),
        "mem_percent": vm.percent,
        "mem_used_gb": (vm.total - vm.available) / (1024**3),
        "mem_available_gb": vm.available / (1024**3),
        "mem_total_gb": vm.total / (1024**3),
        "mem_cached_gb": getattr(vm, "cached", 0) / (1024**3),
        "disk_percent": disk.percent,
        "disk_used_gb": disk.used / (1024**3),
        "disk_total_gb": disk.total / (1024**3),
        "disk_read_bytes": disk_io.read_bytes if disk_io else 0,
        "disk_write_bytes": disk_io.write_bytes if disk_io else 0,
        "net_sent": net.bytes_sent,
        "net_recv": net.bytes_recv,
    }


@dataclass
class DockerContainerRow:
    id: str
    name: str
    image: str
    status: str
    running: bool
    cpu_percent: float
    memory_mb: float
    memory_limit_mb: float
    memory_percent: float
    gpu_percent: float | None = None  # None = no supported GPU monitoring backend found
    gpu_vram_mb: float | None = None


_MEM_UNIT_RE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)")
_MEM_UNIT_FACTORS_MB = {
    "B": 1 / (1024 * 1024), "KB": 1 / 1024, "KiB": 1 / 1024,
    "MB": 1, "MiB": 1, "GB": 1024, "GiB": 1024, "TB": 1024 * 1024, "TiB": 1024 * 1024,
}


def _parse_mem_to_mb(value: str) -> float:
    match = _MEM_UNIT_RE.match(value.strip())
    if not match:
        return 0.0
    number, unit = match.groups()
    return float(number) * _MEM_UNIT_FACTORS_MB.get(unit, 1.0)


class DockerWorker(QThread):
    """Polls `docker ps` / `docker stats` in a background thread so the Docker tab
    never blocks the UI. Emits `unavailable` once if the docker CLI is missing or
    the daemon can't be reached (e.g. no permission / daemon not running)."""

    rowsReady = Signal(list)
    unavailable = Signal(str)

    def __init__(self, interval: float = 2.0, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._running = True
        self._docker = shutil.which("docker")
        self._gpu_monitor = GpuMonitor()

    def stop(self) -> None:
        self._running = False
        self.wait(200)
        if self.isRunning():
            self.terminate()
            self.wait(200)

    def run(self) -> None:
        if not self._docker:
            self.unavailable.emit("Docker ist nicht installiert oder nicht im PATH.")
            return
        while self._running:
            rows, error = self._collect_rows()
            if error:
                self.unavailable.emit(error)
                return
            self.rowsReady.emit(rows)
            for _ in range(max(1, int(self._interval * 10))):
                if not self._running:
                    return
                time.sleep(0.1)

    def _collect_rows(self) -> tuple[list[DockerContainerRow], str]:
        try:
            ps_out = subprocess.check_output(
                [self._docker, "ps", "-a", "--format", "{{json .}}"],
                text=True, timeout=5, stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError as exc:
            return [], (exc.output or str(exc)).strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            return [], str(exc)

        containers = [json.loads(line) for line in ps_out.splitlines() if line.strip()]

        stats: dict[str, dict] = {}
        try:
            stats_out = subprocess.check_output(
                [self._docker, "stats", "--no-stream", "--format", "{{json .}}"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            )
            for line in stats_out.splitlines():
                if line.strip():
                    entry = json.loads(line)
                    stats[entry["ID"]] = entry
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass  # no running containers, or `docker stats` unsupported - CPU/Mem stay 0

        has_gpu_backend = self._gpu_monitor.available()
        gpu_usage_by_pid = self._gpu_monitor.poll() if has_gpu_backend else {}

        rows: list[DockerContainerRow] = []
        for container in containers:
            cid = container.get("ID", "")
            status_text = container.get("Status", "")
            running = status_text.lower().startswith("up")
            stat = stats.get(cid)
            cpu = mem_mb = mem_limit_mb = mem_percent = 0.0
            if stat:
                cpu = float((stat.get("CPUPerc") or "0%").rstrip("%") or 0)
                mem_percent = float((stat.get("MemPerc") or "0%").rstrip("%") or 0)
                usage_parts = (stat.get("MemUsage") or "0B / 0B").split("/")
                if len(usage_parts) == 2:
                    mem_mb = _parse_mem_to_mb(usage_parts[0])
                    mem_limit_mb = _parse_mem_to_mb(usage_parts[1])

            gpu_percent = gpu_vram_mb = None
            if has_gpu_backend and running:
                gpu_percent = gpu_vram_mb = 0.0
                for pid in self._container_pids(cid):
                    info = gpu_usage_by_pid.get(pid)
                    if info:
                        gpu_percent += info.get("gpu_percent", 0.0)
                        gpu_vram_mb += info.get("vram_mb", 0.0)

            rows.append(
                DockerContainerRow(
                    id=cid,
                    name=container.get("Names", cid),
                    image=container.get("Image", ""),
                    status=status_text,
                    running=running,
                    cpu_percent=cpu,
                    memory_mb=mem_mb,
                    memory_limit_mb=mem_limit_mb,
                    memory_percent=mem_percent,
                    gpu_percent=gpu_percent,
                    gpu_vram_mb=gpu_vram_mb,
                )
            )
        return rows, ""

    def _container_pids(self, container_id: str) -> list[int]:
        """Host-side PIDs of processes running inside a container, via `docker top`."""
        try:
            out = subprocess.check_output(
                [self._docker, "top", container_id],
                text=True, timeout=3, stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return []
        lines = out.splitlines()
        if len(lines) < 2:
            return []
        header = lines[0].split()
        try:
            pid_col = header.index("PID")
        except ValueError:
            return []
        pids = []
        for line in lines[1:]:
            parts = line.split()
            if pid_col < len(parts):
                try:
                    pids.append(int(parts[pid_col]))
                except ValueError:
                    continue
        return pids


def docker_container_action(container_id: str, action: str) -> tuple[bool, str]:
    """Run a docker lifecycle action ('start', 'stop', 'restart' or 'rm') on a container."""
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker ist nicht installiert."
    cmd = [docker, "rm", "-f", container_id] if action == "rm" else [docker, action, container_id]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or str(exc)).strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)


def kill_process(pid: int) -> tuple[bool, str]:
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except psutil.TimeoutExpired:
            proc.kill()
        return True, ""
    except psutil.NoSuchProcess:
        return True, ""
    except psutil.AccessDenied:
        return False, "Zugriff verweigert (Berechtigung erforderlich)"
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)


_AUTOSTART_USER_DIR = os.path.expanduser("~/.config/autostart")


def _autostart_system_dirs() -> list[str]:
    xdg_dirs = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")
    return [os.path.join(d, "autostart") for d in xdg_dirs if d]


@dataclass
class AutostartEntry:
    filename: str
    name: str
    command: str
    comment: str
    enabled: bool
    source: str  # "Benutzer" or "System"
    path: str


def _parse_desktop_entry(path: str) -> dict[str, str]:
    """Minimal .desktop [Desktop Entry] key=value reader (freedesktop autostart spec)."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            in_section = False
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("["):
                    in_section = line == "[Desktop Entry]"
                    continue
                if in_section and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def list_autostart_entries() -> list[AutostartEntry]:
    """Merge system (/etc/xdg/autostart, $XDG_CONFIG_DIRS) and user (~/.config/autostart)
    autostart entries; a user file with the same name overrides the system one, per spec."""
    candidates: dict[str, tuple[str, str]] = {}
    for directory in _autostart_system_dirs():
        try:
            for fn in os.listdir(directory):
                if fn.endswith(".desktop"):
                    candidates[fn] = (os.path.join(directory, fn), "System")
        except OSError:
            continue
    try:
        for fn in os.listdir(_AUTOSTART_USER_DIR):
            if fn.endswith(".desktop"):
                candidates[fn] = (os.path.join(_AUTOSTART_USER_DIR, fn), "Benutzer")
    except OSError:
        pass

    entries: list[AutostartEntry] = []
    for filename, (path, source) in candidates.items():
        values = _parse_desktop_entry(path)
        hidden = values.get("Hidden", "false").lower() == "true"
        gnome_enabled = values.get("X-GNOME-Autostart-enabled", "true").lower() != "false"
        entries.append(
            AutostartEntry(
                filename=filename,
                name=values.get("Name", filename.removesuffix(".desktop")),
                command=values.get("Exec", ""),
                comment=values.get("Comment", ""),
                enabled=not hidden and gnome_enabled,
                source=source,
                path=path,
            )
        )
    entries.sort(key=lambda e: e.name.lower())
    return entries


def set_autostart_enabled(entry: AutostartEntry, enabled: bool) -> tuple[bool, str]:
    """Enable/disable an autostart entry. System entries are never edited directly;
    instead a per-user override copy is created in ~/.config/autostart (XDG spec)."""
    try:
        os.makedirs(_AUTOSTART_USER_DIR, exist_ok=True)
        target_path = entry.path
        if entry.source == "System":
            target_path = os.path.join(_AUTOSTART_USER_DIR, entry.filename)
            if not os.path.exists(target_path):
                shutil.copyfile(entry.path, target_path)

        with open(target_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        new_lines = []
        in_section = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                in_section = stripped == "[Desktop Entry]"
                new_lines.append(line)
                continue
            if in_section and stripped.startswith(("Hidden=", "X-GNOME-Autostart-enabled=")):
                continue  # dropped; the flag below is the single source of truth
            new_lines.append(line)

        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"X-GNOME-Autostart-enabled={'true' if enabled else 'false'}\n")

        with open(target_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return True, ""
    except OSError as exc:
        return False, str(exc)
