"""The .deb's behaviour when PySide6 can't be set up. The scripts run as temporary
copies with a PATH of stubs only, so nothing here touches the real system.
"""
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEBIAN = REPO / "packaging" / "debian"
SETUP_VENV = "/usr/libexec/wintaskmanager/setup-venv"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ScriptTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_script(self, path: Path, *args, env=None):
        # stderr is a pipe here, exactly like a start from the desktop menu.
        return subprocess.run(["/bin/sh", str(path), *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              env={"PATH": str(self.root), "LANG": "C", **(env or {})})


class TestStarterWithoutPySide6(ScriptTestCase):
    def setUp(self):
        super().setUp()
        text = ((DEBIAN / "wintaskmanager").read_text()
                .replace("/opt/wintaskmanager/venv", f"{self.root}/missing")
                .replace("/usr/bin/python3", f"{self.root}/missing/python3"))
        self.starter = self.root / "starter"
        self.starter.write_text(text)

    def add_dialog(self, name: str) -> Path:
        log = self.root / f"{name}.log"
        write_executable(self.root / name, f'#!/bin/sh\necho "$@" > {log}\n')
        return log

    def test_failure_names_the_fix(self):
        result = self.run_script(self.starter)
        self.assertEqual(result.returncode, 1)
        self.assertIn(SETUP_VENV, result.stderr)

    def test_menu_start_shows_a_dialog(self):
        for variable in ("DISPLAY", "WAYLAND_DISPLAY"):
            with self.subTest(variable=variable):
                log = self.add_dialog("zenity")
                result = self.run_script(self.starter, env={variable: ":0"})
                self.assertEqual(result.returncode, 1)
                self.assertIn(SETUP_VENV, log.read_text())
                log.unlink()

    def test_other_dialog_tools_are_used_too(self):
        for name in ("kdialog", "xmessage"):
            with self.subTest(name=name):
                log = self.add_dialog(name)
                self.run_script(self.starter, env={"DISPLAY": ":0"})
                self.assertIn(SETUP_VENV, log.read_text())
                (self.root / name).unlink()

    def test_no_dialog_without_a_display(self):
        log = self.add_dialog("zenity")
        self.assertEqual(self.run_script(self.starter).returncode, 1)
        self.assertFalse(log.exists())

    def test_missing_dialog_tools_are_not_an_error(self):
        result = self.run_script(self.starter, env={"DISPLAY": ":0"})
        self.assertEqual(result.returncode, 1)
        self.assertIn(SETUP_VENV, result.stderr)


class TestPostinst(ScriptTestCase):
    def configure(self, setup_rc: int):
        stub = self.root / "setup-venv"
        write_executable(stub, f"#!/bin/sh\nexit {setup_rc}\n")
        postinst = self.root / "postinst"
        text = (DEBIAN / "postinst").read_text()
        self.assertIn(SETUP_VENV, text)
        postinst.write_text(text.replace(SETUP_VENV, str(stub)))
        return self.run_script(postinst, "configure")

    def test_failed_download_does_not_leave_apt_blocked(self):
        result = self.configure(setup_rc=1)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Warning", result.stderr)

    def test_warning_follows_the_locale(self):
        stub = self.root / "setup-venv-stub"
        write_executable(stub, "#!/bin/sh\nexit 1\n")
        postinst = self.root / "postinst"
        postinst.write_text((DEBIAN / "postinst").read_text().replace(SETUP_VENV, str(stub)))
        result = self.run_script(postinst, "configure", env={"LANG": "de_DE.UTF-8"})
        self.assertIn("Warnung", result.stderr)

    def test_successful_setup_is_silent(self):
        result = self.configure(setup_rc=0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")


class TestControlFile(unittest.TestCase):
    def field(self, name: str) -> str:
        text = (DEBIAN / "control.in").read_text()
        return re.search(rf"^{name}: (.*)$", text, re.MULTILINE).group(1)

    def test_pyside6_stays_a_recommendation(self):
        # Ubuntu and Linux Mint have no such packages: as a Depends, the .deb could
        # not be installed there at all. setup-venv covers the missing packages.
        self.assertIn("python3-pyside6", self.field("Recommends"))
        self.assertNotIn("pyside6", self.field("Depends").lower())

    def test_virtual_environment_can_be_created(self):
        self.assertIn("python3-venv", self.field("Depends"))


if __name__ == "__main__":
    unittest.main()
