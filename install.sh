#!/usr/bin/env bash
# Re-exec with bash when started as "sh install.sh" (dash cannot parse arrays).
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -Eeuo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/lintask.desktop"
LEGACY_DESKTOP_FILE="$DESKTOP_DIR/wintaskmanager.desktop"

# Any failing command ends the script (set -e); say where and why instead of
# dying silently in the middle of a download.
on_error() {
    local rc=$? line=$1 cmd=$2
    echo >&2
    echo "ERROR: The installation was aborted (line $line, exit code $rc):" >&2
    echo "    $cmd" >&2
    echo "Common causes: no Internet connection, no sudo rights, missing package python3-venv." >&2
    echo "After fixing it, simply run ./install.sh again." >&2
}
trap 'on_error $LINENO "$BASH_COMMAND"' ERR

# Required: Python with venv/pip, the libraries Qt needs at runtime and a font.
# The PySide6 wheel from pip brings Qt itself but none of these, and a minimal
# installation has few of them (apt list: same as packaging/debian/control.in).
# Where the distribution ships a current PySide6 (Arch: pyside6) it is installed
# as well, so nothing has to be downloaded with pip (see the venv step below).
# Optional: tools for hardware data and benchmarks. A missing optional package
# (not every distribution ships all of them) only prints a warning.
PM=""
REQUIRED=()
OPTIONAL=()
if command -v apt-get >/dev/null; then
    PM="apt"
    REQUIRED=(python3 python3-venv python3-pip
              libxcb-cursor0 libxkbcommon0 libxkbcommon-x11-0 libx11-xcb1
              libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0
              libxcb-shape0 libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxcb-util1
              libegl1 libgl1 libopengl0 libfontconfig1 libfreetype6 libdbus-1-3
              libxrender1 libxi6 libsm6 libice6 libglib2.0-0 fonts-dejavu-core)
    OPTIONAL=(smartmontools lm-sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v dnf >/dev/null; then
    PM="dnf"
    REQUIRED=(python3 python3-pip
              xcb-util-cursor libxkbcommon-x11 libX11-xcb
              xcb-util-wm xcb-util-keysyms xcb-util-image xcb-util-renderutil
              libglvnd-egl libglvnd-glx libglvnd-opengl fontconfig dbus-libs
              libXrender libSM libICE glib2 dejavu-sans-fonts)
    OPTIONAL=(smartmontools lm_sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v pacman >/dev/null; then
    PM="pacman"
    REQUIRED=(python pyside6 python-psutil xcb-util-cursor ttf-dejavu)
    OPTIONAL=(smartmontools lm_sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v zypper >/dev/null; then
    PM="zypper"
    REQUIRED=(python3 python3-pip
              libxcb-cursor0 libxkbcommon-x11-0 libX11-xcb1
              libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0
              libxcb-shape0 libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1
              libEGL1 libGL1 libOpenGL0 libfontconfig1 libdbus-1-3
              libXrender1 libSM6 libICE6 libglib-2_0-0 dejavu-fonts)
    OPTIONAL=(smartmontools sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v apk >/dev/null; then
    PM="apk"
    REQUIRED=(python3 py3-pip
              xcb-util-cursor libxkbcommon xcb-util-wm xcb-util-keysyms xcb-util-image
              xcb-util-renderutil mesa-egl mesa-gl fontconfig dbus-libs font-dejavu)
    OPTIONAL=(smartmontools lm-sensors dmidecode sysbench fio glmark2 pciutils)
fi

# System packages need root. Run as a normal user: the script asks for the
# sudo password itself; the venv and the menu entry then belong to that user.
SUDO="sudo"
if [[ $EUID -eq 0 ]]; then
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        echo "Please run ./install.sh without sudo - the script asks for the password itself." >&2
        echo "(With sudo the virtual environment and the menu entry would belong to the user root.)" >&2
        exit 1
    fi
    SUDO=""
elif ! command -v sudo >/dev/null; then
    echo "Warning: sudo is missing - skipping system packages. Please install them as root:" >&2
    echo "    ${REQUIRED[*]} ${OPTIONAL[*]}" >&2
    PM=""
fi

pm_install() {
    case "$PM" in
        apt) $SUDO apt-get install -y "$@" ;;
        dnf) $SUDO dnf install -y "$@" ;;
        pacman) $SUDO pacman -S --needed --noconfirm "$@" ;;
        zypper) $SUDO zypper --non-interactive install "$@" ;;
        apk) $SUDO apk add "$@" ;;
    esac
}

# A fresh Arch has a stale package database, so pacman -S fails with 404 errors.
# Arch only supports full upgrades ("pacman -Sy" alone causes partial upgrades), and
# upgrading the whole system is the user's decision: pacman asks, without --noconfirm.
upgrade_arch_or_abort() {
    echo "pacman could not install the packages. The package database is probably outdated;" >&2
    echo "Arch only supports upgrading the whole system." >&2
    if [[ -t 0 ]] && $SUDO pacman -Syu --needed "${REQUIRED[@]}"; then
        return
    fi
    echo "ERROR: Please run this and then ./install.sh again:" >&2
    echo "    sudo pacman -Syu" >&2
    exit 1
}

