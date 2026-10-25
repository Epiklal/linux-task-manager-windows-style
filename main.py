#!/usr/bin/env python3
"""Entry point for the Windows-10-style Task Manager."""
import faulthandler
import os
import signal
import sys

from app.remote_server import server_from_args


def _enable_stack_dumps() -> None:
    """`kill -USR1 <pid>` appends a traceback of every thread to the log file,
    e.g. to see where a frozen GUI thread is stuck."""
    cache_dir = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    log_dir = os.path.join(cache_dir, "WinTaskManager")
    try:
        os.makedirs(log_dir, exist_ok=True)
        # faulthandler writes to the raw fd, so the file must stay open for the whole run.
        log_file = open(os.path.join(log_dir, "stacktrace.log"), "a", encoding="utf-8")
    except OSError:
        log_file = sys.stderr
    faulthandler.register(signal.SIGUSR1, file=log_file, all_threads=True)


def main() -> int:
    _enable_stack_dumps()
    if "--server" in sys.argv[1:]:
        server_args = [arg for arg in sys.argv[1:] if arg != "--server"]
        server_from_args(server_args)
        return 0
    from PySide6.QtWidgets import QApplication
    from app.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Task Manager")
    # Matches wintaskmanager.desktop so the panel shows the right icon/name.
    app.setDesktopFileName("wintaskmanager")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
