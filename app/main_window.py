"""Main window: top tab bar + pages, styled to resemble the real Windows 10 Task Manager
(tabs across the top, status bar with process/thread counts, switchable light/dark theme)."""
from __future__ import annotations

import re
from pathlib import Path
from string import Template

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenuBar,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app.i18n import set_language, tr
from app.settings import Settings
from pages.autostart_page import AutostartPage
from pages.benchmarks_page import BenchmarksPage
from pages.docker_page import DockerPage
from pages.performance_page import PerformancePage
from pages.processes_page import ProcessesPage
from pages.sensors_page import SensorsPage
from pages.services_page import ServicesPage
from pages.settings_page import SettingsPage
from app.themes import (
    SCHEMES,
    base_palette,
    get_scheme,
    widget_colors,
    win10_color_map,
    win11_palette,
)
from widgets.nav_tab_bar import NavTabWidget

# Windows 10 Task Manager look, layered on top of the base palette: white window,
# classic tabs, Segoe UI 9pt, grey Win32 buttons, light-blue selection. The dark
# theme and the extra colour schemes swap every colour via app.themes.win10_color_map.
_WIN10_QSS = """
QMainWindow, QWidget {
    font-family: "Segoe UI", "Noto Sans", "Cantarell", sans-serif;
    font-size: 12px;
}
QMenuBar {
    background-color: #ffffff;
    padding: 1px 0;
}
QMenuBar::item {
    background: transparent;
    padding: 3px 8px;
    border: 1px solid transparent;
}
QMenuBar::item:selected {
    background-color: #e5f3ff;
    border-color: #cce8ff;
}
QMenuBar::item:pressed {
    background-color: #cce8ff;
    border-color: #99d1ff;
}
QMenu {
    background-color: #f2f2f2;
    border: 1px solid #cccccc;
    padding: 2px;
}
QMenu::item {
    padding: 4px 24px 4px 28px;
    background: transparent;
}
QMenu::item:selected {
    background-color: #91c9f7;
}
QMenu::separator {
    height: 1px;
    background: #d7d7d7;
    margin: 3px 4px 3px 28px;
}
QTabWidget::pane {
    border: none;
    border-top: 1px solid #d9d9d9;
}
QTabBar::tab {
    background: transparent;
    color: #000000;
    padding: 4px 10px 5px 10px;
    margin-top: 2px;
    border: 1px solid transparent;
    border-bottom: none;
    font-weight: normal;
}
QTabBar::tab:first {
    margin-left: 2px;
}
QTabBar::tab:selected {
    background-color: #ffffff;
    color: #000000;
    border: 1px solid #d9d9d9;
    border-bottom: 1px solid #ffffff;
    font-weight: normal;
}
QTabBar::tab:hover:!selected {
    background-color: #e5f3ff;
    border-color: #cce8ff;
}
QStatusBar {
    background-color: #ffffff;
    border-top: 1px solid #e5e5e5;
    color: #000000;
}
QStatusBar::item {
    border: none;
}
QTableView, QTableWidget {
    border: none;
    selection-background-color: #cce8ff;
    selection-color: #000000;
}
QHeaderView::section {
    background-color: #ffffff;
    color: #4d4d4d;
    padding: 4px 6px;
    border: none;
    border-right: 1px solid #e5e5e5;
    border-bottom: 1px solid #e5e5e5;
}
QHeaderView::section:hover {
    background-color: #d9ebf9;
}
QLineEdit, QSpinBox, QComboBox {
    border: 1px solid #7a7a7a;
    border-radius: 0;
    padding: 3px 5px;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: #0078d7;
}
QComboBox::drop-down {
    border-left: none;
}
QPushButton {
    background-color: #e1e1e1;
    border: 1px solid #adadad;
    border-radius: 0;
    padding: 4px 14px;
    min-width: 60px;
    color: #000000;
}
QPushButton:hover {
    background-color: #e5f1fb;
    border-color: #0078d7;
}
QPushButton:pressed, QPushButton:checked {
    background-color: #cce4f7;
    border-color: #005499;
}
QPushButton:disabled {
    background-color: #cccccc;
    border-color: #bfbfbf;
    color: #838383;
}
QScrollBar:vertical {
    background-color: #f0f0f0;
    width: 17px;
}
QScrollBar::handle:vertical {
    background-color: #cdcdcd;
    min-height: 28px;
    margin: 0 2px;
}
QScrollBar::handle:vertical:hover {
    background-color: #a6a6a6;
}
QScrollBar:horizontal {
    background-color: #f0f0f0;
    height: 17px;
}
QScrollBar::handle:horizontal {
    background-color: #cdcdcd;
    min-width: 28px;
    margin: 2px 0;
}
QScrollBar::handle:horizontal:hover {
    background-color: #a6a6a6;
}
QWidget#perfSidebar {
    border-right: 1px solid #e5e5e5;
}
QWidget#resourceTile {
    background-color: #ffffff;
    border: none;
}
QWidget#resourceTile:hover {
    background-color: #e5f3ff;
}
QWidget#resourceTile[selected="true"] {
    background-color: #cce8ff;
}
QWidget#resourceTile QLabel {
    background: transparent;
}
QLabel#tileTitle {
    font-size: 14px;
    font-weight: normal;
    color: #000000;
}
QLabel#tileValue {
    font-size: 12px;
    color: #4c4c4c;
}
QLabel#detailTitle {
    font-size: 26px;
    font-weight: 300;
}
QLabel#detailSubtitle {
    font-size: 15px;
}
QLabel#chartAxis, QLabel#statLabel, QLabel#statKey {
    color: #707070;
    font-size: 12px;
}
QLabel#statValue {
    font-size: 20px;
}
QLabel#statKeyValue {
    color: #000000;
}
"""


