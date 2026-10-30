"""The language layer and the completeness of the German catalogue. The scans read the
source with `ast`, so a text only reached by a rarely used dialog is still covered.
"""
import ast
import os
import re
import string
import tempfile
import threading
import tokenize
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QStandardPaths, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QComboBox,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTabBar,
)

from app import i18n  # noqa: E402
from app.i18n import (  # noqa: E402
    current_language,
    format_decimal,
    language_override,
    set_language,
    system_language,
    tr,
)
from app.main_window import MainWindow  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.translations_de import GERMAN  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CATALOGUE = REPO / "app" / "translations_de.py"
# tr() and N_() mark translatable text; _echo() in the hardware setup passes its text to tr().
MARKERS = {"tr", "N_", "_echo"}
GERMAN_WORDS = re.compile(
    r"[äöüÄÖÜß]|\b(und|nicht|oder|wird|werden|konnte|kann|keine?|fehlgeschlagen|Fehler|"
    r"Warnung|Speicher|Benutzer|Prozess|Prozesse|Dienste?|Einstellungen|Leistung|"
    r"Aktualisieren|Aktiviert|Deaktiviert|Datenträger|Arbeitsspeicher)\b")
# What older servers send, and the name a language keeps in every other language.
ALLOWED_GERMAN = {
    "Deutsch", "Benutzer", "Kopieren", "Video-Encode", "Video-Decode", "Video-Verarbeitung",
    "Speicher-Controller",
}


def source_files():
    files = [REPO / "main.py"]
    for directory in ("app", "pages", "widgets"):
        files.extend(sorted((REPO / directory).glob("*.py")))
    return [path for path in files if path != CATALOGUE]


def marked_strings() -> dict:
    """{source string: "file:line"} for every literal handed to a marker."""
    found = {}
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in MARKERS and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                found[node.args[0].value] = f"{path.relative_to(REPO)}:{node.lineno}"
    return found


def placeholders(text: str) -> set:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


class TestCatalogue(unittest.TestCase):
    def test_every_marked_string_has_a_german_translation(self):
        missing = [f"{where}: {source!r}" for source, where in marked_strings().items()
                   if source not in GERMAN]
        self.assertEqual(missing, [], "missing in app/translations_de.py")

    def test_catalogue_has_no_stale_entries(self):
        marked = marked_strings()
        self.assertEqual([source for source in GERMAN if source not in marked], [])

    def test_no_translation_is_empty(self):
        self.assertEqual([source for source, text in GERMAN.items() if not text.strip()], [])

    def test_placeholders_match_between_languages(self):
        wrong = [source for source, text in GERMAN.items()
                 if placeholders(source) != placeholders(text)]
        self.assertEqual(wrong, [])

    def test_calls_fill_exactly_the_placeholders_of_their_text(self):
        wrong = []
        for path in source_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "tr" and node.args):
                    continue
                argument = node.args[0]
                where = f"{path.relative_to(REPO)}:{node.lineno}"
                if isinstance(argument, ast.JoinedStr):
                    wrong.append(f"{where}: f-string as source")
                elif isinstance(argument, ast.Constant) and node.keywords:
                    given = {keyword.arg for keyword in node.keywords}
                    if given != placeholders(argument.value):
                        wrong.append(f"{where}: {argument.value!r} gets {sorted(given)}")
        self.assertEqual(wrong, [])

    def test_python_source_has_no_german_left(self):
        leftovers = []
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(text)):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value not in ALLOWED_GERMAN and GERMAN_WORDS.search(node.value)):
                    leftovers.append(f"{path.relative_to(REPO)}:{node.lineno}: {node.value!r}")
            with path.open("rb") as stream:
                for token in tokenize.tokenize(stream.readline):
                    if token.type == tokenize.COMMENT and GERMAN_WORDS.search(token.string):
                        leftovers.append(
                            f"{path.relative_to(REPO)}:{token.start[0]}: {token.string}")
        self.assertEqual(leftovers, [])


class TestLanguageLayer(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_language, "system")

    def system_language_with(self, **variables):
        with mock.patch.dict(os.environ, variables, clear=True):
            return system_language()

    def test_system_language_follows_the_locale(self):
        for variables, expected in (
            ({"LANG": "de_DE.UTF-8"}, "de"),
            ({"LANG": "de_AT"}, "de"),
            ({"LANG": "en_GB.UTF-8"}, "en"),
            ({"LC_MESSAGES": "de_DE.UTF-8", "LANG": "en_US.UTF-8"}, "de"),
            ({"LC_ALL": "en_US.UTF-8", "LANG": "de_DE.UTF-8"}, "en"),
            ({"LANGUAGE": "de:en", "LANG": "en_US.UTF-8"}, "de"),
            ({"LANGUAGE": "fr:de", "LANG": "en_US.UTF-8"}, "de"),
        ):
            with self.subTest(variables=variables):
                self.assertEqual(self.system_language_with(**variables), expected)

    def test_unsupported_or_missing_locale_falls_back_to_english(self):
        for variables in ({}, {"LANG": "fr_FR.UTF-8"}, {"LANG": "C"}, {"LANG": "POSIX"},
                          {"LANG": "C.UTF-8"}, {"LC_ALL": "C", "LANG": "de_DE.UTF-8"},
                          {"LANGUAGE": "", "LANG": "ja_JP"}):
            with self.subTest(variables=variables):
                self.assertEqual(self.system_language_with(**variables), "en")

    def test_set_language_system_means_the_locale(self):
        with mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8"}, clear=True):
            set_language("system")
            self.assertEqual(current_language(), "de")
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            set_language("no-such-language")
            self.assertEqual(current_language(), "en")

    def test_tr_translates_and_fills_placeholders(self):
        source = "Do you really want to end the process with PID {pid}?"
        set_language("en")
        self.assertEqual(tr(source, pid=7), source.format(pid=7))
        set_language("de")
        self.assertEqual(tr(source, pid=7), GERMAN[source].format(pid=7))

    def test_unknown_text_and_braces_pass_through(self):
        set_language("de")
        self.assertEqual(tr("not in the catalogue"), "not in the catalogue")
        self.assertEqual(tr("{literal}"), "{literal}")

    def test_override_applies_to_this_thread_only_for_its_duration(self):
        set_language("en")
        seen = []
        with language_override("de"):
            seen.append(current_language())
            thread =threading.Thread(target=lambda: seen.append(current_language()))
            thread.start()
            thread.join()
        seen.append(current_language())
        self.assertEqual(seen, ["de", "en", "en"])

    def test_override_with_unsupported_code_keeps_the_language(self):
        set_language("de")
        with language_override("fr"):
            self.assertEqual(current_language(), "de")

    def test_decimal_separator_follows_the_language(self):
        set_language("de")
        self.assertEqual(format_decimal(1.25, 2), "1,25")
        set_language("en")
        self.assertEqual(format_decimal(1.25, 2), "1.25")

    def test_layer_stays_free_of_qt_for_the_headless_server(self):
        for path in (i18n.__file__, CATALOGUE):
            self.assertNotIn("PySide6", Path(path).read_text(encoding="utf-8"))


