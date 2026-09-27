#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/wintaskmanager.desktop"

# Required: Python with venv/pip, and xcb-cursor (Qt >= 6.5 needs it on X11).
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
    REQUIRED=(python python-pip xcb-util-cursor)
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

pm_install() {
    case "$PM" in
        apt) sudo apt-get install -y "$@" ;;
        dnf) sudo dnf install -y "$@" ;;
        pacman) sudo pacman -S --needed --noconfirm "$@" ;;
        zypper) sudo zypper --non-interactive install "$@" ;;
        apk) sudo apk add "$@" ;;
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
    # (No "pacman -Sy" on Arch: refreshing without upgrading causes partial upgrades.)
    if [[ "$PM" == "apt" ]]; then
        sudo apt-get update || echo "Warnung: apt-get update fehlgeschlagen." >&2
    fi
    if ! pm_install "${REQUIRED[@]}"; then
        # xcb-cursor may be named differently on old releases; Python is what matters.
        echo "Warnung: nicht alle Pflichtpakete (${REQUIRED[*]}) konnten installiert werden." >&2
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

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

mkdir -p "$DESKTOP_DIR"
escaped_app_dir=${APP_DIR//\\/\\\\}
escaped_app_dir=${escaped_app_dir//&/\\&}
sed "s|@APP_DIR@|$escaped_app_dir|g" "$APP_DIR/wintaskmanager.desktop.in" > "$DESKTOP_FILE"
chmod 644 "$DESKTOP_FILE"

echo "Installation abgeschlossen."
if (( ${#MISSING_OPTIONAL[@]} )); then
    echo "Nicht verfügbar (optional, die zugehörigen Werte/Benchmarks fehlen dann): ${MISSING_OPTIONAL[*]}"
fi
echo "Start im Terminal: $APP_DIR/run.sh"
echo "Startmenü-Eintrag: Task-Manager"
