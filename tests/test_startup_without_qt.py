"""Qt can be installed while a system library it needs is missing. main.py then has to
say what to do instead of ending in a traceback."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class TestStartWithoutQt(unittest.TestCase):
    def test_failure_names_the_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "PySide6"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "QtWidgets.py").write_text(
                'raise ImportError("libEGL.so.1: cannot open shared object file")\n')
            result = subprocess.run(
                [sys.executable, str(REPO / "main.py")],
                capture_output=True, text=True, cwd=directory,
                env={**os.environ, "PYTHONPATH": directory, "LANG": "C", "LANGUAGE": "C",
                     "XDG_CACHE_HOME": directory})
        self.assertEqual(result.returncode, 1)
        self.assertIn("libEGL.so.1", result.stderr)
        self.assertIn("install.sh", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
