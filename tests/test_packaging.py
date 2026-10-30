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
SETUP_VENV = "/usr/libexec/lintask/setup-venv"
MIGRATE = "/usr/libexec/lintask/migrate-wintaskmanager"


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
        text = ((DEBIAN / "lintask").read_text()
                .replace("/opt/lintask/venv", f"{self.root}/missing")
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
    def postinst(self, setup_rc=0, migrate_rc=0) -> Path:
        """postinst with stubs for the two helpers; the migration stub leaves a log."""
        write_executable(self.root / "setup-venv", f"#!/bin/sh\nexit {setup_rc}\n")
        write_executable(self.root / "migrate",
                         f'#!/bin/sh\necho run >> "{self.root}/migrate.log"\nexit {migrate_rc}\n')
        text = (DEBIAN / "postinst").read_text()
        self.assertIn(SETUP_VENV, text)
        self.assertIn(MIGRATE, text)
        path = self.root / "postinst"
        path.write_text(text.replace(SETUP_VENV, f"{self.root}/setup-venv")
                        .replace(MIGRATE, f"{self.root}/migrate"))
        return path

    def configure(self, setup_rc: int):
        return self.run_script(self.postinst(setup_rc=setup_rc), "configure")

    def test_failed_download_does_not_leave_apt_blocked(self):
        result = self.configure(setup_rc=1)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Warning", result.stderr)

    def test_warning_follows_the_locale(self):
        result = self.run_script(self.postinst(setup_rc=1), "configure",
                                 env={"LANG": "de_DE.UTF-8"})
        self.assertIn("Warnung", result.stderr)

    def test_successful_setup_is_silent(self):
        result = self.configure(setup_rc=0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_first_install_takes_over_the_old_settings(self):
        self.assertEqual(self.run_script(self.postinst(), "configure").returncode, 0)
        self.assertEqual((self.root / "migrate.log").read_text(), "run\n")

    def test_update_does_not_migrate_again(self):
        self.assertEqual(self.run_script(self.postinst(), "configure", "1.0.0").returncode, 0)
        self.assertFalse((self.root / "migrate.log").exists())

    def test_failed_migration_does_not_leave_apt_blocked(self):
        result = self.run_script(self.postinst(migrate_rc=1), "configure")
        self.assertEqual(result.returncode, 0)
        self.assertIn("WinTaskManager", result.stderr)


class TestMigration(ScriptTestCase):
    """migrate-wintaskmanager against a temporary root, with logging service tools."""

    SUDO_RULE = ("alice ALL=(root) NOPASSWD: "
                 "/usr/libexec/wintaskmanager/wintaskmanager-smartctl *\n")
    OLD_WANTS = "etc/systemd/system/multi-user.target.wants/wintaskmanager-server.service"

    def setUp(self):
        super().setUp()
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.tree = self.root / "tree"
        self.calls = self.root / "calls.log"
        self.calls.touch()
        for tool in ("deb-systemd-helper", "deb-systemd-invoke", "systemctl", "udevadm"):
            write_executable(self.stubs / tool, f'#!/bin/sh\necho "{tool} $*" >> "{self.calls}"\n')
        self.set_visudo(0)

    def set_visudo(self, rc: int):
        write_executable(self.stubs / "visudo", f"#!/bin/sh\nexit {rc}\n")

    def put(self, relative: str, content: str = "") -> Path:
        path = self.tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def migrate(self, returncode=0):
        result = self.run_script(DEBIAN / "migrate-wintaskmanager",
                                 env={"LINTASK_MIGRATE_ROOT": str(self.tree),
                                      "PATH": f"{self.stubs}:/usr/bin:/bin"})
        self.assertEqual(result.returncode, returncode, result.stderr)
        return self.calls.read_text().splitlines()

    def test_fresh_install_is_a_silent_no_op(self):
        result = self.run_script(DEBIAN / "migrate-wintaskmanager",
                                 env={"LINTASK_MIGRATE_ROOT": str(self.tree),
                                      "PATH": f"{self.stubs}:/usr/bin:/bin"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.calls.read_text(), "")

    def test_server_options_replace_the_shipped_defaults(self):
        old = self.put("etc/default/wintaskmanager-server",
                       'WINTASKMANAGER_SERVER_OPTS="--token secret"\n')
        old.chmod(0o600)
        new = self.put("etc/default/lintask-server", "# shipped\n")
        self.migrate()
        self.assertEqual(new.read_text(), 'LINTASK_SERVER_OPTS="--token secret"\n')
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o600)
        self.assertFalse(old.exists())
        self.assertEqual([p.name for p in new.parent.iterdir()], ["lintask-server"])

    def test_drop_ins_move_along(self):
        self.put("etc/systemd/system/wintaskmanager-server.service.d/port.conf", "[Service]\n")
        self.migrate()
        self.assertTrue((self.tree / "etc/systemd/system/lintask-server.service.d/port.conf").exists())
        self.assertFalse((self.tree / "etc/systemd/system/wintaskmanager-server.service.d").exists())

    def test_sudo_rule_follows_the_helper(self):
        old = self.put("etc/sudoers.d/wintaskmanager-smartctl", self.SUDO_RULE)
        self.migrate()
        new = self.tree / "etc/sudoers.d/lintask-smartctl"
        self.assertEqual(new.read_text(),
                         "alice ALL=(root) NOPASSWD: /usr/libexec/lintask/lintask-smartctl *\n")
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o440)
        self.assertFalse(old.exists())

    def test_sudo_rule_rejected_by_visudo_is_not_installed(self):
        self.set_visudo(1)
        old = self.put("etc/sudoers.d/wintaskmanager-smartctl", self.SUDO_RULE)
        self.migrate()
        self.assertEqual(list(old.parent.iterdir()), [])

    def test_rule_of_a_git_checkout_stays(self):
        text = "alice ALL=(root) NOPASSWD: /usr/local/libexec/wintaskmanager-smartctl *\n"
        old = self.put("etc/sudoers.d/wintaskmanager-smartctl", text)
        self.migrate()
        self.assertEqual(old.read_text(), text)
        self.assertEqual([p.name for p in old.parent.iterdir()], [old.name])

    def test_rapl_rule_moves_along(self):
        old = self.put("etc/udev/rules.d/99-wintaskmanager-rapl.rules", "rule\n")
        calls = self.migrate()
        self.assertEqual((old.parent / "99-lintask-rapl.rules").read_text(), "rule\n")
        self.assertFalse(old.exists())
        self.assertIn("udevadm control --reload", calls)

    def test_enabled_server_stays_enabled_without_a_running_systemd(self):
        self.put(self.OLD_WANTS).unlink()
        (self.tree / self.OLD_WANTS).symlink_to("/usr/lib/systemd/system/x.service")
        calls = self.migrate()
        self.assertIn("deb-systemd-helper enable lintask-server.service", calls)
        self.assertFalse([call for call in calls if call.startswith("deb-systemd-invoke")])

    def test_enabled_server_is_started_on_a_running_systemd(self):
        self.put(self.OLD_WANTS).unlink()
        (self.tree / self.OLD_WANTS).symlink_to("/usr/lib/systemd/system/x.service")
        (self.tree / "run/systemd/system").mkdir(parents=True)
        calls = self.migrate()
        self.assertIn("deb-systemd-invoke start lintask-server.service", calls)

    def test_disabled_server_stays_disabled_and_the_old_mask_goes(self):
        mask = self.tree / "etc/systemd/system/wintaskmanager-server.service"
        mask.parent.mkdir(parents=True)
        mask.symlink_to("/dev/null")
        calls = self.migrate()
        self.assertIn("deb-systemd-helper unmask wintaskmanager-server.service", calls)
        self.assertFalse([call for call in calls if "lintask-server" in call])


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

    def test_package_replaces_its_old_name(self):
        for name in ("Provides", "Conflicts", "Replaces"):
            with self.subTest(name=name):
                self.assertEqual(self.field(name), "wintaskmanager")


class TestServerUnit(unittest.TestCase):
    def test_options_under_the_old_name_keep_working(self):
        unit = (DEBIAN / "lintask-server.service").read_text()
        self.assertIn("$LINTASK_SERVER_OPTS", unit)
        self.assertIn("$WINTASKMANAGER_SERVER_OPTS", unit)


if __name__ == "__main__":
    unittest.main()
