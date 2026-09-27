"""Smoke test: importing pages/widgets and constructing the main window must not raise,
even in a headless environment (uses the offscreen Qt platform plugin).
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.main_window import MainWindow  # noqa: E402


def _stopped(thread) -> bool:
    # Shutdown deletes finished workers (deleteLater), which also counts as stopped.
    return thread is None or not shiboken6.isValid(thread) or not thread.isRunning()


class TestStartup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_constructs_and_shows(self):
        window = MainWindow()
        window.show()
        self.assertEqual(window.tabs.count(), 8)
        window.close()

    def test_main_window_closes_without_leftover_worker_threads(self):
        window = MainWindow()
        window.show()
        QApplication.processEvents()

        threads = (
            window.processes_page.worker,
            window.performance_page._gpu_worker,
            window.performance_page._smart_worker,
            window.docker_page.worker,
        )
        for thread in threads:
            self.assertIsNotNone(thread)
            self.assertTrue(thread.isRunning())

        window.close()
        QApplication.processEvents()
        for _ in range(20):
            QApplication.processEvents()
            if all(_stopped(thread) for thread in threads):
                break
            self.app.thread().msleep(50)

        self.assertTrue(all(_stopped(thread) for thread in threads))
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