# Windows 11 (22H2+) Task Manager: Mica-grey navigation pane, lighter content area,
# rounded controls, accent colour, Segoe UI Variable.
_WIN11_QSS = Template("""
QMainWindow, QWidget {
    font-family: "Segoe UI Variable Text", "Segoe UI", "Noto Sans", "Cantarell", sans-serif;
    font-size: 13px;
    background-color: $content;
    color: $text;
}
QMainWindow, QTabWidget#mainTabs {
    background-color: $nav;
}
QTabWidget#mainTabs::pane {
    border: none;
    border-left: 1px solid $border;
    background-color: $content;
}
QWidget#pageHeader {
    background-color: $content;
}
QLabel#pageTitle {
    font-size: 20px;
    font-weight: 600;
}
QMenu {
    background-color: $card;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 24px 6px 28px;
    border-radius: 4px;
    margin: 1px 2px;
    background: transparent;
}
QMenu::item:selected {
    background-color: $hover;
}
QMenu::separator {
    height: 1px;
    background: $border_strong;
    margin: 4px 8px;
}
QTableView, QTableWidget {
    background-color: $content;
    alternate-background-color: $content;
    border: none;
    gridline-color: $border;
    selection-background-color: $selection;
    selection-color: $text;
}
QHeaderView::section {
    background-color: $content;
    color: $sub;
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid $border_strong;
}
QHeaderView::section:hover {
    background-color: $hover;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background-color: $card;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 5px 8px;
    color: $text;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {
    border-bottom: 2px solid $accent;
}
QComboBox::drop-down {
    border: none;
    width: 26px;
}
QComboBox::down-arrow {
    image: url("$chevron");
    width: 12px;
    height: 12px;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 0;
    border: none;
}
QComboBox QAbstractItemView {
    background-color: $card;
    border: 1px solid $border_strong;
    border-radius: 6px;
    selection-background-color: $hover;
    selection-color: $text;
}
QPushButton {
    background-color: $card;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 6px 14px;
    color: $text;
}
QPushButton:hover {
    background-color: $hover;
}
QPushButton:pressed {
    background-color: $pressed;
    color: $sub;
}
QPushButton:checked, QPushButton#accentButton {
    background-color: $accent;
    border-color: $accent;
    color: $on_accent;
}
QPushButton:disabled {
    color: $disabled;
    background-color: $content;
    border-color: $border;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
}
QRadioButton::indicator {
    border-radius: 8px;
}
QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {
    background-color: $card;
    border: 1px solid $scroll;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: $accent;
    border: 1px solid $accent;
}
QGroupBox {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 8px;
    margin-top: 26px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 4px;
    padding: 0 0 6px 0;
    background: transparent;
}
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {
    background: transparent;
    font-weight: normal;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background-color: $scroll;
    border-radius: 3px;
    min-height: 30px;
    margin: 0 2px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background-color: $scroll;
    border-radius: 3px;
    min-width: 30px;
    margin: 2px 0;
}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
    border: none;
    width: 0;
    height: 0;
}
QWidget#perfSidebar {
    background-color: $content;
    border-right: 1px solid $border;
}
QWidget#resourceTile {
    background-color: transparent;
    border: none;
    border-radius: 6px;
}
QWidget#resourceTile:hover {
    background-color: $hover;
}
QWidget#resourceTile[selected="true"] {
    background-color: $tile_selected;
}
QWidget#resourceTile QLabel {
    background: transparent;
}
QLabel#tileTitle {
    font-size: 14px;
    font-weight: normal;
    color: $text;
}
QLabel#tileValue {
    font-size: 12px;
    color: $sub;
}
QLabel#detailTitle {
    font-size: 28px;
    font-weight: 600;
}
QLabel#detailSubtitle {
    font-size: 14px;
    color: $sub;
}
QLabel#chartAxis, QLabel#statLabel, QLabel#statKey {
    color: $sub;
    font-size: 12px;
}
QLabel#statValue {
    font-size: 20px;
    font-weight: 600;
}
QLabel#statKeyValue {
    color: $text;
}
""")


