#!/usr/bin/env python3
"""Entry point for the Windows-10-style Task Manager."""
import sys

from app.remote_server import server_from_args


def main() -> int:
    if "--server" in sys.argv[1:]:
        server_args = [arg for arg in sys.argv[1:] if arg != "--server"]
        server_from_args(server_args)
        return 0
    from PySide6.QtWidgets import QApplication
    from app.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Task Manager")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
