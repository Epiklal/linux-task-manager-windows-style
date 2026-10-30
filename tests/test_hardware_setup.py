"""The sudo setup and the root helper it allows. Nothing here needs or uses real sudo:
the generated script runs against a stub `sudo` that only records its arguments.
"""
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import hardware_setup
from app.hardware_setup import HELPER_TARGET, PACKAGED_HELPERS, RAPL_UDEV_TARGET, SUDOERS_TARGET

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "wintaskmanager-smartctl"
FAKE_SUDO = """#!/bin/sh
echo "$*" >> "$LOG_DIR/sudo.log"
if [ "$1" = visudo ]; then
    cp "$3" "$LOG_DIR/rule"
    exit "${VISUDO_RC:-0}"
fi
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def block_device() -> str | None:
    for name in sorted(os.listdir("/sys/class/block")) if os.path.isdir("/sys/class/block") else []:
        if os.path.exists(f"/dev/{name}") and stat.S_ISBLK(os.stat(f"/dev/{name}").st_mode):
            return name
    return None


class TestSudoRule(unittest.TestCase):
    def script(self, helper=HELPER_TARGET, source="/src/helper", user="alice", group="alice"):
        with mock.patch.object(hardware_setup, "helper_path", return_value=helper):
            return hardware_setup.setup_script(source, user, group)

    def test_rule_allows_only_the_helper(self):
        for helper in (*PACKAGED_HELPERS, HELPER_TARGET):
            with self.subTest(helper=helper):
                rule = f"alice ALL=(root) NOPASSWD: {helper} *"
                self.assertIn(shlex.quote(rule), self.script(helper))
                self.assertNotIn("NOPASSWD: ALL", self.script(helper))

    def test_rule_is_checked_before_it_is_installed(self):
        script = self.script()
        install = f'sudo install -m 0440 -o root -g root "$tmp" {SUDOERS_TARGET}'
        self.assertIn(f'sudo visudo -cf "$tmp" && {install}', script)

    def test_packaged_helper_is_not_overwritten(self):
        self.assertNotIn("-m 0755", self.script(PACKAGED_HELPERS[0]))
        self.assertIn(f"-m 0755 /src/helper {HELPER_TARGET}", self.script(HELPER_TARGET))

    def test_paths_and_names_are_shell_quoted(self):
        source = "/home/a b/$(touch pwned)/helper"
        group = "a b;touch pwned"
        script = self.script(source=source, group=group)
        self.assertIn(shlex.quote(source), script)
        self.assertIn(f"sudo chgrp {shlex.quote(group)} ", script)

    def test_generated_script_parses(self):
        subprocess.run(["bash", "-n"], input=self.script(), text=True, check=True)


class TestSetupScriptRun(unittest.TestCase):
    def run_script(self, visudo_rc=0):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        write_executable(root / "sudo", FAKE_SUDO)
        with mock.patch.object(hardware_setup, "helper_path", return_value=HELPER_TARGET):
            script = hardware_setup.setup_script(str(HELPER), "alice", "alice")
        result = subprocess.run(
            ["bash", "-c", script], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env={"PATH": f"{root}:/usr/bin:/bin", "LOG_DIR": str(root),
                 "VISUDO_RC": str(visudo_rc), "HOME": str(root)})
        return result, (root / "sudo.log").read_text().splitlines(), root

    def test_valid_rule_gets_installed(self):
        result, sudo_calls, root = self.run_script()
        self.assertIn("SMART- und RAM-Daten können jetzt gelesen werden", result.stdout)
        self.assertTrue(any(SUDOERS_TARGET in call for call in sudo_calls))
        self.assertEqual((root / "rule").read_text(),
                         f"alice ALL=(root) NOPASSWD: {HELPER_TARGET} *\n")

    def test_rule_rejected_by_visudo_is_not_installed(self):
        result, sudo_calls, _ = self.run_script(visudo_rc=1)
        self.assertIn("es wurde keine sudo-Regel installiert", result.stdout)
        self.assertFalse(any(SUDOERS_TARGET in call for call in sudo_calls))


class TestRaplRule(unittest.TestCase):
    def test_rule_uses_absolute_paths_and_read_only_access(self):
        rule = hardware_setup.rapl_udev_rule("alice")
        self.assertRegex(rule, r'RUN\+="/\S*chgrp alice ')
        self.assertRegex(rule, r'RUN\+="/\S*chmod g\+r ')
        self.assertNotIn("g+w", rule)
        self.assertNotIn("a+", rule)

    def test_rule_target_is_a_udev_rule(self):
        self.assertTrue(RAPL_UDEV_TARGET.startswith("/etc/udev/rules.d/"))


class TestHelperScript(unittest.TestCase):
    """The only program the sudo rule allows, so every argument is hostile until checked."""

    def run_helper(self, *args, script=HELPER):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True)

    def stubbed_helper(self):
        """The helper with its tools replaced, so a test can never reach the real ones."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for tool in ("dmidecode", "smartctl"):
            write_executable(root / tool, f'#!/bin/sh\necho {tool} "$@"\n')
        text = (HELPER.read_text()
                .replace("/usr/sbin/dmidecode /usr/bin/dmidecode", f"{root}/dmidecode")
                .replace("/usr/sbin/smartctl /usr/bin/smartctl", f"{root}/smartctl"))
        self.assertNotIn("/usr/sbin/dmidecode", text)
        self.assertNotIn("/usr/sbin/smartctl", text)
        script = root / "helper"
        script.write_text(text)
        return script

    def test_invalid_device_names_are_rejected(self):
        script = self.stubbed_helper()
        for device in ("", ".", "..", "../sda", "../../etc/passwd", "/dev/sda", "sda/", "sda;id",
                       "sda id", "$(id)", "`id`", "sda\nsda", "*", "--help", "--scan", "-a",
                       "does-not-exist", "null", "zero"):
            with self.subTest(device=device):
                result = self.run_helper(device, script=script)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Ungültiges Blockgerät", result.stderr)

    def test_no_argument_is_rejected(self):
        self.assertEqual(self.run_helper(script=self.stubbed_helper()).returncode, 2)

    def test_memory_only_reads_the_memory_table(self):
        result = self.run_helper("memory", "--dump-bin", "/tmp/x", script=self.stubbed_helper())
        self.assertEqual(result.stdout.split(), ["dmidecode", "-t", "memory"])

    def test_block_device_gets_fixed_smartctl_arguments(self):
        device = block_device()
        if device is None:
            self.skipTest("kein Blockgerät vorhanden")
        result = self.run_helper(device, "--scan", "-s", "on", script=self.stubbed_helper())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.split(), ["smartctl", "--all", "--json=o", f"/dev/{device}"])


if __name__ == "__main__":
    unittest.main()
