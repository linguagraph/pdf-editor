"""Help ▸ About: version, AGPL notice with the source offer, third-party licenses."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QTabWidget,
    QWidget,
)

from pdfeditor import SOURCE_URL, __version__
from pdfeditor.licenses import about_text, license_text, notices
from pdfeditor.ui.dialogs.base import FormDialog


def _text(content: str, name: str) -> QPlainTextEdit:
    box = QPlainTextEdit(content)
    box.setReadOnly(True)
    box.setAccessibleName(name)
    return box


class AboutDialog(FormDialog):
    def __init__(self, engine_version: str = "", parent: QWidget | None = None) -> None:
        super().__init__(
            f"pdfeditor {__version__}",
            "An open-source PDF editor." + (f" {engine_version}" if engine_version else ""),
            parent,
            window_title="About pdfeditor",
            primary=None,
            cancel="Close",
        )
        self.resize(640, 520)
        head = QLabel(f'Source code: <a href="{SOURCE_URL}">{SOURCE_URL}</a>', self)
        head.setOpenExternalLinks(True)
        head.setWordWrap(True)
        self.tabs = QTabWidget(self)
        self.tabs.setAccessibleName("About pages")
        self.tabs.tabBar().setAccessibleName("About pages")
        self.tabs.addTab(_text(about_text(), "About"), "About")
        self.tabs.addTab(_text(notices(), "Third-party licenses"), "Third-Party Licenses")
        self.tabs.addTab(_text(license_text(), "License"), "License (AGPL-3.0)")
        self.add_widget(head)
        self.add_widget(self.tabs, 1)
