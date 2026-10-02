"""Translations: Qt's own strings (dialog buttons, file dialogs, ...) plus the app's catalogues.

App catalogues are ``data/i18n/pdfeditor_<lang>.qm`` built from ``self.tr``/``QCoreApplication
.translate`` strings with pyside6-lupdate/lrelease. This is the scaffolding: the language
setting, loading and fallbacks; the UI strings are being moved to ``tr`` gradually.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator

from pdfeditor.bundle import data_path

log = logging.getLogger(__name__)

LANGUAGE_NAMES = {"en": "English", "bg": "Български", "de": "Deutsch", "fr": "Français"}


def _app_catalogue_dir() -> Path | None:
    try:
        return data_path("i18n")
    except FileNotFoundError:
        return None


def available_languages() -> dict[str, str]:
    """Code -> display name: English plus every language with an app catalogue."""
    out = {"en": LANGUAGE_NAMES["en"]}
    folder = _app_catalogue_dir()
    if folder is not None:
        for qm in sorted(folder.glob("pdfeditor_*.qm")):
            code = qm.stem.split("_", 1)[1]
            out[code] = LANGUAGE_NAMES.get(code, QLocale(code).nativeLanguageName() or code)
    return out


def install_translators(app: QCoreApplication, language: str = "") -> list[QTranslator]:
    """Load translations for ``language`` ("" = the system's). Returns the installed
    translators (they must stay alive as long as the application)."""
    locale = QLocale(language) if language else QLocale.system()
    installed: list[QTranslator] = []
    qt_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    candidates = [("qtbase", qt_dir)]
    folder = _app_catalogue_dir()
    if folder is not None:
        candidates.append(("pdfeditor", str(folder)))
    for name, directory in candidates:
        translator = QTranslator(app)
        if translator.load(locale, name, "_", directory):
            app.installTranslator(translator)
            installed.append(translator)
            log.info("loaded %s translation for %s", name, locale.name())
    return installed


# Markers for strings kept in tables and translated where they're shown. They have Qt's names
# so pyside6-lupdate extracts them, and are typed (PySide6's own return ``object``).
def QT_TRANSLATE_NOOP(context: str, text: str) -> str:
    return text


def QT_TR_NOOP(text: str) -> str:
    return text
