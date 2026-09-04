"""Tables where Ctrl+mouse-wheel grows/shrinks every row at once. Dragging individual
row borders via a visible (numbered) vertical header felt fiddly for tables full of
constantly refreshing data, so all rows are resized together instead."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableView, QTableWidget

_MIN_ROW_HEIGHT = 18
_MAX_ROW_HEIGHT = 60
_STEP = 2


class ResizableRowsMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._row_height_callback = None

    def set_row_height_callback(self, callback) -> None:
        """Registered callback(new_height: int) fires whenever Ctrl+wheel resizes rows,
        so callers can persist the chosen height (e.g. to app settings)."""
        self._row_height_callback = callback

    def set_default_row_height(self, height: int) -> None:
        """Apply a row height (e.g. one restored from settings) to all current rows."""
        height = max(_MIN_ROW_HEIGHT, min(_MAX_ROW_HEIGHT, height))
        header = self.verticalHeader()
        header.setDefaultSectionSize(height)
        model = self.model()
        row_count = model.rowCount() if model is not None else self.rowCount()
        for row in range(row_count):
            self.setRowHeight(row, height)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.ControlModifier:
            delta = _STEP if event.angleDelta().y() > 0 else -_STEP
            self._resize_all_rows(delta)
            event.accept()
            return
        super().wheelEvent(event)

    def _resize_all_rows(self, delta: int) -> None:
        header = self.verticalHeader()
        new_height = max(_MIN_ROW_HEIGHT, min(_MAX_ROW_HEIGHT, header.defaultSectionSize() + delta))
        header.setDefaultSectionSize(new_height)
        model = self.model()
        row_count = model.rowCount() if model is not None else self.rowCount()
        for row in range(row_count):
            self.setRowHeight(row, new_height)
        if self._row_height_callback is not None:
            self._row_height_callback(new_height)


class ResizableRowsTableView(ResizableRowsMixin, QTableView):
    pass


class ResizableRowsTableWidget(ResizableRowsMixin, QTableWidget):
    pass
