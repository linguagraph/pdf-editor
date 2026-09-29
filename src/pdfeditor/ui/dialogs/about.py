"""Help ▸ About: version, AGPL notice with the source offer, third-party licenses."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pdfeditor import SOURCE_URL, __version__
from pdfeditor.licenses import about_text, license_text, notices


def _text(content: str, name: str) -> QPlainTextEdit:
    box = QPlainTextEdit(content)
    box.setReadOnly(True)
    box.setAccessibleName(name)
    return box


class AboutDialog(QDialog):
    def __init__(self, engine_version: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About pdfeditor")
        self.resize(640, 520)
        head = QLabel(
            f"<h2>pdfeditor {__version__}</h2>"
            "<p>An open-source PDF editor.</p>"
            + (f"<p>{engine_version}</p>" if engine_version else "")
            + f'<p>Source code: <a href="{SOURCE_URL}">{SOURCE_URL}</a></p>',
            self,
        )
        head.setOpenExternalLinks(True)
        head.setWordWrap(True)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(_text(about_text(), "About"), "About")
        self.tabs.addTab(_text(notices(), "Third-party licenses"), "Third-Party Licenses")
        self.tabs.addTab(_text(license_text(), "License"), "License (AGPL-3.0)")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(head)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)
