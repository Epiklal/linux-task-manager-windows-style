"""psutil helpers. All per-process CPU sampling is non-blocking (interval=None)
so the process list never stalls the UI thread, no matter how many processes exist.
"""
from __future__ import annotations

import glob
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

import psutil
from PySide6.QtCore import QThread, Signal

from app.hardware_setup import helper_path
from app.i18n import N_

# A leftover nvidia-smi is useless while nouveau drives the card (no NVIDIA module).
_NVIDIA_SMI = (shutil.which("nvidia-smi")
               if os.path.isdir("/proc/driver/nvidia") or not os.path.isdir("/sys/module/nouveau")
               else None)

_DRM_CLIENT_ID_RE = re.compile(r"^drm-client-id:\s+(\d+)")
_DRM_VRAM_RE = re.compile(r"^drm-memory-vram:\s+(\d+)\s+KiB")
_DRM_ENGINE_RE = re.compile(r"^drm-engine-([\w-]+):\s+(\d+)\s+ns")
# amdgpu: gfx/compute/sdma/enc/dec/jpeg; i915 (Intel): render/copy/video/video-enhance.
_GPU_ENGINE_LABELS = {
    "gfx": "3D", "render": "3D", "compute": "Compute", "sdma": N_("Copy"), "dma": N_("Copy"),
    "copy": N_("Copy"), "enc": N_("Video Encode"), "dec": N_("Video Decode"),
    "video": N_("Video Decode"), "video-enhance": N_("Video Processing"), "jpeg": "JPEG",
}


def _pcie_link_info(current_speed: str, current_width: str,
                    max_speed: str, max_width: str) -> dict:
    """Normalize PCIe link values and calculate theoretical link capacity."""
    speed_re = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*GT/s", re.IGNORECASE)

    def parse_speed(value: str) -> float | None:
        match = speed_re.search(value)
        return float(match.group(1)) if match else None

    def parse_width(value: str) -> int | None:
        match = re.search(r"\d+", value)
        return int(match.group()) if match else None

    current_gt = parse_speed(current_speed)
    max_gt = parse_speed(max_speed)
    current_lanes = parse_width(current_width)
    max_lanes = parse_width(max_width)
    effective_gt = current_gt or max_gt
    effective_lanes = current_lanes or max_lanes
    bandwidth_gb_s = None
    if effective_gt is not None and effective_lanes is not None:
        efficiency = 0.8 if effective_gt <= 5 else 128 / 130
        bandwidth_gb_s = effective_gt * efficiency * effective_lanes / 8
    return {
        "generation": _pcie_generation(effective_gt),
        "current_speed_gt_s": current_gt,
        "current_width": current_lanes,
        "max_speed_gt_s": max_gt,
        "max_generation": _pcie_generation(max_gt),
        "max_width": max_lanes,
        "bandwidth_gb_s": bandwidth_gb_s,
    }


def _pcie_generation(speed_gt_s: float | None) -> str | None:
    generations = {2.5: "1", 5.0: "2", 8.0: "3", 16.0: "4", 32.0: "5", 64.0: "6"}
    if speed_gt_s is None:
        return None
    closest = min(generations, key=lambda speed: abs(speed - speed_gt_s))
    return f"Gen {generations[closest]}"


def _nvidia_rate_mb_s(value: str) -> float | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
    return float(match.group(1)) if match else None


# These names key the chart history, in the snapshot too: English, translated for display.
GPU_ENGINE_METRICS = ["3D", "Compute", N_("Copy"), N_("Video Encode"), N_("Video Decode"),
                      "JPEG", N_("Memory Controller")]


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
                    if engine_match and engine_match.group(1) in ("gfx", "render", "compute"):
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

        # Forget exited processes, or the dict keeps every PID that ever used the GPU.
        self._prev_busy = {pid: v for pid, v in self._prev_busy.items() if pid in result}
        return result


def _pci_location_text(address: str) -> str:
    """'0000:2d:00.0' -> 'PCI bus 45, device 0, function 0' (as Windows words it)."""
    match = re.search(r"([0-9a-fA-F]+):([0-9a-fA-F]+)\.([0-7])$", address.strip())
    if not match:
        return address
    bus, device, function = (int(part, 16) for part in match.groups())
    return f"PCI bus {bus}, device {device}, function {function}"


