# WinTaskManager

A native Linux system monitor that looks and feels like the Windows Task Manager.
Choose the classic **Windows 10** look or the modern **Windows 11** look, pick a light,
dark or one of nine popular color schemes, and get live processes, detailed
performance graphs, sensors, services, Docker containers, autostart entries and
hardware benchmarks in one PySide6 desktop application. A built-in **server mode**
lets you watch a headless Linux server from your desktop.

Built for Linux desktops running Wayland or X11. The user interface is in German.

![Windows 10 design, light theme, performance view](docs/screenshots/win10-light-performance.png)

## Contents

- [Screenshots](#screenshots)
- [Features at a glance](#features-at-a-glance)
- [What gets installed](#what-gets-installed)
- [Install, run, update, uninstall](#install)
- [Feature guide (every tab)](#feature-guide)
- [Design and color schemes](#design-and-color-schemes)
- [Remote monitoring (server mode)](#remote-monitoring-server-mode)
- [Benchmarks](#benchmarks)
- [Hardware data notes](#hardware-data-notes)
- [Custom styling (QSS)](#custom-styling-qss)
- [Security](#security)
- [Development](#development)

## Screenshots

| Windows 10, light | Windows 11, light |
| --- | --- |
| ![Process list in the Windows 10 design](docs/screenshots/win10-light-processes.png) | ![Process list in the Windows 11 design](docs/screenshots/win11-light-processes.png) |
| Process list with heat map, grouped into apps and background processes | Navigation pane, page header with search and commands |

| Windows 10, dark | Windows 11, dark |
| --- | --- |
| ![One graph per logical processor](docs/screenshots/win10-dark-performance-cores.png) | ![Memory view in the Windows 11 design](docs/screenshots/win11-dark-performance.png) |
| One graph per logical processor ("Pro Kern") | Memory usage |
| ![GPU view](docs/screenshots/win10-dark-gpu.png) | ![Settings in the Windows 11 design](docs/screenshots/win11-dark-settings.png) |
| GPU engines, dedicated and shared GPU memory | Settings: design, color scheme, refresh rates, remote server |
| ![Network view](docs/screenshots/win10-dark-network.png) | |
| Network throughput, receive (solid) and send (dashed), auto-scaled | |

| More tabs | |
| --- | --- |
| ![Services](docs/screenshots/win10-light-services.png) | ![Docker](docs/screenshots/win11-dark-docker.png) |
| **Dienste**: all systemd services with description and status | **Docker**: containers with live CPU, memory and GPU usage (example containers) |
| ![Autostart](docs/screenshots/win11-light-autostart.png) | ![Benchmarks](docs/screenshots/win10-dark-benchmarks.png) |
| **Autostart**: user and system autostart entries, enable/disable | **Benchmarks**: CPU, memory, GPU and disk results |
| ![Sensors](docs/screenshots/win10-dark-sensors.png) | |
| **Sensoren**: every hardware sensor the kernel exposes | |

**Color schemes** (here: Dracula, Nord, Catppuccin Latte, Tokyo Night in the Windows 11 design):

![Four color schemes side by side](docs/screenshots/color-schemes.png)

## Features at a glance

- **Processes** with Windows-style grouping, heat map, overall usage in the column
  headers, search, end task, run new task and drag & drop to start programs.
- **Performance** graphs for CPU (overall or per logical processor), memory, every
  physical disk, network (auto-scaled, send/receive) and GPU (selectable engines,
  dedicated and shared memory).
- **Detailed hardware data**: CPU temperature and frequency, RAM speed and module
  temperature, SMART disk data, GPU model, driver, temperature, power and PCIe link.
- **Sensors**, **systemd services**, **Docker containers** (start/stop/restart/remove,
  favorites, custom names and pictures) and **autostart entries** (enable/disable).
- **Benchmarks** for CPU, memory, GPU and disk using established Linux tools.
- **Two designs** (Windows 10 / Windows 11) and **11 color schemes**.
- **Server mode**: monitor a headless machine over the network, protected by an
  access token.
- **Customizable**: refresh rates, column widths and row height are remembered, and
  your own Qt Style Sheet (QSS) rules can override anything.

## What gets installed

`./install.sh` asks for your password once (for the package manager) and then
installs:

**System packages**

| Package | Used for | Required? |
| --- | --- | --- |
| `python3`, `python3-venv`, `python3-pip` | Running the application | Yes |
| `libxcb-cursor0` / `xcb-util-cursor` | Needed by Qt 6.5+ to start on X11 | Yes (X11) |
| `smartmontools` | Disk health data (SMART): temperature, power-on hours, lifetime reads/writes | Optional |
| `lm-sensors` | Hardware sensors and temperatures | Optional |
| `dmidecode` | RAM speed and module information | Optional |
| `sysbench` | CPU and memory benchmarks | Optional |
| `fio` | Disk benchmark | Optional |
| `glmark2` | GPU benchmark | Optional |
| `pciutils` | Exact GPU model name (`lspci`) | Optional |

"Optional" means the application runs without it; the related values or
benchmarks are then shown as unavailable. Package names differ slightly between
distributions; the installer picks the right ones. If a distribution does not ship
one of the optional packages, the installer skips it and lists it at the end
instead of aborting.

**Python packages** (only inside the project's own virtual environment, not
system-wide): [`PySide6`](https://pypi.org/project/PySide6/) (Qt user interface)
and [`psutil`](https://pypi.org/project/psutil/) (process and system data).

**Files created**

| Location | Content |
| --- | --- |
| `<project folder>/.venv/` | Virtual environment with the Python packages |
| `~/.local/share/applications/wintaskmanager.desktop` | **Task-Manager** entry in your application menu |
| `~/.config/WinTaskManager/TaskManager.conf` | Your settings (created on first start, readable only by you) |

Nothing else is changed on your system. The optional **Einstellungen →
Hardwaredaten mit sudo einrichten** button additionally installs a small read-only
helper (`/usr/local/libexec/wintaskmanager-smartctl`) and a matching sudo rule
(`/etc/sudoers.d/wintaskmanager-smartctl`) so SMART and RAM data can be read
without typing your password every time. The helper can only run `smartctl` on
block devices and `dmidecode -t memory`.

The button detects your package manager (apt, dnf, pacman, zypper, apk) and opens
your terminal (e.g. Konsole, GNOME Terminal, Ptyxis, Xfce Terminal, kitty,
Alacritty, foot, xterm) to ask for your password. The sudo rule is checked with
`visudo` *before* it is installed, so a faulty rule can never break `sudo`. If no
terminal is found, the application shows the commands to run yourself. `sudo` must
be installed.

On a server without a desktop, run the same setup manually from the project folder
(after installing `smartmontools` and `dmidecode` with your package manager):

```bash
sudo install -d -m 0755 /usr/local/libexec
sudo install -m 0755 scripts/wintaskmanager-smartctl /usr/local/libexec/wintaskmanager-smartctl
tmp="$(mktemp)"
echo "$USER ALL=(root) NOPASSWD: /usr/local/libexec/wintaskmanager-smartctl *" > "$tmp"
sudo visudo -cf "$tmp" && sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/wintaskmanager-smartctl
rm -f "$tmp"
```

**Also used if present** (not installed by the script): `lspci` (package
`pciutils`, preinstalled on most systems) for the exact GPU model name,
`nvidia-smi` for NVIDIA GPUs, the `docker` CLI for the Docker tab and `systemctl`
for the services tab.

### Supported distributions

| Distribution family | Package manager |
| --- | --- |
| Debian, Ubuntu, Linux Mint | `apt` |
| Fedora | `dnf` |
| Arch Linux | `pacman` |
| openSUSE | `zypper` |
| Alpine Linux | `apk` |

Any other distribution works too if Python 3 (with `venv` and `pip`) is installed:
the installer then skips the system packages and sets up the application anyway.
Install the optional tools yourself if you want the related data.

Note: PySide6 is installed with `pip`, which provides it for glibc-based systems
(practically all distributions) on x86-64 and ARM64. Alpine Linux uses musl, for
which `pip` has no PySide6 build; there you need a PySide6 package from the
distribution, which is untested.

## Install

```bash
git clone https://github.com/Epiklal/linux-task-manager-windows-style.git
cd linux-task-manager-windows-style
./install.sh
```

## Run

Start **Task-Manager** from your application menu, or from the project folder:

```bash
./run.sh
```

If Qt needs an explicitly selected display backend:

```bash
QT_QPA_PLATFORM=wayland ./run.sh
QT_QPA_PLATFORM=xcb ./run.sh
```

## Update

```bash
cd linux-task-manager-windows-style
git pull
./install.sh
```

Your settings are kept.

## Uninstall

```bash
rm ~/.local/share/applications/wintaskmanager.desktop
rm -r ~/.config/WinTaskManager
rm -r linux-task-manager-windows-style      # the project folder, including .venv
```

If you used **Hardwaredaten mit sudo einrichten**, also remove the helper and
its sudo rule:

```bash
sudo rm /etc/sudoers.d/wintaskmanager-smartctl /usr/local/libexec/wintaskmanager-smartctl
```

If you set up the server as a systemd service (see below), stop and remove it too:

```bash
sudo systemctl disable --now wintaskmanager-server
sudo rm /etc/systemd/system/wintaskmanager-server.service
```

The system packages from the table above stay installed; remove them with your
package manager if you no longer need them.

## Feature guide

The tab names are the German names shown in the application.

### Prozesse (processes)

- **Columns**: Name, Status, CPU, Arbeitsspeicher (memory), GPU, GPU-Speicher
  (GPU memory), PID, Benutzer (user), Threads. Above CPU, memory, GPU and GPU memory
  the header shows the overall usage, e.g. `7%` above *CPU*.
- **Heat map**: the busier a process, the stronger its cells are colored (yellow in
  the Windows 10 design, blue in Windows 11, scheme colors otherwise).
- **Grouping**: sorted by name (the default), processes are split into *Apps* (your
  own processes) and *Hintergrundprozesse* (background processes). Processes with the
  same name are combined into one group, e.g. `bash (18)`, with summed values.
  Click the arrow to expand or collapse a group.
- **Sorting**: click a column header; click again to reverse.
- **Search**: filter by process name.
- **Task beenden** (end task): select a process and click the button (Windows 10:
  bottom right, Windows 11: page header). You are asked for confirmation; the process
  first gets a normal terminate signal and is killed only if it does not exit within
  two seconds.
- **Neuen Task ausführen** (run new task): enter a command, e.g. `firefox` or
  `gnome-terminal --working-directory=/tmp` (Windows 10: menu *Datei*, Windows 11:
  page header).
- **Datei starten** (menu *Datei*): choose an executable file to start.
- **Drag & drop**: drop executable files from your file manager onto the list to
  start them.
- Program icons come from your desktop icon theme.

### Leistung (performance)

The left column shows a small live graph for every device; click one to see details.

- **CPU**: utilization graph for the last 60 seconds. **Pro Kern** switches to one
  graph per logical processor. Below: utilization, speed, temperature, processes,
  uptime, cores, logical processors and power draw (if the kernel provides it).
- **Arbeitsspeicher** (memory): usage graph; used, available, cached and total
  memory, RAM speed and module temperature.
- **Datenträger** (one entry per physical disk): active time; read and write speed,
  lifetime reads/writes, power cycles, power-on hours, used/total space and
  temperature (SMART values see [Hardware data notes](#hardware-data-notes)).
- **Netzwerk** (network): receive (solid line) and send (dashed line). The graph
  scales automatically (shown top right, e.g. `10,0 MBit/s`). Also: adapter, link
  speed, IPv4 address and totals since boot.
- **GPU**:
  - four engine graphs. Click an engine name (e.g. **3D ▾**) to choose what the
    graph shows (3D, copy, video decode, video encode, compute, memory controller,
    PCIe receive/send, ... depending on the driver).
  - right-click a GPU graph for **Graph anzeigen als** (choose the metric) and
    **Anzahl Graphen** (show 1 to 10 engine graphs).
  - graphs for dedicated and shared GPU memory.
  - details: utilization, dedicated/shared/total GPU memory, GPU temperature,
    driver, driver version, physical location (PCI bus), PCIe generation and width,
    link status and power draw.

### Sensoren (sensors)

Every hardware sensor the Linux kernel exposes (`/sys/class/hwmon`, no root
needed): temperatures, fan speeds, voltages, power and more, with category, value,
unit and source. Refreshes automatically; click a column header to sort.

### Dienste (services)

All systemd services with description and status (read-only), with search and
**Aktualisieren** (refresh). Requires `systemctl`.

### Docker

All containers (running and stopped) with image, status, CPU %, memory, memory
limit, memory %, GPU % and GPU memory, colored with the same heat map as the process
list. Search by name.

- **Starten / Stoppen / Neustart / Entfernen**: start, stop, restart or remove the
  selected container (removing asks for confirmation).
- **★ Favorit**, **Bild wählen...** (custom picture) and **Umbenennen...** (custom
  display name) make containers easier to find. These are stored only in your
  settings; the real container is not renamed.

Requires the `docker` CLI and permission to use it (e.g. membership in the `docker`
group).

### Autostart

All XDG autostart entries of your user (`~/.config/autostart`) and of the system
(`/etc/xdg/autostart`) with command, source and status, plus search and refresh.
**Aktivieren/Deaktivieren** toggles the selected entry. System entries are never
edited directly: a personal override copy is created in `~/.config/autostart`, as
the XDG specification intends.

### Benchmarks

Choose the tests, set the options and click **Ausgewählte starten** (start
selected). See [Benchmarks](#benchmarks) for details. The application stays
responsive while a benchmark runs.

### Einstellungen (settings)

- **Darstellung**: design (Windows 10 / Windows 11) and color scheme.
- **Aktualisierungsgeschwindigkeit**: refresh rate of the process list (200-5000 ms)
  and of the performance graphs (100-5000 ms).
- **Remote-Server**: connect to a machine running in server mode.
- **Hardwaredaten (SMART und Arbeitsspeicher)**: one-time setup for SMART and RAM
  data (see [What gets installed](#what-gets-installed)).
- **Eigener Stil (QSS)**: your own style rules (see [Custom styling](#custom-styling-qss)).

### Menus (Windows 10 design)

- **Datei**: Neuen Task ausführen, Datei starten…, Beenden (quit).
- **Optionen**: Immer im Vordergrund (always on top; whether this works depends on
  your window manager - some Wayland compositors ignore it).
- **Ansicht**: Design and Farbschema.

The Windows 11 design has no menu bar, just like the original: the commands are in
the page headers, the options in **Einstellungen**.

### Mouse and keyboard tips

- **Ctrl + mouse wheel** over a table makes all rows taller or shorter; the height
  is remembered per table.
- Drag the border between two column headers to resize a column; widths are
  remembered.

## Design and color schemes

Open **Einstellungen → Darstellung**:

- **Design**: *Windows 10* (tabs, menu bar, footer) or *Windows 11* (navigation pane
  with icons, page headers, rounded controls, no menu bar or status bar).
- **Farbschema**: Hell (light, default), Dunkel (dark), Nord, Dracula, Catppuccin
  Mocha, Catppuccin Latte, Gruvbox Dark, Tokyo Night, One Dark, Solarized Dark or
  Solarized Light.

Every color scheme works with both designs and also changes the graph colors and
the heat map. In the Windows 10 design both options are also in the menu under
**Ansicht**. Changes apply immediately.

## Remote monitoring (server mode)

The same program can run **headless on a server** (no desktop needed) and send its
data to the desktop application on another computer. This is useful for a home
server, NAS or VM.

### 1. Install on the server

Install exactly as on the desktop (clone the repository, run `./install.sh`).

### 2. Start the server

```bash
./run.sh --server
```

Example output:

```text
WinTaskManager Server läuft auf http://192.168.1.20:8765
Zugriffsschlüssel: 3f1Vw0...kQ
Beenden mit Strg+C
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--host ADDRESS` | `0.0.0.0` (all network interfaces) | Address to listen on; use `127.0.0.1` for local/SSH-tunnel access only |
| `--port PORT` | `8765` | TCP port |
| `--token TOKEN` | *random* | Use your own access token instead of a random one |
| `--generate-token` | *(default behavior)* | Generate a random token; kept for compatibility |
| `--no-auth` | off | Start **without** a token. Anyone who can reach the port can read all data and start benchmarks - only for trusted networks |

Without `--token`, a **new random token is generated on every start**. For a server
that runs permanently, use a fixed token (see step 4).

If a firewall is active, open the port, e.g. `sudo ufw allow 8765/tcp`.

### 3. Connect from the desktop

Open **Einstellungen → Remote-Server**, enable **Daten vom Server verwenden**,
enter the server IP, port and access token, and click **Serververbindung
anwenden**. The token is stored in your settings file, which only you can read.

While connected, these tabs show the **server's** data: Leistung, Prozesse,
Sensoren, Dienste, Docker and Autostart. Benchmarks started on the Benchmarks tab
run **on the server**. Disable the checkbox to go back to local data.

In remote mode the application is **read-only**: *Task beenden*, autostart changes
and Docker start/stop/restart/remove are disabled, because they would act on your
local machine rather than on the server. Local-only things such as Docker favorites,
names and pictures still work.

### 4. Run the server permanently (systemd)

Create a fixed token once:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Create `/etc/systemd/system/wintaskmanager-server.service` (adjust the path, user
and token):

```ini
[Unit]
Description=WinTaskManager monitoring server
After=network-online.target
Wants=network-online.target

[Service]
User=youruser
ExecStart=/home/youruser/linux-task-manager-windows-style/run.sh --server --token YOUR_TOKEN
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Because the file contains the token, make it readable by root only and start the
service:

```bash
sudo chmod 600 /etc/systemd/system/wintaskmanager-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now wintaskmanager-server
```

For SMART and RAM details on the server, set up the helper and sudo rule once with
the manual commands in [What gets installed](#what-gets-installed) (run them as the
user that runs the server).

### Limits for remote benchmarks

To protect the server, benchmark requests are limited: at most 300 seconds, at most
one thread per CPU core, only one benchmark at a time, and the disk test only runs on
a mount point of one of the server's physical disks.

### API (for your own scripts)

All requests need the header `Authorization: Bearer <token>` (unless `--no-auth`).

```bash
# Full snapshot as JSON
curl -H "Authorization: Bearer $TOKEN" http://SERVER:8765/api/v1/snapshot

# Run a benchmark ("cpu", "memory", "gpu" or "disk")
curl -H "Authorization: Bearer $TOKEN" \
     -d '{"kind": "cpu", "cpu_threads": 4, "cpu_seconds": 10}' \
     http://SERVER:8765/api/v1/benchmark
```

The snapshot contains `version`, `timestamp`, `hostname`, `system` (CPU, memory,
network, uptime), `cpu_model`, `gpu`, `processes`, `sensors`, `services`, `docker`,
`docker_error`, `autostart` and `disks`. Error answers: `401` wrong or missing token,
`409` a benchmark is already running, `413` request too large, `400` invalid request.

## Benchmarks

The **Benchmarks** tab uses established Linux tools instead of custom benchmark
loops:

| Area | Tool | Result |
| --- | --- | --- |
| CPU | `sysbench` | Events per second |
| Memory | `sysbench` | Memory throughput in MiB/s |
| GPU | `glmark2` | Offscreen `refract` scene score |
| Disk | `fio` | Sequential read throughput in MiB/s |

Options: number of CPU threads (1 up to your number of logical processors), CPU test
duration (5-300 seconds), prime limit, and the target filesystem for the disk test.
The disk test uses a temporary 256 MiB file in the selected mounted filesystem and
removes it afterwards. If you may not write to the mount point itself (e.g. `/`),
the file goes into a writable folder on the same disk, such as `~/.cache` or
`/var/tmp`. It does not overwrite existing files or benchmark a raw block
device.

Benchmark results vary with power profiles, cooling, background load, drivers, and
the selected filesystem. Compare runs made under similar conditions.

## Hardware data notes

Hardware reporting is best-effort because Linux drivers and individual devices
expose different data. Unavailable values are shown as *Nicht verfügbar* rather
than guessed.

- **SMART** values require a drive and connection adapter that forward SMART
  commands. Some USB enclosures do not expose all lifetime counters. Most values
  need root rights; use **Hardwaredaten mit sudo einrichten** once (or the manual
  commands in [What gets installed](#what-gets-installed)).
- **RAM speed and module temperature** need `dmidecode` (root, via the same helper)
  and a board that reports them.
- **CPU power draw** appears only when the kernel exposes a compatible energy or
  power sensor, such as RAPL, `amd_energy`, or Zenpower.
- **GPU** metrics use `nvidia-smi` for NVIDIA hardware and the Linux DRM/sysfs
  interface for AMD, Intel, and Nouveau drivers. Which engines are available depends
  on the driver. The model name is read with `lspci`.
- **Per-process GPU usage** comes from `nvidia-smi` on NVIDIA and otherwise from the
  kernel's DRM fdinfo interface (amdgpu, i915, xe, nouveau).

## Custom styling (QSS)

**Einstellungen → Eigener Stil (QSS)** accepts Qt Style Sheet rules. Pick a
template (whole application, tab bar, buttons, tables, performance tiles, charts)
and click **Vorlage einfügen**, edit it and click **Stil anwenden**. Your rules are
stored per user and applied on top of the selected design and color scheme.

For example, this changes the chart colors:

```css
LiveChart {
    qproperty-lineColor: #ff8c00;
    qproperty-backgroundColor: #151515;
    qproperty-gridColor: #454545;
}
```

**Stil zurücksetzen** removes all custom rules.

## Security

- The settings file (including the server token) is readable only by your user.
- The server always requires an access token unless started with `--no-auth`;
  tokens are compared in constant time.
- Remote mode never runs actions on your local machine for data that comes from the
  server.
- Autostart changes only ever touch `.desktop` files in the XDG autostart folders.
- The optional SMART helper validates the device name and can only read SMART and
  memory data.
- The server connection uses plain HTTP. On networks you do not trust, start the
  server with `--host 127.0.0.1` and connect through an SSH tunnel, then use
  `127.0.0.1` as the server IP in the settings:

  ```bash
  ssh -L 8765:localhost:8765 user@server
  ```

## Development

```bash
./install.sh
source .venv/bin/activate
python -m unittest tests.test_startup
```

Project layout:

| Path | Content |
| --- | --- |
| `main.py` | Entry point (desktop application or `--server`) |
| `app/main_window.py` | Main window, menus, Windows 10 / 11 stylesheets |
| `app/themes.py` | Color schemes; a new scheme only needs a few base colors plus five chart colors |
| `app/system_info.py` | All data collection (processes, hardware, disks, GPU, Docker, autostart) |
| `app/remote_server.py`, `app/remote_client.py` | Server mode and its client |
| `app/settings.py` | Persisted settings |
| `pages/` | One module per tab |
| `widgets/` | Graphs, performance tiles, Windows 11 navigation pane, tables |

Generated local files are excluded through `.gitignore`, including `.venv`, Python
bytecode, and test caches. User preferences are stored outside the repository in
the current user's Qt settings directory.

## Contributing

Issues and pull requests are welcome. Keep changes focused, preserve the
non-blocking UI behavior, and include a focused test when adding or changing a
feature.
