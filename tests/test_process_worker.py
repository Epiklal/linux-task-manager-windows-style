"""The process list must survive a clock correction: NTP changes the boot time psutil
derives process start times from, and is_running() then reports every process object
created before the correction as gone."""
import os
import unittest
from unittest import mock

import psutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.system_info import ProcessWorker


class TestUserColumn(unittest.TestCase):
    def rows(self):
        worker = ProcessWorker()
        worker._sync_process_table()
        return worker, {row.pid: row for row in worker._collect_rows()}

    def test_own_process_shows_its_user(self):
        _, rows = self.rows()
        self.assertEqual(rows[os.getpid()].username, psutil.Process().username())

    def test_user_stays_after_a_clock_correction(self):
        worker, _ = self.rows()
        with mock.patch.object(psutil.Process, "is_running", return_value=False):
            rows = {row.pid: row for row in worker._collect_rows()}
        self.assertEqual(rows[os.getpid()].username, psutil.Process().username())


if __name__ == "__main__":
    unittest.main()