_GPU_VENDORS = {"Advanced Micro Devices": "AMD", "NVIDIA": "NVIDIA", "Intel": "Intel"}


def _lspci_gpu_name(slot: str) -> str | None:
    """Marketing name such as 'AMD Radeon RX 7900 XTX' from `lspci -mm`.

    lspci lists a device family ('Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M');
    the board's subsystem name usually tells which variant it is.
    """
    try:
        out = subprocess.check_output(["lspci", "-mm", "-s", slot], text=True, timeout=2,
                                      stderr=subprocess.DEVNULL)
        fields = shlex.split(out.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    positional = [f for f in fields if not f.startswith("-")]
    if len(positional) < 4:
        return None
    vendor_text, device_text = positional[2], positional[3]
    subsystem = positional[5] if len(positional) > 5 else ""
    vendor = next((short for key, short in _GPU_VENDORS.items() if key in vendor_text), "")
    bracket = re.search(r"\[([^\]]+)\]\s*$", device_text)
    family = bracket.group(1) if bracket else device_text
    variants = family.split("/")
    model = variants[0]
    if len(variants) > 1:
        # 'Radeon RX 7900 XT' + '7900 XTX' -> prefix 'Radeon RX', candidates per variant.
        tokens = variants[0].split()
        alt_len = len(variants[1].split())
        prefix = " ".join(tokens[:-alt_len]) if len(tokens) > alt_len else ""
        suffixes = [" ".join(tokens[-alt_len:]), *variants[1:]]
        matching = [v for v in suffixes if re.search(rf"\b{re.escape(v)}\b", subsystem)]
        if matching:
            model = f"{prefix} {max(matching, key=len)}".strip()
    return f"{vendor} {model}".strip()


RAPL_PERMISSION_HINT = N_("No read permission (Settings → Set up hardware data)")
_POWERCAP_BASE = "/sys/class/powercap"
# Newer Intel CPUs expose the same package counters via MSR, MMIO and TPMI; the
# MSR interface comes first so a package is not counted twice.
_RAPL_INTERFACES = ("intel-rapl", "intel-rapl-mmio", "intel-rapl-tpmi")


class _EnergyCounter:
    """Watts from a cumulative energy counter in µJ (RAPL energy_uj, hwmon energyN_input)."""

    def __init__(self, path: str, wrap_uj: int = 0):
        self.path = path
        self._wrap_uj = wrap_uj  # RAPL counters restart at max_energy_range_uj
        self._prev: tuple[float, int] | None = None
        self.permission_denied = False

    def poll_watts(self) -> float | None:
        """Average power since the previous call; None on the first call."""
        try:
            with open(self.path) as f:
                energy_uj = int(f.read().strip())
        except PermissionError:
            # RAPL energy_uj is root-only since Linux 5.10 (PLATYPUS side channel).
            self.permission_denied = True
            return None
        except (OSError, ValueError):
            return None
        self.permission_denied = False
        now = time.monotonic()
        prev, self._prev = self._prev, (now, energy_uj)
        if prev is None or now <= prev[0]:
            return None
        delta = energy_uj - prev[1]
        if delta < 0:  # the counter wrapped around
            if not self._wrap_uj:
                return None
            delta += self._wrap_uj
        return delta / 1_000_000 / (now - prev[0])


def _rapl_counters(kind: str, base: str = _POWERCAP_BASE) -> list[_EnergyCounter]:
    """One counter per socket for the RAPL zones named `kind` ("package", "uncore", …).

    Intel and AMD Zen both use the intel-rapl powercap interface. Top-level zones
    are package-N (or psys, the whole laptop platform on Skylake+, which is never
    added on top); sub-zones like intel-rapl:0:1 are core, uncore (Intel
    integrated graphics) and dram.
    """
    zones: dict[tuple[str | None, str], str] = {}
    for interface in _RAPL_INTERFACES:
        for zone_dir in sorted(glob.glob(f"{base}/{interface}:*")):
            name = _read_sysfs_str(f"{zone_dir}/name") or ""
            if not (name == kind or name.startswith(f"{kind}-")):
                continue
            if not os.path.exists(f"{zone_dir}/energy_uj"):
                continue
            package_dir = zone_dir.rsplit(":", 1)[0] if zone_dir.count(":") > 1 else zone_dir
            zones.setdefault((_read_sysfs_str(f"{package_dir}/name"), name), zone_dir)
    counters = []
    for zone_dir in zones.values():
        wrap = _read_sysfs_str(f"{zone_dir}/max_energy_range_uj") or ""
        counters.append(_EnergyCounter(f"{zone_dir}/energy_uj", int(wrap) if wrap.isdigit() else 0))
    return counters


def _nvidia_float(value: str) -> float | None:
    """nvidia-smi prints "[N/A]" / "[Not Supported]" for values old GeForce cards lack."""
    try:
        return float(value)
    except ValueError:
        return None


class GpuSystemMonitor:
    """Best-effort overall (not per-process) GPU stats: utilization/VRAM/temp/power.

    Uses `nvidia-smi` if present, else the AMD/Intel/nouveau sysfs interface under
    /sys/class/drm/cardN/device (gpu_busy_percent, mem_info_vram_*, hwmon
    temp/power). Cheap enough (plain file reads) to poll from the UI thread.
    """

    def __init__(self):
        self._nvidia = _NVIDIA_SMI
        self._card_path = None if self._nvidia else self._find_card()
        self._power_counter = self._find_power_counter() if self._card_path else None
        self._prev_engine_busy: dict[str, int] = {}
        self._prev_engine_time: float | None = None
        # Name / driver / PCI location never change, so they are read only once.
        self._static_info = (self._nvidia_static_info() if self._nvidia
                             else self._sysfs_static_info() if self._card_path else {})

    def available(self) -> bool:
        return bool(self._nvidia or self._card_path)

    @staticmethod
    def _find_card() -> str | None:
        base = "/sys/class/drm"
        candidates = []
        try:
            for name in sorted(os.listdir(base)):
                if not re.match(r"^card\d+$", name):
                    continue
                device = f"{base}/{name}/device"
                if not os.path.exists(f"{device}/uevent"):
                    continue
                boot_vga = _read_sysfs_str(f"{device}/boot_vga") == "1"
                has_vram = os.path.exists(f"{device}/mem_info_vram_total")
                has_busy = os.path.exists(f"{device}/gpu_busy_percent")
                vendor = _read_sysfs_str(f"{device}/vendor") or ""
                score = (not boot_vga, has_vram, has_busy, vendor == "0x10de")
                candidates.append((score, device))
        except OSError:
            pass
        return max(candidates, default=((False, False, False, False), None))[1]

    def _find_power_counter(self) -> _EnergyCounter | None:
        """Energy counter for GPUs without a power sensor: Intel Arc (i915/xe hwmon
        energy1_input) or Intel integrated graphics (RAPL uncore zone)."""
        for energy_path in sorted(glob.glob(f"{self._card_path}/hwmon/hwmon*/energy1_input")):
            return _EnergyCounter(energy_path)
        is_intel_igpu = (_read_sysfs_str(f"{self._card_path}/vendor") == "0x8086"
                         and os.path.basename(os.path.realpath(self._card_path)) == "0000:00:02.0")
        uncore = _rapl_counters("uncore") if is_intel_igpu else []
        return uncore[0] if len(uncore) == 1 else None

    def poll(self) -> dict | None:
        if self._nvidia:
            result = self._poll_nvidia()
        elif self._card_path:
            result = self._poll_sysfs()
            if result is not None:
                result["engines"] = self._poll_sysfs_engines()
                if result["percent"] is None:
                    # i915/xe/nouveau have no gpu_busy_percent: use the busiest engine.
                    result["percent"] = min(100.0, max(result["engines"].values(), default=0.0))
        else:
            return None
        return (result | self._static_info) if result is not None else None

    def _nvidia_static_info(self) -> dict:
        try:
            out = subprocess.check_output(
                [self._nvidia, "--query-gpu=driver_version,pci.bus_id",
                 "--format=csv,noheader"],
                text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
            version, bus_id = (part.strip() for part in out.splitlines()[0].split(","))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
                IndexError, ValueError):
            return {}
        return {"driver": "nvidia", "driver_version": version,
                "pci_location": _pci_location_text(bus_id)}

    def _sysfs_static_info(self) -> dict:
        try:
            with open(f"{self._card_path}/uevent") as f:
                uevent = f.read()
        except OSError:
            return {}
        driver = re.search(r"DRIVER=(\w+)", uevent)
        slot = re.search(r"PCI_SLOT_NAME=(\S+)", uevent)
        driver_name = driver.group(1) if driver else ""
        info = {"driver": driver_name}
        version = _read_sysfs_str(f"/sys/module/{driver_name}/version") if driver_name else None
        # In-tree drivers (amdgpu, i915, nouveau) have no own version: they ship with the kernel.
        info["driver_version"] = version or f"Kernel {platform.release()}"
        if slot:
            info["pci_location"] = _pci_location_text(slot.group(1))
            name = _lspci_gpu_name(slot.group(1))
            if name:
                info["name"] = name
        return info

    def _poll_nvidia(self) -> dict | None:
        try:
            out = subprocess.check_output(
                [self._nvidia,
                 "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                 "temperature.gpu,power.draw,pcie.link.gen.current,"
                 "pcie.link.width.current,pcie.link.gen.max,pcie.link.width.max,"
                 "pcie.tx_util,pcie.rx_util",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
            fields = [part.strip() for part in out.splitlines()[0].split(",")]
            name, util, mem_used, mem_total, temp, power = fields[:6]
            pcie_gen, pcie_width, max_gen, max_width, tx_util, rx_util = fields[6:]
            result = {
                "name": name,
                "percent": _nvidia_float(util) or 0.0,
                "vram_used_mb": _nvidia_float(mem_used),
                "vram_total_mb": _nvidia_float(mem_total),
                "temp_c": _nvidia_float(temp),
                "power_w": _nvidia_float(power),
                "pcie": _pcie_link_info(pcie_gen, pcie_width, max_gen, max_width) | {
                    "tx_mb_s": _nvidia_rate_mb_s(tx_util),
                    "rx_mb_s": _nvidia_rate_mb_s(rx_util),
                },
            }
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
                IndexError, ValueError):
            return self._poll_nvidia_legacy()
        result["engines"] = self._poll_nvidia_engines()
        return result

    def _poll_nvidia_legacy(self) -> dict | None:
        """Keep older NVIDIA drivers usable when PCIe query fields are absent."""
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
                "percent": _nvidia_float(util) or 0.0,
                "vram_used_mb": _nvidia_float(mem_used),
                "vram_total_mb": _nvidia_float(mem_total),
                "temp_c": _nvidia_float(temp),
                "power_w": _nvidia_float(power),
                "pcie": {},
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
                    "3D": float(sm), "Memory Controller": float(mem),
                    "Video Encode": float(enc), "Video Decode": float(dec),
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
        percent = self._read_float(f"{self._card_path}/gpu_busy_percent")
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
        power_reason = None
        if power_w is None and self._power_counter is not None:
            power_w = self._power_counter.poll_watts()
            if self._power_counter.permission_denied:
                power_reason = RAPL_PERMISSION_HINT
        return {
            "name": self._read_name(),
            "percent": percent,
            "power_reason": power_reason,
            "vram_used_mb": (vram_used / (1024 * 1024)) if vram_used is not None else None,
            "vram_total_mb": (vram_total / (1024 * 1024)) if vram_total is not None else None,
            "gtt_used_mb": (gtt_used / (1024 * 1024)) if gtt_used is not None else None,
            "gtt_total_mb": (gtt_total / (1024 * 1024)) if gtt_total is not None else None,
            "temp_c": temp_c,
            "power_w": power_w,
            "pcie": _pcie_link_info(
                _read_sysfs_str(f"{self._card_path}/current_link_speed") or "",
                _read_sysfs_str(f"{self._card_path}/current_link_width") or "",
                _read_sysfs_str(f"{self._card_path}/max_link_speed") or "",
                _read_sysfs_str(f"{self._card_path}/max_link_width") or "",
            ),
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


class WorkerStopped(subprocess.SubprocessError):
    """Raised by StoppableWorker._run_command once stop() has been requested."""


def _run_capture(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)


class StoppableWorker(QThread):
    """QThread whose stop() never uses terminate().

    Cancelling a thread that runs Python code can leave the GIL or allocator locks
    held (that crashed the app on close). Instead run() checks `_stop_event`, and
    stop() ends the external command the thread is waiting on (see _run_command)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._child_lock = threading.Lock()
        self._child: subprocess.Popen | None = None

    def stop(self) -> None:
        self._stop_event.set()
        # SIGTERM first: sudo forwards it to its root child, SIGKILL would orphan it.
        self._signal_child(kill=False)
        if not self.wait(3000):
            self._signal_child(kill=True)
            self.wait()

    def _signal_child(self, kill: bool) -> None:
        with self._child_lock:
            if self._child is None:
                return
            try:
                if kill:
                    self._child.kill()
                else:
                    self._child.terminate()
            except OSError:
                pass

    def _run_command(self, cmd: list[str], timeout: float, *, stderr=subprocess.PIPE,
                     check: bool = False) -> subprocess.CompletedProcess:
        """subprocess.run() that stop() can interrupt; raises WorkerStopped then.

        Also works on a worker that was never started (the remote server uses that)."""
        with self._child_lock:
            if self._stop_event.is_set():
                raise WorkerStopped(cmd)
            child = self._child = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                text=True, encoding="utf-8", errors="replace",
            )
        try:
            try:
                stdout, error_output = child.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise
        finally:
            with self._child_lock:
                self._child = None
        if self._stop_event.is_set():
            raise WorkerStopped(cmd)
        if check and child.returncode:
            raise subprocess.CalledProcessError(child.returncode, cmd, stdout, error_output)
        return subprocess.CompletedProcess(cmd, child.returncode, stdout, error_output)


class GpuSystemWorker(StoppableWorker):
    """Poll overall GPU statistics without blocking the Qt UI thread."""

    snapshotReady = Signal(object)

    def __init__(self, interval: float = 0.5, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._monitor = GpuSystemMonitor()

    def run(self) -> None:
        while not self._stop_event.is_set():
            snapshot = self._monitor.poll()
            if self._stop_event.is_set():
                return
            self.snapshotReady.emit(snapshot)
            self._stop_event.wait(self._interval)


class CpuPowerMonitor:
    """Best-effort CPU package power draw via Linux RAPL (/sys/class/powercap).

    Works on Intel (Sandy Bridge and newer) and AMD Zen, on every distro, as long
    as energy_uj is readable (root-only since Linux 5.10, see hardware_setup).
    The zenpower hwmon driver is only a fallback, so nothing is counted twice.
    Returns None when unavailable so the UI can show a reason instead of 0.
    """

    def __init__(self):
        self._counters = _rapl_counters("package")
        self._hwmon_power_paths = [] if self._counters else self._find_hwmon_power_paths()

    def available(self) -> bool:
        return bool(self._counters or self._hwmon_power_paths)

    def unavailable_reason(self) -> str:
        if any(counter.permission_denied for counter in self._counters):
            return RAPL_PERMISSION_HINT
        if self.available():
            return N_("Measuring …")  # RAPL needs two samples for a rate
        return N_("The kernel provides no CPU power sensor")

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
        # Poll every counter, so each one keeps its previous sample up to date.
        readings = [counter.poll_watts() for counter in self._counters]
        watts = [value for value in readings if value is not None]
        if watts:
            return sum(watts)
        total_watts = 0.0
        got_any = False
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
    return N_("Unknown CPU")


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


def read_dmi_memory() -> str | None:
    """Output of "dmidecode -t memory" via the restricted hardware helper when available."""
    commands = []
    helper = helper_path()
    if os.path.exists(helper):
        commands.append(["sudo", "-n", helper, "memory"])
    dmidecode = shutil.which("dmidecode")
    if dmidecode:
        commands.append([dmidecode, "-t", "memory"])
    for command in commands:
        try:
            return subprocess.check_output(
                command, text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def get_ram_speed_mhz() -> int | None:
    """Configured DIMM speed via the restricted hardware helper when available."""
    out = read_dmi_memory()
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


def _is_ram_chip(chip: str) -> bool:
    """Sensor chips that sit on the memory modules."""
    return chip.lower().startswith(("jc42", "spd"))


def get_ram_temperature() -> tuple[float | None, int]:
    """Return average temperature and detected DIMM sensor count (JC42/SPD)."""
    try:
        temperatures = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None, 0
    values = [
        entry.current
        for chip, entries in temperatures.items()
        if _is_ram_chip(chip)
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


def _smartctl_disk_stats(device: str, use_sudo: bool = False, run=None) -> dict:
    """Return best-effort lifetime SMART metrics for one Linux block device.

    `run(cmd, timeout)` executes the commands; DiskSmartWorker passes its
    interruptible _run_command."""
    run = run or _run_capture
    smartctl = shutil.which("smartctl")
    commands = []
    helper = helper_path()
    if os.path.exists(helper):
        commands.append(["sudo", "-n", helper, device])
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
            completed = run(candidate, timeout=12)
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


class DiskSmartWorker(StoppableWorker):
    """Read potentially slow SMART data away from the UI thread."""

    snapshotReady = Signal(dict)

    def __init__(self, devices: list[str], use_sudo: bool = False,
                 interval: float = 60.0, parent=None):
        super().__init__(parent)
        self._devices = devices
        self._use_sudo = use_sudo
        self._interval = interval

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                stats = {
                    device: _smartctl_disk_stats(device, self._use_sudo, run=self._run_command)
                    for device in self._devices
                }
                if self._stop_event.is_set():
                    return
                self.snapshotReady.emit(stats)
                wait_seconds = 5.0 if not shutil.which("smartctl") else self._interval
                self._stop_event.wait(wait_seconds)
        except WorkerStopped:
            pass


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
    if filename.startswith("freq"):  # hwmon reports clocks in Hz (e.g. amdgpu sclk/mclk)
        return value / 1_000_000, "MHz"
    return value, ""


def _hwmon_sensors(ram: bool) -> list[dict[str, str | float]]:
    """The hwmon sensors on the memory modules (ram=True) or on everything else."""
    sensors: list[dict[str, str | float]] = []
    for hwmon_path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = _read_sysfs_str(f"{hwmon_path}/name") or os.path.basename(hwmon_path)
        if _is_ram_chip(chip) != ram:
            continue
        for input_path in sorted(glob.glob(f"{hwmon_path}/*_input")):
            value = _read_number(input_path)
            if value is None:
                continue
            filename = os.path.basename(input_path)
            prefix = filename.removesuffix("_input")
            label = _read_sysfs_str(f"{hwmon_path}/{prefix}_label") or prefix
            scaled, unit = _sensor_value(prefix, value)
            sensors.append({
                "category": N_("Hardware monitor"), "name": f"{chip}: {label}",
                "value": scaled, "unit": unit, "source": input_path,
            })
    return sensors


def list_ram_sensors() -> list[dict[str, str | float]]:
    """The sensors on the memory modules, which the memory tab shows."""
    return _hwmon_sensors(ram=True)


def list_hardware_sensors() -> list[dict[str, str | float]]:
    """Read every Linux hardware sensor endpoint without root access, except the memory modules."""
    sensors = _hwmon_sensors(ram=False)

    for zone_path in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        value = _read_number(f"{zone_path}/temp")
        if value is None:
            continue
        zone_type = _read_sysfs_str(f"{zone_path}/type") or os.path.basename(zone_path)
        sensors.append({
            "category": N_("Thermal zone"), "name": zone_type,
            "value": value / 1000, "unit": "°C", "source": f"{zone_path}/temp",
        })

    for energy_path in sorted(glob.glob("/sys/class/powercap/*/energy_uj")):
        value = _read_number(energy_path)
        if value is None:
            continue
        zone = os.path.dirname(energy_path)
        name = _read_sysfs_str(f"{zone}/name") or os.path.basename(zone)
        sensors.append({
            "category": N_("Energy"), "name": name,
            "value": value / 1_000_000, "unit": "J", "source": energy_path,
        })

    for supply_path in sorted(glob.glob("/sys/class/power_supply/*")):
        supply_type = _read_sysfs_str(f"{supply_path}/type") or N_("Power supply")
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


class ProcessWorker(StoppableWorker):
    """Polls all processes in a background thread and emits ready-to-display rows."""

    rowsReady = Signal(list)

    def __init__(self, interval: float = 1.0, parent=None):
        super().__init__(parent)
        self._interval = interval
        # Back-pressure: the next batch is only collected once the GUI has handled the
        # previous one (mark_consumed). Every queued rowsReady keeps its whole row list
        # alive, so without this a stalled GUI thread lets the queue grow without limit.
        self._consumed = threading.Event()
        self._consumed.set()
        self._procs: dict[int, psutil.Process] = {}
        self._gpu_monitor = GpuMonitor()

    def set_interval(self, seconds: float) -> None:
        self._interval = seconds

    def mark_consumed(self) -> None:
        self._consumed.set()

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
        if self._stop_event.wait(0.25):
            return

        while not self._stop_event.is_set():
            if self._consumed.is_set():
                self._sync_process_table()
                rows = self._collect_rows()
                self._consumed.clear()
                self.rowsReady.emit(rows)
            self._stop_event.wait(self._interval)

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
                    username = proc.username()
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


class DockerWorker(StoppableWorker):
    """Polls `docker ps` / `docker stats` in a background thread so the Docker tab
    never blocks the UI. Emits `unavailable` once if the docker CLI is missing or
    the daemon can't be reached (e.g. no permission / daemon not running)."""

    rowsReady = Signal(list)
    unavailable = Signal(str)

    def __init__(self, interval: float = 2.0, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._docker = shutil.which("docker")
        self._gpu_monitor = GpuMonitor()

    def run(self) -> None:
        if not self._docker:
            self.unavailable.emit(N_("Docker is not installed or not in PATH."))
            return
        try:
            while not self._stop_event.is_set():
                rows, error = self._collect_rows()
                if self._stop_event.is_set():
                    return
                if error:
                    self.unavailable.emit(error)
                    return
                self.rowsReady.emit(rows)
                self._stop_event.wait(max(0.1, self._interval))
        except WorkerStopped:
            pass

    def _collect_rows(self) -> tuple[list[DockerContainerRow], str]:
        if not self._docker:
            return [], N_("Docker is not installed or not in PATH.")
        try:
            ps_out = self._run_command(
                [self._docker, "ps", "-a", "--format", "{{json .}}"],
                timeout=5, stderr=subprocess.STDOUT, check=True,
            ).stdout
        except subprocess.CalledProcessError as exc:
            return [], (exc.output or str(exc)).strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            return [], str(exc)

        containers = [json.loads(line) for line in ps_out.splitlines() if line.strip()]

        stats: dict[str, dict] = {}
        try:
            stats_out = self._run_command(
                [self._docker, "stats", "--no-stream", "--format", "{{json .}}"],
                timeout=5, stderr=subprocess.DEVNULL, check=True,
            ).stdout
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
            out = self._run_command(
                [self._docker, "top", container_id],
                timeout=3, stderr=subprocess.DEVNULL, check=True,
            ).stdout
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
        return False, N_("Docker is not installed.")
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
        return False, N_("Access denied (permission required)")
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
    source: str  # "user" or "system", also in the snapshot; the page words it
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
                    candidates[fn] = (os.path.join(directory, fn), "system")
        except OSError:
            continue
    try:
        for fn in os.listdir(_AUTOSTART_USER_DIR):
            if fn.endswith(".desktop"):
                candidates[fn] = (os.path.join(_AUTOSTART_USER_DIR, fn), "user")
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
    allowed_dirs = [os.path.realpath(d) for d in (_AUTOSTART_USER_DIR, *_autostart_system_dirs())]
    source_dir = os.path.dirname(os.path.realpath(entry.path))
    if (os.path.basename(entry.filename) != entry.filename
            or not entry.filename.endswith(".desktop")
            or source_dir not in allowed_dirs):
        # Never edit a file outside the XDG autostart folders, whatever the entry says.
        return False, N_("Invalid autostart entry")
    try:
        os.makedirs(_AUTOSTART_USER_DIR, exist_ok=True)
        target_path = entry.path
        if entry.source == "system":
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
