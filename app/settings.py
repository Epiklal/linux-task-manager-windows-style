"""Persisted application settings (theme + refresh rates) via QSettings (INI backend)."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QSettings

ORG = "WinTaskManager"
APP = "TaskManager"

DEFAULT_THEME = "light"
DEFAULT_STYLE = "win10"  # "win10" or "win11" window layout / look
DEFAULT_PROCESS_INTERVAL_MS = 1000
DEFAULT_PERFORMANCE_INTERVAL_MS = 500
DEFAULT_REMOTE_HOST = "127.0.0.1"
DEFAULT_REMOTE_PORT = 8765


class Settings:
    def __init__(self):
        self._store = QSettings(ORG, APP)
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """The file holds the remote-server token: readable by this user only (0600).
        QSettings keeps the mode when it rewrites the file later."""
        path = self._store.fileName()
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
            else:
                # Create it empty with 0600 now, so it is never readable by others,
                # not even right after the very first start.
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError:
            pass

    @property
    def theme(self) -> str:
        return self._store.value("theme", DEFAULT_THEME, type=str)

    @theme.setter
    def theme(self, value: str) -> None:
        self._store.setValue("theme", value)

    @property
    def style(self) -> str:
        value = self._store.value("style", DEFAULT_STYLE, type=str)
        return value if value in ("win10", "win11") else DEFAULT_STYLE

    @style.setter
    def style(self, value: str) -> None:
        self._store.setValue("style", value)

    @property
    def process_interval_ms(self) -> int:
        return self._store.value(
            "process_interval_ms", DEFAULT_PROCESS_INTERVAL_MS, type=int
        )

    @process_interval_ms.setter
    def process_interval_ms(self, value: int) -> None:
        self._store.setValue("process_interval_ms", value)

    @property
    def performance_interval_ms(self) -> int:
        return self._store.value(
            "performance_interval_ms", DEFAULT_PERFORMANCE_INTERVAL_MS, type=int
        )

    @performance_interval_ms.setter
    def performance_interval_ms(self, value: int) -> None:
        self._store.setValue("performance_interval_ms", value)

    @property
    def remote_host(self) -> str:
        return self._store.value("remote_host", DEFAULT_REMOTE_HOST, type=str)

    @remote_host.setter
    def remote_host(self, value: str) -> None:
        self._store.setValue("remote_host", value)

    @property
    def remote_enabled(self) -> bool:
        return self._store.value("remote_enabled", False, type=bool)

    @remote_enabled.setter
    def remote_enabled(self, value: bool) -> None:
        self._store.setValue("remote_enabled", value)

    @property
    def remote_port(self) -> int:
        return self._store.value("remote_port", DEFAULT_REMOTE_PORT, type=int)

    @remote_port.setter
    def remote_port(self, value: int) -> None:
        self._store.setValue("remote_port", value)

    @property
    def remote_token(self) -> str:
        return self._store.value("remote_token", "", type=str)

    @remote_token.setter
    def remote_token(self, value: str) -> None:
        self._store.setValue("remote_token", value)
        self._store.sync()  # make sure the file exists before locking it down
        self._restrict_permissions()

    @property
    def smart_sudo_enabled(self) -> bool:
        return self._store.value("smart_sudo_enabled", False, type=bool)

    @smart_sudo_enabled.setter
    def smart_sudo_enabled(self, value: bool) -> None:
        self._store.setValue("smart_sudo_enabled", value)

    @property
    def custom_qss(self) -> str:
        return self._store.value("custom_qss", "", type=str)

    @custom_qss.setter
    def custom_qss(self, value: str) -> None:
        self._store.setValue("custom_qss", value)

    @property
    def docker_meta(self) -> dict:
        """Per-container UI metadata (favorite / custom name / icon path), keyed by
        the container's docker name so it survives across restarts and refreshes."""
        raw = self._store.value("docker_meta", "{}", type=str)
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def set_docker_meta(self, meta: dict) -> None:
        self._store.setValue("docker_meta", json.dumps(meta))

    def column_widths(self, table_key: str) -> list[int] | None:
        """Saved column widths for a table (e.g. "processes"), or None if never set."""
        raw = self._store.value(f"column_widths_{table_key}", "", type=str)
        if not raw:
            return None
        try:
            widths = json.loads(raw)
            return widths if isinstance(widths, list) else None
        except (TypeError, ValueError):
            return None

    def set_column_widths(self, table_key: str, widths: list[int]) -> None:
        self._store.setValue(f"column_widths_{table_key}", json.dumps(widths))

    def row_height(self, table_key: str, default: int) -> int:
        return self._store.value(f"row_height_{table_key}", default, type=int)

    def set_row_height(self, table_key: str, height: int) -> None:
        self._store.setValue(f"row_height_{table_key}", height)
