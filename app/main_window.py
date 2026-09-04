"""Main window: top tab bar + pages, styled to resemble the real Windows 10 Task Manager
(tabs across the top, status bar with process/thread counts, switchable light/dark theme)."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget

from app.settings import Settings
from pages.autostart_page import AutostartPage
from pages.benchmarks_page import BenchmarksPage
from pages.docker_page import DockerPage
from pages.performance_page import PerformancePage
from pages.processes_page import ProcessesPage
from pages.sensors_page import SensorsPage
from pages.services_page import ServicesPage
from pages.settings_page import SettingsPage

# Palette: (window bg, pane border, tab hover, statusbar text, table bg, alt row,
#           selection bg, selection fg, header bg, header text, border,
#           input bg, input border, button hover bg, disabled text, tile bg, tile border)
_PALETTES = {
    "light": dict(
        bg="#f2f2f2", border="#d6d6d6", hover="#e6e6e6", status_text="#444444",
        table_bg="#ffffff", alt_row="#ffffff", sel_bg="#cce4f7", sel_fg="#000000",
        header_bg="#f2f2f2", header_text="#333333", input_bg="#ffffff",
        input_border="#c6c6c6", button_hover="#e5f1fb", disabled="#a0a0a0",
        tile_bg="#ffffff", tile_border="#e0e0e0", text="#1a1a1a", tab_text="#333333",
        tab_selected="#000000", tile_title="#333333", tile_value="#666666",
        detail_info="#444444",
    ),
    "dark": dict(
        bg="#191919", border="#303030", hover="#292929", status_text="#b8b8b8",
        table_bg="#1e1e1e", alt_row="#222222", sel_bg="#333333", sel_fg="#ffffff",
        header_bg="#242424", header_text="#e6e6e6", input_bg="#242424",
        input_border="#393939", button_hover="#313131", disabled="#777777",
        tile_bg="#191919", tile_border="#191919", text="#f1f1f1", tab_text="#c9c9c9",
        tab_selected="#ffffff", tile_title="#e6e6e6", tile_value="#b9b9b9",
        detail_info="#d0d0d0",
    ),
}


def build_qss(theme: str) -> str:
    p = _PALETTES[theme]
    return f"""
QMainWindow, QWidget {{
    background-color: {p['bg']};
    color: {p['text']};
    font-family: "Segoe UI", "Cantarell", sans-serif;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {p['border']};
    background-color: {p['bg']};
}}
QTabBar {{
    background-color: {p['bg']};
}}
QTabBar::tab {{
    background-color: {p['bg']};
    color: {p['tab_text']};
    padding: 9px 16px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {p['tab_selected']};
    border-bottom: 2px solid #0078d4;
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    background-color: {p['hover']};
}}
QStatusBar {{
    background-color: {p['bg']};
    border-top: 1px solid {p['border']};
    color: {p['status_text']};
}}
QTableView, QTableWidget {{
    background-color: {p['table_bg']};
    alternate-background-color: {p['alt_row']};
    gridline-color: {p['border']};
    selection-background-color: {p['sel_bg']};
    selection-color: {p['sel_fg']};
    border: 1px solid {p['border']};
}}
QHeaderView::section {{
    background-color: {p['header_bg']};
    color: {p['header_text']};
    padding: 6px;
    border: none;
    border-right: 1px solid {p['border']};
    border-bottom: 1px solid {p['border']};
}}
QLineEdit, QSpinBox, QComboBox {{
    background-color: {p['input_bg']};
    border: 1px solid {p['input_border']};
    border-radius: 2px;
    padding: 5px;
    color: {p['text']};
}}
QPushButton {{
    background-color: {p['input_bg']};
    border: 1px solid {p['input_border']};
    border-radius: 2px;
    padding: 6px 16px;
}}
QPushButton:hover {{
    background-color: {p['button_hover']};
    border-color: #0078d4;
}}
QPushButton:disabled {{
    color: {p['disabled']};
}}
QGroupBox {{
    border: 1px solid {p['border']};
    border-radius: 2px;
    margin-top: 10px;
    padding-top: 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}}
QLabel#tileTitle {{
    font-weight: 600;
    color: {p['tile_title']};
}}
QLabel#tileValue {{
    color: {p['tile_value']};
    font-size: 11px;
}}
QLabel#detailTitle {{
    font-size: 22px;
    font-weight: 500;
}}
QLabel#detailInfo {{
    color: {p['detail_info']};
}}
QWidget#resourceTile {{
    background-color: {p['tile_bg']};
    border: none;
    border-left: 3px solid transparent;
}}
QWidget#resourceTile[selected="true"] {{
    background-color: {p['hover']};
    border-left-color: #00b7c3;
}}
QPushButton:checked {{
    background-color: #333333;
    border-color: #00b7c3;
}}
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Task-Manager")
        self.resize(1000, 650)

        self.settings = Settings()

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.processes_page = ProcessesPage(self.settings)
        self.performance_page = PerformancePage(self.settings.smart_sudo_enabled)
        self.sensors_page = SensorsPage()
        self.services_page = ServicesPage()
        self.docker_page = DockerPage(self.settings)
        self.autostart_page = AutostartPage(self.settings)
        self.benchmarks_page = BenchmarksPage()
        self.settings_page = SettingsPage(self.settings)

        self.tabs.addTab(self.processes_page, "Prozesse")
        self.tabs.addTab(self.performance_page, "Leistung")
        self.tabs.addTab(self.sensors_page, "Sensoren")
        self.tabs.addTab(self.services_page, "Dienste")
        self.tabs.addTab(self.docker_page, "Docker")
        self.tabs.addTab(self.autostart_page, "Autostart")
        self.tabs.addTab(self.benchmarks_page, "Benchmarks")
        self.tabs.addTab(self.settings_page, "Einstellungen")

        self.setCentralWidget(self.tabs)

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label, stretch=1)
        self.processes_page.statsChanged.connect(self._update_status)

        self.settings_page.themeChanged.connect(self.apply_theme)
        self.settings_page.processIntervalChanged.connect(
            lambda ms: self.processes_page.set_refresh_interval(ms / 1000)
        )
        self.settings_page.performanceIntervalChanged.connect(
            self.performance_page.set_refresh_interval
        )
        self.settings_page.smartSudoEnabled.connect(
            self.performance_page.set_smart_sudo_enabled
        )
        self.settings_page.customQssChanged.connect(self._apply_custom_qss)

        self.processes_page.set_refresh_interval(self.settings.process_interval_ms / 1000)
        self.performance_page.set_refresh_interval(self.settings.performance_interval_ms)
        self.apply_theme(self.settings.theme)

    def apply_theme(self, theme: str) -> None:
        self.setStyleSheet(f"{build_qss(theme)}\n{self.settings.custom_qss}")
        self.performance_page.set_theme(theme == "dark")

    def _apply_custom_qss(self, qss: str) -> None:
        self.settings.custom_qss = qss
        self.apply_theme(self.settings.theme)

    def _update_status(self, process_count: int, thread_count: int) -> None:
        self.status_label.setText(
            f"Prozesse: {process_count}     Threads: {thread_count}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.processes_page.shutdown()
        self.performance_page.shutdown()
        self.sensors_page.shutdown()
        self.services_page.shutdown()
        self.docker_page.shutdown()
        self.autostart_page.shutdown()
        self.benchmarks_page.shutdown()
        super().closeEvent(event)
