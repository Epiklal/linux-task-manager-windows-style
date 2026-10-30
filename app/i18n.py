"""Small translation layer: English source strings, one catalogue per other language.

Free of Qt on purpose, because the headless server imports it. Text that crosses the
network stays in English (marked with N_) and is translated by whoever displays it.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

from app.translations_de import GERMAN

LANGUAGES = ("de", "en")
FALLBACK_LANGUAGE = "en"
_CATALOGUES = {"de": GERMAN}

_language: str | None = None
_override = threading.local()


def N_(source: str) -> str:
    """Marks a string for the catalogue check; translate it later with tr(variable)."""
    return source


def system_language() -> str:
    """The language of the user's locale, English when it isn't a supported one."""
    # Same as gettext: the first variable that is set decides, and LANGUAGE is a
    # colon-separated list of preferences.
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable)
        if value:
            for preference in value.split(":"):
                if preference[:2].lower() in LANGUAGES:
                    return preference[:2].lower()
            break
    return FALLBACK_LANGUAGE


def set_language(code: str) -> None:
    """Sets the process language; anything but a supported code means the system's."""
    global _language
    _language = code if code in LANGUAGES else system_language()


def current_language() -> str:
    override = getattr(_override, "language", None)
    if override:
        return override
    global _language
    if _language is None:
        _language = system_language()
    return _language


@contextmanager
def language_override(code: str):
    """Translates into `code` on this thread only, e.g. for one server request."""
    previous = getattr(_override, "language", None)
    _override.language = code if code in LANGUAGES else previous
    try:
        yield
    finally:
        _override.language = previous


def tr(source: str, **values) -> str:
    """The translation of `source` (unknown text passes through), with {placeholders}
    filled in. Without values no formatting happens, so braces in `source` are safe."""
    catalogue = _CATALOGUES.get(current_language())
    text = catalogue.get(source, source) if catalogue else source
    return text.format(**values) if values else text


def format_decimal(value: float, decimals: int = 1) -> str:
    """Number with the decimal separator of the current language."""
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",") if current_language() == "de" else text
