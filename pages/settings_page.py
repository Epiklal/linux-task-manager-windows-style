"""Settings tab - theme and refresh-rate controls, applied live and persisted."""
from __future__ import annotations

import getpass
import shlex
from pathlib import Path

from PySide6.QtCore import QProcess, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.settings import Settings

THEME_LABELS = {"light": "Hell (Standard)", "dark": "Dunkel"}
QSS_TEMPLATES = {
    "Gesamte App": """QWidget {
    background-color: #191919;
    color: #f1f1f1;
    font-size: 13px;
}""",
    "Tab-Leiste": """QTabBar::tab {
    background-color: #191919;
    color: #c9c9c9;
    padding: 9px 16px;
}
QTabBar::tab:selected {
    color: #ffffff;
    border-bottom: 2px solid #00b7c3;
}""",
    "Schaltflächen": """QPushButton {
    background-color: #242424;
    color: #f1f1f1;
    border: 1px solid #393939;
    border-radius: 2px;
    padding: 6px 16px;
}
QPushButton:hover {
    background-color: #313131;
    border-color: #00b7c3;
}""",
    "Tabellen": """QTableView, QTableWidget {
    background-color: #1e1e1e;
    color: #f1f1f1;
    gridline-color: #303030;
    selection-background-color: #333333;
}
QHeaderView::section {
    background-color: #242424;
    color: #e6e6e6;
}""",
    "Leistungs-Kacheln": """QWidget#resourceTile {
    background-color: #191919;
    border-left: 3px solid transparent;
}
QWidget#resourceTile[selected=\"true\"] {
    background-color: #292929;
    border-left-color: #00b7c3;
}""",
    "Leistungs-Diagramme": """LiveChart {
    qproperty-lineColor: #00b7c3;
    qproperty-backgroundColor: #191919;
    qproperty-gridColor: #303030;
}""",
}