MISSING_OPTIONAL=()
install_system_packages() {
    if [[ -z "$PM" ]]; then
        echo "Unknown package manager - skipping system packages." >&2
        echo "Required: Python 3 with venv and pip, the X11 and OpenGL libraries Qt needs, a font." >&2
        echo "Optional: smartmontools, lm-sensors, dmidecode, sysbench, fio, glmark2, pciutils." >&2
        return
    fi
    echo "Installing system packages ($PM) ..."
    if [[ "$PM" == "apt" ]]; then
        $SUDO apt-get update || echo "Warning: apt-get update failed." >&2
    fi
    if ! pm_install "${REQUIRED[@]}"; then
        if [[ "$PM" == "pacman" ]]; then
            upgrade_arch_or_abort
        else
            # One name may differ on this release; verify_qt below decides what really counts.
            echo "Warning: not all required packages could be installed - trying one by one." >&2
            for package in "${REQUIRED[@]}"; do
                pm_install "$package" || echo "Warning: package not available: $package" >&2
            done
        fi
    fi
    # Try all optional packages at once; if that fails, one by one.
    if ! pm_install "${OPTIONAL[@]}"; then
        for package in "${OPTIONAL[@]}"; do
            pm_install "$package" || MISSING_OPTIONAL+=("$package")
        done
    fi
}

install_system_packages

if ! command -v python3 >/dev/null; then
    echo "Python 3 was not found. Please install Python 3 and run this again." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    echo "Python $(python3 --version 2>&1 | cut -d' ' -f2) is too old - Python 3.9 or newer is required." >&2
    exit 1
fi

# The distribution's own PySide6 (Arch: pyside6, Debian 13: python3-pyside6.*) is
# preferred: the venv then only passes the system packages through and the
# ~100 MB pip download is skipped. Same check as packaging/debian/setup-venv.
SYSTEM_QT_CHECK='import sys
import PySide6, PySide6.QtCore, PySide6.QtGui, PySide6.QtWidgets, psutil
sys.exit(PySide6.__version_info__ < (6, 6))'
if python3 -c "$SYSTEM_QT_CHECK" 2>/dev/null; then
    QT_SOURCE="System package (PySide6 $(python3 -c 'import PySide6; print(PySide6.__version__)'))"
    echo "PySide6 and psutil are available as system packages - no download needed."
    # A venv from an earlier run may still hold its own (older) PySide6 copy;
    # recreating it costs nothing because nothing has to be downloaded.
    [[ -e "$VENV_DIR" ]] && rm -rf "$VENV_DIR"
    echo "Creating virtual environment: $VENV_DIR"
    python3 -m venv --system-site-packages --without-pip "$VENV_DIR"
else
    QT_SOURCE="pip (in $VENV_DIR)"
    echo "Creating virtual environment: $VENV_DIR"
    if ! python3 -m venv "$VENV_DIR"; then
        echo "ERROR: 'python3 -m venv' failed. On Debian/Ubuntu/Mint this usually means the package python3-venv is missing:" >&2
        echo "    sudo apt install python3-venv" >&2
        exit 1
    fi
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip \
        || echo "Warning: pip could not be updated - continuing with the existing version." >&2
    echo "Installing Python packages (PySide6 is ~100 MB - this can take a few minutes) ..."
    "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
fi

# Only a Qt that really loads counts as installed; otherwise the failure would
# show up later as an application that doesn't start. The X11 plugin links
# against the libraries from REQUIRED, ldd names the ones that are missing.
QT_PLUGINS_CHECK='from PySide6.QtCore import QLibraryInfo
import PySide6.QtWidgets, psutil
print(QLibraryInfo.path(QLibraryInfo.PluginsPath))'
verify_qt() {
    local plugins missing=""
    if ! plugins="$("$VENV_DIR/bin/python" -c "$QT_PLUGINS_CHECK" 2>&1)"; then
        echo "ERROR: Qt could not be loaded after the installation:" >&2
        echo "$plugins" | tail -n 3 >&2
        return 1
    fi
    if [[ -e "$plugins/platforms/libqxcb.so" ]] && command -v ldd >/dev/null; then
        missing="$(ldd "$plugins/platforms/libqxcb.so" | awk '/not found/ {print $1}' | sort -u)" || true
    fi
    if [[ -n "$missing" ]]; then
        echo "ERROR: Qt needs system libraries that are not installed:" >&2
        echo "$missing" | sed 's/^/    /' >&2
        return 1
    fi
}
if ! verify_qt; then
    if [[ -n "$PM" ]]; then
        echo "Packages for this: ${REQUIRED[*]}" >&2
    fi
    echo "After fixing it, simply run ./install.sh again." >&2
    exit 1
fi

# ZIP downloads from GitHub lose the executable bit (and "sh install.sh" works without it).
chmod +x "$APP_DIR/install.sh" "$APP_DIR/run.sh" 2>/dev/null || true

mkdir -p "$DESKTOP_DIR"
escaped_app_dir=${APP_DIR//\\/\\\\}
escaped_app_dir=${escaped_app_dir//&/\\&}
sed "s|@APP_DIR@|$escaped_app_dir|g" "$APP_DIR/lintask.desktop.in" > "$DESKTOP_FILE"
chmod 644 "$DESKTOP_FILE"
# The entry an install from before the rename created; two menu entries otherwise.
rm -f "$LEGACY_DESKTOP_FILE"

echo
echo "Installation finished."
echo "Qt/PySide6: $QT_SOURCE"
if (( ${#MISSING_OPTIONAL[@]} )); then
    echo "Not available (optional, the related values/benchmarks will be missing): ${MISSING_OPTIONAL[*]}"
fi
echo "Start in a terminal: $APP_DIR/run.sh"
echo "Application menu entry: Lintask"
