"""One-time setup for SMART / RAM data: installs smartmontools + dmidecode, the
read-only helper script and a sudo rule that allows running only that helper.
Also makes the CPU energy counters (RAPL, Intel and AMD) readable for the user's
group - the kernel makes them root-only since Linux 5.10: right away, and on every
boot through a udev rule (systemd-udevd and eudev alike).

Works on every distribution the installer supports (apt, dnf, pacman, zypper, apk)
and with the common terminal emulators.
"""
from __future__ import annotations

import os
import shlex
import shutil

from app.i18n import tr

# Installed by the distribution packages: the .deb (build-deb.sh) under /usr/libexec,
# the Arch package (packaging/arch/PKGBUILD) under /usr/lib. Packages stay out of
# /usr/local, so the manual setup uses a different path.
PACKAGED_HELPERS = (
    "/usr/libexec/wintaskmanager/wintaskmanager-smartctl",
    "/usr/lib/wintaskmanager/wintaskmanager-smartctl",
)
HELPER_TARGET = "/usr/local/libexec/wintaskmanager-smartctl"
SUDOERS_TARGET = "/etc/sudoers.d/wintaskmanager-smartctl"
RAPL_UDEV_TARGET = "/etc/udev/rules.d/99-wintaskmanager-rapl.rules"
# intel-rapl (MSR), intel-rapl-mmio and intel-rapl-tpmi zones, also on AMD.
RAPL_ENERGY_GLOB = "/sys/class/powercap/intel-rapl*:*/energy_uj"
# udev runs RUN+= programs without a PATH, so they need absolute paths. Only system
# directories are searched; NixOS has no /bin/chgrp at all.
_SYSTEM_BIN_PATH = "/usr/bin:/bin:/run/current-system/sw/bin"

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


def helper_path() -> str:
    """The privileged helper to run via sudo: the packaged one if installed, otherwise
    the one the in-app setup copies to /usr/local."""
    for candidate in PACKAGED_HELPERS:
        if os.access(candidate, os.X_OK):
            return candidate
    return HELPER_TARGET


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


def _system_binary(name: str) -> str:
    return shutil.which(name, path=_SYSTEM_BIN_PATH) or f"/bin/{name}"


def rapl_udev_rule(group: str) -> str:
    """Grants `group` read access to the RAPL energy counters on every boot."""
    energy = "/sys$devpath/energy_uj"
    return ('ACTION=="add", SUBSYSTEM=="powercap", KERNEL=="intel-rapl*:*", '
            'TEST=="energy_uj", '
            f'RUN+="{_system_binary("chgrp")} {group} {energy}", '
            f'RUN+="{_system_binary("chmod")} g+r {energy}"')


def _echo(text: str) -> str:
    """Shell command that prints the translated `text`."""
    return "echo " + shlex.quote(tr(text))


def _rapl_steps(group: str) -> list[str]:
    """Bash lines that unlock the RAPL counters now and install the boot-time rule."""
    rule = rapl_udev_rule(group)
    # /etc is read-only on NixOS; the rule belongs into configuration.nix there.
    nix_option = f"services.udev.extraRules = ''{rule}'';"
    return [
        "shopt -s nullglob",
        f"rapl=({RAPL_ENERGY_GLOB})",
        'if [ "${#rapl[@]}" -eq 0 ]; then',
        "    " + _echo("No RAPL energy counter found: the kernel offers no power reading for this CPU."),
        f'elif sudo chgrp {shlex.quote(group)} "${{rapl[@]}}" && sudo chmod g+r "${{rapl[@]}}"; then',
        "    if [ -e /etc/NIXOS ]; then",
        "        " + _echo("Done until the next reboot. To make it permanent, add this to "
                           "/etc/nixos/configuration.nix:"),
        f"        printf '  %s\\n' {shlex.quote(nix_option)}",
        "    elif ! command -v udevadm >/dev/null; then",
        "        " + _echo("Done until the next reboot (no udev available, so no permanent rule)."),
        "    else",
        f"        printf '%s\\n' {shlex.quote(rule)} > \"$tmp\"",
        f'        if sudo install -m 0644 -o root -g root "$tmp" {RAPL_UDEV_TARGET}; then',
        "            sudo udevadm control --reload 2>/dev/null",
        "            " + _echo("Done: the CPU power draw can now be read."),
        "        else",
        "            " + _echo("Done until the next reboot - the udev rule could not be installed."),
        "        fi",
        "    fi",
        "else",
        "    " + _echo("The CPU power reading could not be enabled."),
        "fi",
    ]


def setup_script(helper_source: str, username: str, group: str) -> str:
    """Bash script for the setup. The sudo rule is syntax-checked *before* it is
    installed, so a broken rule can never lock the user out of sudo."""
    helper = helper_path()
    rule = f"{username} ALL=(root) NOPASSWD: {helper} *"
    if helper in PACKAGED_HELPERS:
        # The distribution package already ships the helper - only the sudo rule is missing.
        helper_step = "true"
    else:
        helper_step = ("sudo install -d -m 0755 /usr/local/libexec"
                       f" && sudo install -m 0755 {shlex.quote(helper_source)} {HELPER_TARGET}")
    install_packages = package_install_command()
    if install_packages:
        packages_step = f"{install_packages} || " + _echo(
            "Warning: smartmontools/dmidecode could not be installed - "
            "please install them with your package manager."
        )
    else:
        packages_step = _echo("Unknown package manager: please install smartmontools "
                              "and dmidecode yourself.")
    return "\n".join([
        _echo("== WinTaskManager: setting up hardware data =="),
        packages_step,
        'tmp="$(mktemp)"',
        f"printf '%s\\n' {shlex.quote(rule)} > \"$tmp\"",
        f"if {helper_step}"
        ' && sudo visudo -cf "$tmp"'
        f' && sudo install -m 0440 -o root -g root "$tmp" {SUDOERS_TARGET}; then',
        "    echo; " + _echo("Done: SMART and memory data can now be read."),
        "else",
        "    echo; " + _echo("Setup failed - no sudo rule was installed."),
        "fi",
        *_rapl_steps(group),
        'rm -f "$tmp"',
        "echo; read -r -p " + shlex.quote(tr("Press Enter to close ")) + " _",
    ])
