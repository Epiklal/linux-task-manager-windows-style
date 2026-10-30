"""Keeps every test away from the real settings of whoever runs them: QSettings and
QStandardPaths read XDG_CONFIG_HOME, and importing this package comes before any test
module creates a Settings object."""
import atexit
import os
import tempfile

_config = tempfile.TemporaryDirectory(prefix="lintask-tests-")
atexit.register(_config.cleanup)
os.environ["XDG_CONFIG_HOME"] = _config.name