class SettingsPage(QWidget):
    themeChanged = Signal(str)
    processIntervalChanged = Signal(int)  # milliseconds
    performanceIntervalChanged = Signal(int)  # milliseconds
    smartSudoEnabled = Signal(bool)
    customQssChanged = Signal(str)

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings

        appearance_box = QGroupBox("Darstellung")
        self.theme_combo = QComboBox()
        for key, label in THEME_LABELS.items():
            self.theme_combo.addItem(label, key)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(settings.theme))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)

        appearance_form = QFormLayout(appearance_box)
        appearance_form.addRow("Farbschema:", self.theme_combo)

        speed_box = QGroupBox("Aktualisierungsgeschwindigkeit")
        self.process_spin = QSpinBox()
        self.process_spin.setRange(200, 5000)
        self.process_spin.setSingleStep(100)
        self.process_spin.setSuffix(" ms")
        self.process_spin.setValue(settings.process_interval_ms)
        self.process_spin.valueChanged.connect(self._on_process_interval_changed)

        self.performance_spin = QSpinBox()
        self.performance_spin.setRange(100, 5000)
        self.performance_spin.setSingleStep(100)
        self.performance_spin.setSuffix(" ms")
        self.performance_spin.setValue(settings.performance_interval_ms)
        self.performance_spin.valueChanged.connect(self._on_performance_interval_changed)

        speed_form = QFormLayout(speed_box)
        speed_form.addRow("Prozessliste:", self.process_spin)
        speed_form.addRow("Leistungsdiagramme:", self.performance_spin)

        smart_box = QGroupBox("Hardwaredaten (SMART und Arbeitsspeicher)")
        self.smart_setup_button = QPushButton("Hardwaredaten mit sudo einrichten")
        self.smart_setup_button.clicked.connect(self._setup_smart_access)
        self.smart_status = QLabel(self._smart_status_text())
        self.smart_status.setWordWrap(True)
        smart_form = QFormLayout(smart_box)
        smart_form.addRow(self.smart_setup_button)
        smart_form.addRow(self.smart_status)

        custom_qss_box = QGroupBox("Eigener Stil (QSS)")
        self.custom_qss_edit = QPlainTextEdit(settings.custom_qss)
        self.custom_qss_edit.setPlaceholderText(
            "QWidget { background-color: #191919; }\n"
            "QPushButton { border-radius: 0px; }"
        )
        self.custom_qss_edit.setMinimumHeight(190)
        self.qss_template_combo = QComboBox()
        self.qss_template_combo.addItem("Vorlage auswählen")
        self.qss_template_combo.addItems(QSS_TEMPLATES)
        self.insert_qss_template_button = QPushButton("Vorlage einfügen")
        self.insert_qss_template_button.clicked.connect(self._insert_qss_template)
        custom_qss_template_row = QHBoxLayout()
        custom_qss_template_row.addWidget(self.qss_template_combo, stretch=1)
        custom_qss_template_row.addWidget(self.insert_qss_template_button)
        self.apply_custom_qss_button = QPushButton("Stil anwenden")
        self.apply_custom_qss_button.clicked.connect(self._apply_custom_qss)
        self.reset_custom_qss_button = QPushButton("Stil zurücksetzen")
        self.reset_custom_qss_button.clicked.connect(self._reset_custom_qss)
        custom_qss_actions = QHBoxLayout()
        custom_qss_actions.addWidget(self.apply_custom_qss_button)
        custom_qss_actions.addWidget(self.reset_custom_qss_button)
        custom_qss_layout = QVBoxLayout(custom_qss_box)
        custom_qss_layout.addLayout(custom_qss_template_row)
        custom_qss_layout.addWidget(self.custom_qss_edit)
        custom_qss_layout.addLayout(custom_qss_actions)

        hint = QLabel(
            "Kleinere Werte aktualisieren schneller, benötigen aber etwas mehr CPU."
        )
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(appearance_box)
        layout.addWidget(speed_box)
        layout.addWidget(smart_box)
        layout.addWidget(custom_qss_box)
        layout.addWidget(hint)
        layout.addStretch(1)

    def _on_theme_changed(self, index: int) -> None:
        theme = self.theme_combo.itemData(index)
        self.settings.theme = theme
        self.themeChanged.emit(theme)

    def _on_process_interval_changed(self, value: int) -> None:
        self.settings.process_interval_ms = value
        self.processIntervalChanged.emit(value)

    def _on_performance_interval_changed(self, value: int) -> None:
        self.settings.performance_interval_ms = value
        self.performanceIntervalChanged.emit(value)

    def _apply_custom_qss(self) -> None:
        qss = self.custom_qss_edit.toPlainText()
        self.settings.custom_qss = qss
        self.customQssChanged.emit(qss)

    def _insert_qss_template(self) -> None:
        template = QSS_TEMPLATES.get(self.qss_template_combo.currentText())
        if not template:
            return
        editor = self.custom_qss_edit
        if editor.toPlainText().strip():
            editor.appendPlainText("")
        editor.appendPlainText(template)

    def _reset_custom_qss(self) -> None:
        self.custom_qss_edit.clear()
        self._apply_custom_qss()

    def _smart_status_text(self) -> str:
        if self.settings.smart_sudo_enabled:
            return "SMART wird mit sudo abgefragt. Die Freigabe läuft nach einigen Minuten ab."
        return "Für Temperatur, Startvorgänge und Gesamtdaten benötigt SMART meist sudo."

    def _setup_smart_access(self) -> None:
        helper_source = Path(__file__).resolve().parents[1] / "scripts" / "wintaskmanager-smartctl"
        username = shlex.quote(getpass.getuser())
        helper_path = shlex.quote(str(helper_source))
        command = (
            "sudo apt-get update && sudo apt-get install -y smartmontools && "
            "sudo install -d -m 0755 /usr/local/libexec && "
            f"sudo install -m 0755 {helper_path} /usr/local/libexec/wintaskmanager-smartctl && "
            f"printf '%s\\n' '{username} ALL=(root) NOPASSWD: /usr/local/libexec/wintaskmanager-smartctl *' | "
            "sudo tee /etc/sudoers.d/wintaskmanager-smartctl >/dev/null && "
            "sudo chmod 440 /etc/sudoers.d/wintaskmanager-smartctl && "
            "sudo visudo -cf /etc/sudoers.d/wintaskmanager-smartctl; "
            "echo 'Dauerhafter, eingeschränkter Zugriff auf SMART- und RAM-Daten eingerichtet. Dieses Terminal kann geschlossen werden.'; exec bash"
        )
        started = QProcess.startDetached("x-terminal-emulator", ["-e", "bash", "-lc", command])
        if not started:
            self.smart_status.setText("Terminal konnte nicht gestartet werden.")
            return
        self.smart_status.setText(
            "Im Terminal Passwort eingeben und den Abschluss abwarten. "
            "Danach den Button bei Bedarf erneut drücken, um sudo zu erneuern."
        )
        self.smartSudoEnabled.emit(True)
