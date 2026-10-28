"""Help ▸ About: the Elanero artwork, version, AGPL notice with the source offer, licenses."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTabWidget,
    QWidget,
)

from pdfeditor import APP_DISPLAY_NAME, PRIVACY_URL, SOURCE_URL, __version__
from pdfeditor.bundle import channel
from pdfeditor.licenses import about_text, license_text, notices
from pdfeditor.ui.branding import app_icon, wordmark
from pdfeditor.ui.dialogs.base import FormDialog
from pdfeditor.ui.theme import theme_manager

WORDMARK_HEIGHT = 72
ICON_SIZE = 64


def _text(content: str, name: str) -> QPlainTextEdit:
    box = QPlainTextEdit(content)
    box.setReadOnly(True)
    box.setAccessibleName(name)
    return box


class AboutDialog(FormDialog):
    def __init__(self, engine_version: str = "", parent: QWidget | None = None) -> None:
        super().__init__(
            APP_DISPLAY_NAME,
            f"Version {__version__} ({channel()}). An open-source PDF editor."
            + (f" {engine_version}" if engine_version else ""),
            parent,
            window_title="About Elanero",
            primary=None,
            cancel="Close",
        )
        self.resize(640, 560)
        # The wordmark's lettering is deep teal for light backgrounds and mustn't be recolored,
        # so dark and high-contrast themes show the icon alone (the title names the app).
        self.brand = QLabel(self)
        self.brand.setAccessibleName("Elanero logo")
        manager = theme_manager()
        ratio = self.devicePixelRatioF()
        if manager.scheme == "light" and not manager.high_contrast_applied:
            self.brand.setPixmap(wordmark(WORDMARK_HEIGHT, ratio))
        else:
            self.brand.setPixmap(app_icon().pixmap(ICON_SIZE, ICON_SIZE))
        self.brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        outer = self.layout()
        assert isinstance(outer, QBoxLayout)
        outer.insertWidget(0, self.brand)
        head = QLabel(
            f'Source code: <a href="{SOURCE_URL}">{SOURCE_URL}</a><br>'
            f'<a href="{PRIVACY_URL}">Privacy policy</a>: no data collected; works offline.',
            self,
        )
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
