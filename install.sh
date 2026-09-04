#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/wintaskmanager.desktop"

install_system_packages() {
    local packages=()
    if command -v apt-get >/dev/null; then
        packages=(python3 python3-venv python3-pip smartmontools lm-sensors dmidecode sysbench fio glmark2)
        sudo apt-get update
        sudo apt-get install -y "${packages[@]}"
    elif command -v dnf >/dev/null; then
        packages=(python3 python3-pip smartmontools lm_sensors dmidecode sysbench fio glmark2)
        sudo dnf install -y "${packages[@]}"
    elif command -v pacman >/dev/null; then
        packages=(python python-pip smartmontools lm_sensors dmidecode sysbench fio glmark2)
        sudo pacman -Sy --needed --noconfirm "${packages[@]}"
    elif command -v zypper >/dev/null; then
        packages=(python3 python3-pip smartmontools sensors dmidecode sysbench fio glmark2)
        sudo zypper --non-interactive install "${packages[@]}"
    elif command -v apk >/dev/null; then
        packages=(python3 py3-pip smartmontools lm-sensors dmidecode sysbench fio glmark2)
        sudo apk add "${packages[@]}"
    else
        echo "Nicht unterstützte Paketverwaltung. Python 3, venv, smartmontools, lm-sensors, dmidecode, sysbench, fio und glmark2 bitte manuell installieren." >&2
        exit 1
    fi
}

install_system_packages

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

mkdir -p "$DESKTOP_DIR"
escaped_app_dir=${APP_DIR//\\/\\\\}
escaped_app_dir=${escaped_app_dir//&/\\&}
sed "s|@APP_DIR@|$escaped_app_dir|g" "$APP_DIR/wintaskmanager.desktop.in" > "$DESKTOP_FILE"
chmod 644 "$DESKTOP_FILE"

echo "Installation abgeschlossen."
echo "Start im Terminal: $APP_DIR/run.sh"
echo "Startmenü-Eintrag: Task-Manager"
