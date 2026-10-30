#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

PYTHON_BIN=""
if [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
    echo "The application is not installed yet. Please run ./install.sh first." >&2
    exit 1
fi

if [[ "$PYTHON_BIN" == *".venv/bin/python" ]]; then
    if [[ ! -f "$PYTHON_BIN" ]]; then
        echo "The virtual environment is damaged or missing. Please run ./install.sh first." >&2
        exit 1
    fi
    if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util
for name in ("PySide6", "psutil"):
    if importlib.util.find_spec(name) is None:
        raise SystemExit(1)
PY
    then
        echo "The virtual environment is incomplete. Please run ./install.sh first." >&2
        exit 1
    fi
else
    if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util
for name in ("PySide6", "psutil"):
    if importlib.util.find_spec(name) is None:
        raise SystemExit(1)
PY
    then
        echo "The Python dependencies are missing. Please run ./install.sh first." >&2
        exit 1
    fi
fi

# PySide6 (Qt) supports both native Wayland and XCB (X11) platform plugins.
# Let Qt auto-detect the session; QT_QPA_PLATFORM can be forced if needed:
#   QT_QPA_PLATFORM=wayland ./run.sh
#   QT_QPA_PLATFORM=xcb ./run.sh
exec "$PYTHON_BIN" main.py "$@"