_ICON_DIR = Path(__file__).resolve().parent.parent / "widgets" / "icons"


def build_qss(theme: str, style: str = "win10") -> str:
    scheme = get_scheme(theme)
    base = _build_base_qss(base_palette(scheme))
    if style == "win11":
        icon = "dark" if scheme.dark else "light"
        chevron = (_ICON_DIR / f"chevron_down_{icon}.svg").as_posix()
        return f"{base}\n{_WIN11_QSS.substitute(win11_palette(scheme), chevron=chevron)}"
    color_map = win10_color_map(scheme)
    win10 = re.sub(r"#[0-9a-fA-F]{6}",
                   lambda m: color_map.get(m.group(0).lower(), m.group(0)), _WIN10_QSS)
    return f"{base}\n{win10}"


# (icon, page title) per main tab, used by the Windows 11 navigation pane.
_NAV_ICONS = ["processes", "performance", "sensors", "services", "docker", "autostart",
              "benchmarks", "settings"]


class PageFrame(QWidget):
    """Wraps a page with the Windows 11 page header: a title and command buttons.
    In the Windows 10 style the header is hidden and the page looks unchanged."""

    def __init__(self, title: str, page: QWidget, actions: list[QWidget] | None = None,
                 parent=None):
        super().__init__(parent)
        self.page = page
        self.header = QWidget()
        self.header.setObjectName("pageHeader")
        self.header_layout = header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(20, 14, 16, 10)
        header_layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)
        for action in actions or []:
            header_layout.addWidget(action)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(page, 1)
        self.header.setVisible(False)


