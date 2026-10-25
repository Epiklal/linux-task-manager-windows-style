#!/usr/bin/env bash
# Re-exec with bash when started as "sh install.sh" (dash cannot parse arrays).
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -Eeuo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/wintaskmanager.desktop"

# Any failing command ends the script (set -e); say where and why instead of
# dying silently in the middle of a download.
on_error() {
    local rc=$? line=$1 cmd=$2
    echo >&2
    echo "FEHLER: Die Installation ist abgebrochen (Zeile $line, Exit-Code $rc):" >&2
    echo "    $cmd" >&2
    echo "Häufige Ursachen: keine Internetverbindung, kein sudo-Recht, fehlendes Paket python3-venv." >&2
    echo "Nach dem Beheben einfach ./install.sh erneut starten." >&2
}
trap 'on_error $LINENO "$BASH_COMMAND"' ERR

# Required: Python with venv/pip, and xcb-cursor (Qt >= 6.5 needs it on X11).
# Where the distribution ships a current PySide6 (Arch: pyside6) it is installed
# as well, so nothing has to be downloaded with pip (see the venv step below).
# Optional: tools for hardware data and benchmarks. A missing optional package
# (not every distribution ships all of them) only prints a warning.
PM=""
REQUIRED=()
OPTIONAL=()
if command -v apt-get >/dev/null; then
    PM="apt"
    REQUIRED=(python3 python3-venv python3-pip libxcb-cursor0)
    OPTIONAL=(smartmontools lm-sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v dnf >/dev/null; then
    PM="dnf"
    REQUIRED=(python3 python3-pip xcb-util-cursor)
    OPTIONAL=(smartmontools lm_sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v pacman >/dev/null; then
    PM="pacman"
    REQUIRED=(python pyside6 python-psutil xcb-util-cursor)
    OPTIONAL=(smartmontools lm_sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v zypper >/dev/null; then
    PM="zypper"
    REQUIRED=(python3 python3-pip libxcb-cursor0)
    OPTIONAL=(smartmontools sensors dmidecode sysbench fio glmark2 pciutils)
elif command -v apk >/dev/null; then
    PM="apk"
    REQUIRED=(python3 py3-pip xcb-util-cursor)
    OPTIONAL=(smartmontools lm-sensors dmidecode sysbench fio glmark2 pciutils)
fi

# System packages need root. Run as a normal user: the script asks for the
# sudo password itself; the venv and the menu entry then belong to that user.
SUDO="sudo"
if [[ $EUID -eq 0 ]]; then
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        echo "Bitte ./install.sh ohne sudo starten - das Skript fragt selbst nach dem Passwort." >&2
        echo "(Mit sudo landen die virtuelle Umgebung und der Menüeintrag beim Benutzer root.)" >&2
        exit 1
    fi
    SUDO=""
elif ! command -v sudo >/dev/null; then
    echo "Warnung: sudo fehlt - Systempakete werden übersprungen. Bitte als root nachinstallieren:" >&2
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

MISSING_OPTIONAL=()
install_system_packages() {
    if [[ -z "$PM" ]]; then
        echo "Unbekannte Paketverwaltung - Systempakete werden übersprungen." >&2
        echo "Benötigt: Python 3 mit venv und pip. Optional: smartmontools, lm-sensors," >&2
        echo "dmidecode, sysbench, fio, glmark2, pciutils, xcb-cursor." >&2
        return
    fi
    echo "Installiere Systempakete ($PM) ..."
    # (No "pacman -Sy" on Arch: refreshing without upgrading causes partial upgrades.)
    if [[ "$PM" == "apt" ]]; then
        $SUDO apt-get update || echo "Warnung: apt-get update fehlgeschlagen." >&2
    fi
    if ! pm_install "${REQUIRED[@]}"; then
        # xcb-cursor may be named differently on old releases; Python is what matters.
        echo "Warnung: nicht alle Pflichtpakete (${REQUIRED[*]}) konnten installiert werden." >&2
        if [[ "$PM" == "pacman" ]]; then
            echo "Auf Arch hilft meist ein vorheriges 'sudo pacman -Syu' (veraltete Paketdatenbank)." >&2
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
    echo "Python 3 wurde nicht gefunden. Bitte Python 3 installieren und erneut starten." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    echo "Python $(python3 --version 2>&1 | cut -d' ' -f2) ist zu alt - benötigt wird Python 3.9 oder neuer." >&2
    exit 1
fi

# The distribution's own PySide6 (Arch: pyside6, Debian 13: python3-pyside6.*) is
# preferred: the venv then only passes the system packages through and the
# ~100 MB pip download is skipped. Same check as packaging/debian/setup-venv.
SYSTEM_QT_CHECK='import sys
import PySide6, PySide6.QtCore, PySide6.QtGui, PySide6.QtWidgets, psutil
sys.exit(PySide6.__version_info__ < (6, 6))'
if python3 -c "$SYSTEM_QT_CHECK" 2>/dev/null; then
    QT_SOURCE="Systempaket (PySide6 $(python3 -c 'import PySide6; print(PySide6.__version__)'))"
    echo "PySide6 und psutil sind als Systempakete vorhanden - kein Download nötig."
    # A venv from an earlier run may still hold its own (older) PySide6 copy;
    # recreating it costs nothing because nothing has to be downloaded.
    [[ -e "$VENV_DIR" ]] && rm -rf "$VENV_DIR"
    echo "Lege virtuelle Umgebung an: $VENV_DIR"
    python3 -m venv --system-site-packages --without-pip "$VENV_DIR"
else
    QT_SOURCE="pip (in $VENV_DIR)"
    echo "Lege virtuelle Umgebung an: $VENV_DIR"
    if ! python3 -m venv "$VENV_DIR"; then
        echo "FEHLER: 'python3 -m venv' schlug fehl. Auf Debian/Ubuntu/Mint fehlt dann meist das Paket python3-venv:" >&2
        echo "    sudo apt install python3-venv" >&2
        exit 1
    fi
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip \
        || echo "Warnung: pip konnte nicht aktualisiert werden - weiter mit der vorhandenen Version." >&2
    echo "Installiere Python-Pakete (PySide6 ist ~100 MB - das kann einige Minuten dauern) ..."
    "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
fi

# ZIP downloads from GitHub lose the executable bit (and "sh install.sh" works without it).
chmod +x "$APP_DIR/install.sh" "$APP_DIR/run.sh" 2>/dev/null || true

mkdir -p "$DESKTOP_DIR"
escaped_app_dir=${APP_DIR//\\/\\\\}
escaped_app_dir=${escaped_app_dir//&/\\&}
sed "s|@APP_DIR@|$escaped_app_dir|g" "$APP_DIR/wintaskmanager.desktop.in" > "$DESKTOP_FILE"
chmod 644 "$DESKTOP_FILE"

echo
echo "Installation abgeschlossen."
echo "Qt/PySide6: $QT_SOURCE"
if (( ${#MISSING_OPTIONAL[@]} )); then
    echo "Nicht verfügbar (optional, die zugehörigen Werte/Benchmarks fehlen dann): ${MISSING_OPTIONAL[*]}"
fi
echo "Start im Terminal: $APP_DIR/run.sh"
echo "Startmenü-Eintrag: Task-Manager"
