"""Document properties: description, advanced info and fonts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.session import DocumentSession
from pdfeditor.model.metadata import EncryptionMethod
from pdfeditor.ui.panels.attachments import human_size


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _selectable(text: str) -> QLabel:
    label = QLabel(text or "—")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)
    return label


class PropertiesDialog(QDialog):
    def __init__(self, session: DocumentSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Document Properties — {session.display_name}")
        self.resize(560, 480)
        with session.lock:
            doc = session.document
            meta = doc.metadata()
            info = doc.info()
            fonts = doc.fonts()
            path = doc.path

        tabs = QTabWidget(self)

        description = QWidget()
        form = QFormLayout(description)
        form.addRow("File:", _selectable(str(path) if path else "(not saved)"))
        form.addRow("Title:", _selectable(meta.title))
        form.addRow("Author:", _selectable(meta.author))
        form.addRow("Subject:", _selectable(meta.subject))
        form.addRow("Keywords:", _selectable(meta.keywords))
        form.addRow("Created:", _selectable(meta.creation_date))
        form.addRow("Modified:", _selectable(meta.mod_date))
        form.addRow("Application:", _selectable(meta.creator))
        form.addRow("PDF producer:", _selectable(meta.producer))
        tabs.addTab(description, "Description")

        advanced = QWidget()
        form = QFormLayout(advanced)
        form.addRow("PDF version:", _selectable(info.pdf_version))
        form.addRow("Pages:", _selectable(str(info.page_count)))
        form.addRow("File size:", _selectable(human_size(info.file_size) if info.file_size else ""))
        encrypted = info.encryption is not EncryptionMethod.NONE
        form.addRow("Security:", _selectable(info.encryption.value if encrypted else "No security"))
        if encrypted:
            p = info.permissions
            allowed = [
                name
                for name, ok in (
                    ("printing", p.print),
                    ("changing", p.modify),
                    ("copying", p.copy),
                    ("commenting", p.annotate),
                    ("form filling", p.fill_forms),
                    ("page assembly", p.assemble),
                )
                if ok
            ]
            form.addRow("Allowed:", _selectable(", ".join(allowed) or "nothing"))
        form.addRow("Tagged PDF:", _selectable(_yes_no(info.is_tagged)))
        form.addRow("Has forms:", _selectable(_yes_no(info.has_forms)))
        form.addRow("Digitally signed:", _selectable(_yes_no(info.has_signatures)))
        form.addRow("Contains JavaScript:", _selectable(_yes_no(info.has_javascript)))
        form.addRow("Repaired on open:", _selectable(_yes_no(info.is_repaired)))
        tabs.addTab(advanced, "Advanced")

        self.fonts_table = QTableWidget(len(fonts), 4)
        self.fonts_table.setHorizontalHeaderLabels(["Font", "Type", "Encoding", "Embedding"])
        self.fonts_table.verticalHeader().setVisible(False)
        self.fonts_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, f in enumerate(sorted(fonts, key=lambda f: f.name.lower())):
            embedding = (
                "Embedded subset" if f.subset else "Embedded" if f.embedded else "Not embedded"
            )
            for col, text in enumerate((f.name, f.type, f.encoding, embedding)):
                self.fonts_table.setItem(row, col, QTableWidgetItem(text))
        self.fonts_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tabs.addTab(self.fonts_table, f"Fonts ({len(fonts)})")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self.tabs = tabs