def _build_base_qss(p: dict) -> str:
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
QComboBox::drop-down {{
    width: 24px;
    border: none;
    border-left: 1px solid {p['input_border']};
}}
QComboBox QAbstractItemView {{
    background-color: {p['input_bg']};
    color: {p['text']};
    selection-background-color: {p['sel_bg']};
    selection-color: {p['sel_fg']};
    border: 1px solid {p['input_border']};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
}}
QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {{
    background-color: {p['input_bg']};
    border: 1px solid {p['input_border']};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: #0078d4;
    border: 1px solid #0078d4;
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
QScrollBar:vertical {{
    background-color: {p['bg']};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {p['border']};
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: #999999;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar:horizontal, QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal, QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: none;
    border: none;
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
QLabel#detailSubtitle {{
    font-size: 15px;
}}
QLabel#chartAxis, QLabel#statLabel, QLabel#statKey {{
    color: {p['tile_value']};
    font-size: 11px;
}}
QLabel#statValue {{
    font-size: 19px;
}}
QLabel#statKeyValue {{
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
        QApplication.setStyle("Fusion")
        super().__init__()
        self.resize(1000, 700)

        self.settings = Settings()
        self._always_on_top = False
        set_language(self.settings.language)

        self._build_ui()

    def _build_ui(self) -> None:
        """Creates everything that shows text; runs again when the language changes."""
        self.setWindowTitle(tr("Task Manager"))

        self.tabs = NavTabWidget()

        self.processes_page = ProcessesPage(self.settings)
        self.performance_page = PerformancePage(self.settings.smart_sudo_enabled)
        self.sensors_page = SensorsPage()
        self.services_page = ServicesPage()
        self.docker_page = DockerPage(self.settings)
        self.autostart_page = AutostartPage(self.settings)
        self.benchmarks_page = BenchmarksPage()
        self.settings_page = SettingsPage(self.settings)

        # Windows 11 shows these commands in the page header instead of menu/footer.
        processes_new_task = QPushButton(tr("Run new task"))
        processes_new_task.clicked.connect(self.processes_page.run_new_task)
        performance_new_task = QPushButton(tr("Run new task"))
        performance_new_task.clicked.connect(self.processes_page.run_new_task)
        pages = [
            (self.processes_page, tr("Processes"),
             [processes_new_task, self.processes_page.header_end_task_btn]),
            (self.performance_page, tr("Performance"), [performance_new_task]),
            (self.sensors_page, tr("Sensors"), None),
            (self.services_page, tr("Services"), None),
            (self.docker_page, "Docker", None),
            (self.autostart_page, tr("Autostart"), None),
            (self.benchmarks_page, tr("Benchmarks"), None),
            (self.settings_page, tr("Settings"), None),
        ]
        self.page_frames: list[PageFrame] = []
        for page, title, actions in pages:
            frame = PageFrame(title, page, actions)
            self.page_frames.append(frame)
            self.tabs.addTab(frame, title)
        self.tabs.nav_bar.icon_kinds = _NAV_ICONS

        self.setCentralWidget(self.tabs)

        self.status_label = QLabel()
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().addWidget(self.status_label, stretch=1)
        # Windows 10 keeps "End task" in the footer, bottom right.
        self.statusBar().addPermanentWidget(self.processes_page.end_task_btn)
        self.statusBar().setContentsMargins(8, 4, 10, 6)
        self.processes_page.statsChanged.connect(self._update_status)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._build_menu()

        self.settings_page.themeChanged.connect(self.apply_theme)
        self.settings_page.themeChanged.connect(self._sync_theme_actions)
        self.settings_page.styleChanged.connect(self._on_style_changed)
        self.settings_page.languageChanged.connect(self._on_language_changed)
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
        self.settings_page.remoteConnectionChanged.connect(
            self.performance_page.set_remote_connection
        )
        self.settings_page.remoteConnectionChanged.connect(
            self.benchmarks_page.set_remote_connection
        )
        self.performance_page.remoteSnapshotReady.connect(self._apply_remote_snapshot)

        self.processes_page.set_refresh_interval(self.settings.process_interval_ms / 1000)
        self.performance_page.set_refresh_interval(self.settings.performance_interval_ms)
        if self.settings.remote_enabled:
            self.performance_page.set_remote_connection(
                True,
                self.settings.remote_host,
                self.settings.remote_port,
                self.settings.remote_token,
            )
            self.benchmarks_page.set_remote_connection(
                True, self.settings.remote_host, self.settings.remote_port,
                self.settings.remote_token,
            )
        self.apply_theme(self.settings.theme)

    def _on_language_changed(self, language: str) -> None:
        # Deferred: the signal comes from a combo box inside the page being replaced.
        QTimer.singleShot(0, lambda: self._switch_language(language))

    def _switch_language(self, language: str) -> None:
        """Rebuilds all pages, menus and the status bar in the new language."""
        set_language(language)
        self.tabs.currentChanged.disconnect(self._on_tab_changed)
        self._shut_down_pages()
        self.setMenuBar(QMenuBar())
        self.setStatusBar(QStatusBar())
        self._build_ui()
        self.tabs.setCurrentWidget(self.page_frames[-1])

    def _build_menu(self) -> None:
        """File / Options / View, like the Windows 10 Task Manager."""
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu(tr("&File"))
        file_menu.addAction(tr("&Run new task"), self.processes_page.run_new_task)
        file_menu.addAction(tr("&Start file…"), self.processes_page.choose_and_start_file)
        file_menu.addSeparator()
        file_menu.addAction(tr("E&xit"), self.close)

        options_menu = menu_bar.addMenu(tr("&Options"))
        on_top = options_menu.addAction(tr("&Always on top"))
        on_top.setCheckable(True)
        on_top.setChecked(self._always_on_top)
        on_top.toggled.connect(self._set_always_on_top)

        view_menu = menu_bar.addMenu(tr("&View"))
        style_menu = view_menu.addMenu(tr("&Style"))
        style_group = QActionGroup(style_menu)
        for key, label in (("win10", "Windows &10"), ("win11", "Windows &11")):
            action = style_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.settings.style == key)
            action.triggered.connect(lambda _checked=False, k=key: self._choose_style(k))
            style_group.addAction(action)
        theme_menu = view_menu.addMenu(tr("&Color scheme"))
        self._theme_actions: dict[str, QAction] = {}
        group = QActionGroup(theme_menu)
        for key, scheme in SCHEMES.items():
            action = theme_menu.addAction(tr(scheme.name))
            action.setCheckable(True)
            action.setChecked(self.settings.theme == key)
            action.triggered.connect(lambda _checked=False, k=key: self._choose_theme(k))
            group.addAction(action)
            self._theme_actions[key] = action

    def _choose_style(self, style: str) -> None:
        combo = self.settings_page.style_combo
        combo.setCurrentIndex(combo.findData(style))

    def _on_style_changed(self, _style: str) -> None:
        self.apply_theme(self.settings.theme)

    def _choose_theme(self, theme: str) -> None:
        combo = self.settings_page.theme_combo
        combo.setCurrentIndex(combo.findData(theme))

    def _sync_theme_actions(self, theme: str) -> None:
        action = self._theme_actions.get(theme)
        if action is not None:
            action.setChecked(True)

    def _set_always_on_top(self, enabled: bool) -> None:
        self._always_on_top = enabled
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.show()

    def _on_tab_changed(self, index: int) -> None:
        self.processes_page.end_task_btn.setVisible(index == 0)

    def apply_theme(self, theme: str) -> None:
        style = self.settings.style
        win11 = style == "win11"
        colors = widget_colors(get_scheme(theme), style)
        self.setStyleSheet(f"{build_qss(theme, style)}\n{self.settings.custom_qss}")
        self.tabs.set_win11(win11, colors)
        # Windows 11 has neither menu bar nor status bar; its commands live in page headers.
        self.menuBar().setVisible(not win11)
        self.statusBar().setVisible(not win11)
        for frame in self.page_frames:
            frame.header.setVisible(win11)
        # Win11 puts the search box into the header, Win10 above the list.
        self.processes_page.place_search_box(
            self.page_frames[0].header_layout if win11 else None)
        self.performance_page.set_theme(colors["dark"], colors["chart_bg"], colors["charts"])
        self.processes_page.set_theme(colors, win11)

    def _apply_custom_qss(self, qss: str) -> None:
        self.settings.custom_qss = qss
        self.apply_theme(self.settings.theme)

    def _apply_remote_snapshot(self, payload: dict) -> None:
        if payload.get("remote_disabled"):
            self.processes_page.set_remote_rows(None)
            self.sensors_page.set_remote_sensors(None)
            self.services_page.set_remote_services(None)
            self.autostart_page.set_remote_autostart(None)
            self.docker_page.set_remote_docker(None)
            return
        self.processes_page.set_remote_rows(payload.get("processes"))
        self.sensors_page.set_remote_sensors(payload.get("sensors"))
        self.services_page.set_remote_services(payload.get("services"))
        self.autostart_page.set_remote_autostart(payload.get("autostart"))
        self.docker_page.set_remote_docker(
            payload.get("docker"), payload.get("docker_error", "")
        )

    def _update_status(self, process_count: int, thread_count: int) -> None:
        self.status_label.setText(
            tr("Processes: {processes}     Threads: {threads}",
               processes=process_count, threads=thread_count)
        )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._shut_down_pages()
        super().closeEvent(event)

    def _shut_down_pages(self) -> None:
        self.processes_page.shutdown()
        self.performance_page.shutdown()
        self.sensors_page.shutdown()
        self.services_page.shutdown()
        self.docker_page.shutdown()
        self.autostart_page.shutdown()
        self.benchmarks_page.shutdown()