def visible_texts(window) -> list:
    """Everything the window shows that isn't live data from the system."""
    texts = [window.windowTitle()]
    for action in window.menuBar().actions():
        texts.append(action.text())
        if action.menu():
            texts.extend(item.text() for item in action.menu().actions())
    for widget in window.findChildren(object):
        if isinstance(widget, (QLabel, QAbstractButton)):
            texts.append(widget.text())
        elif isinstance(widget, QGroupBox):
            texts.append(widget.title())
        elif isinstance(widget, (QLineEdit, QPlainTextEdit)):
            texts.append(widget.placeholderText())
        elif isinstance(widget, QComboBox):
            texts.extend(widget.itemText(index) for index in range(widget.count()))
        elif isinstance(widget, QTabBar):
            texts.extend(widget.tabText(index) for index in range(widget.count()))
        elif isinstance(widget, QHeaderView) and widget.model() is not None:
            texts.extend(str(widget.model().headerData(section, widget.orientation(),
                                                       Qt.DisplayRole) or "")
                         for section in range(widget.count()))
        if hasattr(widget, "toolTip"):
            texts.append(widget.toolTip())
    return [text for text in texts if text]


class TestMainWindowLanguage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.directory = tempfile.TemporaryDirectory()
        for settings_format in (QSettings.IniFormat, QSettings.NativeFormat):
            QSettings.setPath(settings_format, QSettings.UserScope, cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        config = QStandardPaths.writableLocation(QStandardPaths.GenericConfigLocation)
        for settings_format in (QSettings.IniFormat, QSettings.NativeFormat):
            QSettings.setPath(settings_format, QSettings.UserScope, config)
        cls.directory.cleanup()
        set_language("system")

    def open_window(self, language: str) -> MainWindow:
        Settings().language = language
        window = MainWindow()
        window.show()
        self.addCleanup(window.close)
        self.pump()
        return window

    def pump(self) -> None:
        for _ in range(5):
            QApplication.processEvents()

    def choose(self, window: MainWindow, language: str) -> None:
        combo = window.settings_page.language_combo
        combo.setCurrentIndex(combo.findData(language))
        self.pump()

    def tab_texts(self, window: MainWindow) -> list:
        return [window.tabs.tabText(index) for index in range(window.tabs.count())]

    def test_english_window_shows_no_german(self):
        window = self.open_window("en")
        german = {text for source, text in GERMAN.items() if text != source}
        texts = visible_texts(window)
        self.assertGreater(len(texts), 50)
        self.assertEqual([text for text in texts if text != "Deutsch"
                          and (text in german or re.search("[äöüÄÖÜß]", text))], [])

    def test_german_window_shows_no_english_source_text(self):
        window = self.open_window("de")
        untranslated = {source for source, text in GERMAN.items() if text != source}
        self.assertEqual([text for text in visible_texts(window)
                          if text in untranslated and text != "English"], [])

    def test_language_switches_live_and_is_remembered(self):
        window = self.open_window("en")
        english_tabs = self.tab_texts(window)
        old_page = window.settings_page

        self.choose(window, "de")
        self.assertEqual(current_language(), "de")
        self.assertIsNot(window.settings_page, old_page)
        german_tabs = self.tab_texts(window)
        self.assertEqual(len(german_tabs), 8)
        self.assertNotEqual(german_tabs, english_tabs)
        self.assertEqual(window.tabs.currentWidget(), window.page_frames[-1])
        self.assertEqual(window.settings_page.language_combo.currentData(), "de")
        self.assertEqual(Settings().language, "de")

        self.choose(window, "en")
        self.assertEqual(self.tab_texts(window), english_tabs)
        self.assertEqual(Settings().language, "en")

    def test_system_default_follows_the_locale(self):
        window = self.open_window("en")
        with mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8"}, clear=True):
            self.choose(window, "system")
        self.assertEqual(current_language(), "de")
        self.assertEqual(window.settings_page.language_combo.currentData(), "system")

    def test_new_settings_default_to_the_system_language(self):
        settings = Settings()
        settings._store.remove("language")
        self.assertEqual(settings.language, "system")
