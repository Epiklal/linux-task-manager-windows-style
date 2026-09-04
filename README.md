# WinTaskManager

A native Linux system monitor inspired by the Windows Task Manager performance
view. WinTaskManager combines live process management, hardware telemetry,
services, startup entries, Docker containers, and focused hardware benchmarks in
one PySide6 desktop application.

Built for Linux desktops running Wayland or X11.

## Highlights

- **Live processes** - Search, sort, end processes, launch a command or executable,
  and drop an executable file onto the process list to start it.
- **Windows-style grouping** - When sorted by name, processes are grouped into
  applications and background processes. Repeated process names can be expanded or
  collapsed individually.
- **Performance dashboard** - Rolling CPU, per-core CPU, memory, network, GPU,
  and physical disk charts with a dark Windows-inspired visual style.
- **Detailed hardware data** - CPU temperature, frequency, core count, RAM speed
  and DIMM temperature when exposed by the system, disk health data, GPU memory,
  temperature, power draw, and engine utilization where supported.
- **SMART disk telemetry** - Shows drive temperature, power cycles, power-on hours,
  and lifetime read/write totals when the drive and adapter provide those values.
- **Hardware benchmarks** - Run configurable CPU, memory, GPU, and disk read
  benchmarks without freezing the application.
- **System management** - Browse systemd services, Docker containers, sensors, and
  XDG autostart entries.
- **Persistent customization** - Choose a light or dark theme, tune refresh rates,
  save table sizing, and override the interface with guided Qt Style Sheet (QSS)
  templates.

## Linux Requirements

The installer supports the following package managers:

| Distribution family | Package manager |
| --- | --- |
| Debian, Ubuntu, Linux Mint | `apt` |
| Fedora | `dnf` |
| Arch Linux | `pacman` |
| openSUSE | `zypper` |
| Alpine Linux | `apk` |

It installs Python, a virtual environment, and the optional system tools used for
hardware information and benchmarks: `smartmontools`, `lm-sensors`, `dmidecode`,
`sysbench`, `fio`, and `glmark2`.

## Install on Linux

Clone the repository and run the installer:

```bash
git clone https://github.com/Epiklal/wintaskmanager.git
cd wintaskmanager
./install.sh
```

The installer creates a project-local `.venv`, installs the Python dependencies,
and adds **Task-Manager** to the current user's application menu.

## Run

Start it from the application menu or from the project directory:

```bash
./run.sh
```

If Qt needs an explicitly selected display backend:

```bash
QT_QPA_PLATFORM=wayland ./run.sh
QT_QPA_PLATFORM=xcb ./run.sh
```

## Benchmarks (Linux)

The **Benchmarks** tab uses established Linux tools instead of custom benchmark
loops:

| Area | Tool | Result |
| --- | --- | --- |
| CPU | `sysbench` | Events per second |
| Memory | `sysbench` | Memory throughput in MiB/s |
| GPU | `glmark2` | Offscreen `refract` scene score |
| Disk | `fio` | Sequential read throughput in MiB/s |

CPU thread count, run duration, and prime limit are configurable. The disk test
uses a temporary 256 MiB file in the selected mounted filesystem and removes it
afterwards. It does not overwrite existing files or benchmark a raw block device.

Benchmark results vary with power profiles, cooling, background load, drivers, and
the selected filesystem. Compare runs made under similar conditions.

## Hardware Data Notes

Hardware reporting is best-effort because Linux drivers and individual devices
expose different data.

- SMART values require a drive and connection adapter that forward SMART commands.
  Some USB enclosures do not expose all lifetime counters.
- The hardware setup action in **Settings** can install `smartmontools` and set up
  the restricted read-only helper required for SMART and SMBIOS memory data.
- CPU package wattage appears only when the kernel exposes a compatible energy or
  power sensor, such as RAPL, `amd_energy`, or Zenpower.
- GPU metrics use `nvidia-smi` for NVIDIA hardware and Linux DRM/sysfs interfaces
  for AMD, Intel, and Nouveau drivers where available.

Unavailable values are shown as such rather than guessed.

## Custom Styling

The **Settings** tab includes an **Own Style (QSS)** editor with insertable
templates for the application, tab bar, buttons, tables, performance tiles, and
charts. QSS rules are stored per user and applied after the built-in theme.

For example, this changes the chart accent color:

```css
LiveChart {
    qproperty-lineColor: #ff8c00;
    qproperty-backgroundColor: #151515;
    qproperty-gridColor: #454545;
}
```

Use **Reset style** in Settings to remove all custom QSS rules.

## Development

```bash
./install.sh
source .venv/bin/activate
python -m unittest tests.test_startup
```

Generated local files are excluded through `.gitignore`, including `.venv`, Python
bytecode, and test caches. User preferences are stored outside the repository in
the current user's Qt settings directory.

## Contributing

Issues and pull requests are welcome. Keep changes focused, preserve the
non-blocking UI behavior, and include a focused test when adding or changing a
feature.
