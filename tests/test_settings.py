"""Settings file: taking it over from before the rename, and its permissions."""
import os
import stat
import tempfile
import unittest

from PySide6.QtCore import QSettings, QStandardPaths

from app.settings import APP, LEGACY_ORG, ORG, Settings

LEGACY_CONTENT = "[General]\ntheme=dark\nremote_token=old-token\n"


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestTestRunIsolation(unittest.TestCase):
    def test_settings_do_not_touch_the_real_config(self):
        real_config = os.path.join(os.path.expanduser("~"), ".config") + os.sep
        self.assertFalse(QSettings(ORG, APP).fileName().startswith(real_config))
        self.assertTrue(os.environ["XDG_CONFIG_HOME"].startswith(tempfile.gettempdir()))


class TestLegacyMigration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(self._restore_settings_path)
        self.config = directory.name
        for settings_format in (QSettings.IniFormat, QSettings.NativeFormat):
            QSettings.setPath(settings_format, QSettings.UserScope, self.config)
        self.legacy_folder = os.path.join(self.config, LEGACY_ORG)
        self.legacy = os.path.join(self.legacy_folder, f"{APP}.conf")
        self.path = os.path.join(self.config, ORG, f"{APP}.conf")

    @staticmethod
    def _restore_settings_path():
        config = QStandardPaths.writableLocation(QStandardPaths.GenericConfigLocation)
        for settings_format in (QSettings.IniFormat, QSettings.NativeFormat):
            QSettings.setPath(settings_format, QSettings.UserScope, config)

    def _write(self, path: str, content: str, mode: int = 0o644) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
        os.chmod(path, mode)

    def test_moved_values_can_be_read(self):
        self._write(self.legacy, LEGACY_CONTENT)
        settings = Settings()
        self.assertEqual(settings.theme, "dark")
        self.assertEqual(settings.remote_token, "old-token")

    def test_file_is_moved_and_locked_down(self):
        self._write(self.legacy, LEGACY_CONTENT)
        Settings()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(_mode(self.path), 0o600)
        self.assertFalse(os.path.exists(self.legacy_folder))

    def test_moved_values_survive_saving_and_the_next_start(self):
        self._write(self.legacy, LEGACY_CONTENT)
        settings = Settings()
        settings.theme = "light"
        settings._store.sync()
        self.assertEqual(_mode(self.path), 0o600)
        reopened = Settings()
        self.assertEqual(reopened.theme, "light")
        self.assertEqual(reopened.remote_token, "old-token")

    def test_existing_new_file_is_not_overwritten(self):
        self._write(self.legacy, LEGACY_CONTENT)
        self._write(self.path, "[General]\ntheme=light\n", 0o600)
        self.assertEqual(Settings().theme, "light")
        with open(self.legacy, encoding="utf-8") as file:
            self.assertEqual(file.read(), LEGACY_CONTENT)

    def test_other_files_in_the_old_folder_stay(self):
        self._write(self.legacy, LEGACY_CONTENT)
        other = os.path.join(self.legacy_folder, "other.conf")
        self._write(other, "keep")
        self.assertEqual(Settings().theme, "dark")
        self.assertTrue(os.path.exists(other))

    def test_first_start_without_old_file_creates_private_file(self):
        Settings()
        self.assertEqual(_mode(self.path), 0o600)
        self.assertFalse(os.path.exists(self.legacy_folder))


if __name__ == "__main__":
    unittest.main()
