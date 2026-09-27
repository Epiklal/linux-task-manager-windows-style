"""One-time setup for SMART / RAM data: installs smartmontools + dmidecode, the
read-only helper script and a sudo rule that allows running only that helper.

Works on every distribution the installer supports (apt, dnf, pacman, zypper, apk)
and with the common terminal emulators.
"""
from __future__ import annotations

import shlex
import shutil

HELPER_TARGET = "/usr/local/libexec/wintaskmanager-smartctl"
SUDOERS_TARGET = "/etc/sudoers.d/wintaskmanager-smartctl"

# (package manager binary, command installing smartmontools + dmidecode)
_PACKAGE_COMMANDS = [
    ("apt-get", "sudo apt-get update && sudo apt-get install -y smartmontools dmidecode"),
    ("dnf", "sudo dnf install -y smartmontools dmidecode"),
    ("pacman", "sudo pacman -S --needed --noconfirm smartmontools dmidecode"),
    ("zypper", "sudo zypper --non-interactive install smartmontools dmidecode"),
    ("apk", "sudo apk add smartmontools dmidecode"),
]

# (terminal binary, arguments that go before the command to run)
_TERMINALS = [
    ("x-terminal-emulator", ["-e"]),   # Debian/Ubuntu: the user's chosen terminal
    ("gnome-terminal", ["--"]),
    ("ptyxis", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("mate-terminal", ["-x"]),
    ("terminator", ["-x"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("foot", []),
    ("wezterm", ["start", "--"]),
    ("xterm", ["-e"]),
]


def package_install_command() -> str | None:
    for binary, command in _PACKAGE_COMMANDS:
        if shutil.which(binary):
            return command
    return None


def terminal_command(script: str) -> list[str] | None:
    """argv that opens a terminal window running `script` in bash, or None."""
    for binary, prefix in _TERMINALS:
        path = shutil.which(binary)
        if path:
            return [path, *prefix, "bash", "-c", script]
    return None


def setup_script(helper_source: str, username: str) -> str:
    """Bash script for the setup. The sudo rule is syntax-checked *before* it is
    installed, so a broken rule can never lock the user out of sudo."""
    rule = f"{username} ALL=(root) NOPASSWD: {HELPER_TARGET} *"
    install_packages = package_install_command()
    if install_packages:
        packages_step = (
            f"{install_packages} || echo 'Warnung: smartmontools/dmidecode konnten nicht "
            "installiert werden - bitte mit der Paketverwaltung nachinstallieren.'"
        )
    else:
        packages_step = ("echo 'Unbekannte Paketverwaltung: bitte smartmontools und "
                         "dmidecode selbst installieren.'")
    return "\n".join([
        "echo '== WinTaskManager: Hardwaredaten einrichten =='",
        packages_step,
        'tmp="$(mktemp)"',
        f"printf '%s\\n' {shlex.quote(rule)} > \"$tmp\"",
        "if sudo install -d -m 0755 /usr/local/libexec"
        f" && sudo install -m 0755 {shlex.quote(helper_source)} {HELPER_TARGET}"
        ' && sudo visudo -cf "$tmp"'
        f' && sudo install -m 0440 -o root -g root "$tmp" {SUDOERS_TARGET}; then',
        "    echo; echo 'Fertig: SMART- und RAM-Daten können jetzt gelesen werden.'",
        "else",
        "    echo; echo 'Einrichtung fehlgeschlagen - es wurde keine sudo-Regel installiert.'",
        "fi",
        'rm -f "$tmp"',
        "echo; read -r -p 'Zum Schließen Enter drücken ' _",
    ])
