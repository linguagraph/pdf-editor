"""Document properties: description, advanced info and fonts."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import SetMetadataCommand
from pdfeditor.core.session import DocumentSession
from pdfeditor.model.metadata import EncryptionMethod, FontInfo
from pdfeditor.services.fonts import cached_catalog, installed_ref_for_font_name
from pdfeditor.ui.dialogs.base import FormDialog, add_row, form_layout
from pdfeditor.ui.panels.attachments import human_size
from pdfeditor.ui.style.tokens import METRICS


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _selectable(text: str) -> QLabel:
    label = QLabel(text or "—")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)
    return label


def page_ranges(pages: tuple[int, ...]) -> str:
    """1-based, compact: ``1-3, 7``."""
    out: list[str] = []
    start = prev = -2
    for p in sorted(pages):
        if p == prev + 1:
            prev = p
            continue
        if start >= 0:
            out.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
        start = prev = p
    if start >= 0:
        out.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
    return ", ".join(out)


def editing_use(font: FontInfo) -> tuple[str, str]:
    """How text in ``font`` is set when edited, and why (for the Fonts tab)."""
    if font.type == "Type3":
        return "Can't edit", "Text in Type3 fonts can't be edited."
    installed = installed_ref_for_font_name(cached_catalog(), font.name)
    if font.embedded and not font.subset:
        return "Reused", "The complete font is embedded, so edits use it."
    if installed is not None:
        return (
            "Installed copy",
            "Edits use the characters embedded in the PDF and take any others from the copy "
            "of this font installed on this computer.",
        )
    if font.subset:
        return (
            "Partly reused",
            "Only the characters already used are embedded; others are set in a standard font.",
        )
    return "Standard substitute", "The font isn't embedded or installed; edits use a standard font."


class PropertiesDialog(FormDialog):
    def __init__(self, session: DocumentSession, parent: QWidget | None = None) -> None:
        super().__init__(
            "Document Properties",
            session.display_name,
            parent,
            window_title=f"Document Properties — {session.display_name}",
        )
        self.session = session
        self.resize(600, 560)
        with session.lock:
            doc = session.document
            meta = doc.metadata()
            info = doc.info()
            fonts = doc.fonts()
            pending = doc.pending_security()
            path = doc.path

        tabs = QTabWidget(self)
        tabs.setAccessibleName("Property pages")
        tabs.tabBar().setAccessibleName("Property pages")

        description = QWidget()
        form = form_layout()
        form.setContentsMargins(*(METRICS.space(3),) * 4)
        description.setLayout(form)
        form.addRow("File:", _selectable(str(path) if path else "(not saved)"))
        self.original = meta
        self.fields: dict[str, QLineEdit] = {}
        editable = session.engine.capabilities.metadata_write
        for key, label in (
            ("title", "Title:"),
            ("author", "Author:"),
            ("subject", "Subject:"),
            ("keywords", "Keywords:"),
        ):
            edit = QLineEdit(getattr(meta, key))
            edit.setReadOnly(not editable)
            self.fields[key] = edit
            add_row(form, label, edit)
        form.addRow("Created:", _selectable(meta.creation_date))
        form.addRow("Modified:", _selectable(meta.mod_date))
        form.addRow("Application:", _selectable(meta.creator))
        form.addRow("PDF producer:", _selectable(meta.producer))
        tabs.addTab(description, "Description")

        advanced = QWidget()
        form = form_layout()
        form.setContentsMargins(*(METRICS.space(3),) * 4)
        advanced.setLayout(form)
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
        if pending is not None:
            change = (
                "removed"
                if pending.method is EncryptionMethod.NONE
                else f"set to {pending.method.value}"
                + (" with an open password" if pending.user_password else "")
            )
            form.addRow("On next save:", _selectable(f"Security will be {change}"))
        form.addRow("Tagged PDF:", _selectable(_yes_no(info.is_tagged)))
        form.addRow("Has forms:", _selectable(_yes_no(info.has_forms)))
        form.addRow("Digitally signed:", _selectable(_yes_no(info.has_signatures)))
        form.addRow("Contains JavaScript:", _selectable(_yes_no(info.has_javascript)))
        form.addRow("Repaired on open:", _selectable(_yes_no(info.is_repaired)))
        tabs.addTab(advanced, "Advanced")

        self.fonts_table = QTableWidget(len(fonts), 6)
        self.fonts_table.setAccessibleName("Fonts")
        self.fonts_table.setHorizontalHeaderLabels(
            ["Font", "Type", "Encoding", "Embedding", "Pages", "For editing"]
        )
        self.fonts_table.verticalHeader().setVisible(False)
        self.fonts_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.fonts_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.fonts_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.font_rows = sorted(fonts, key=lambda f: f.name.lower())
        self.show_font: FontInfo | None = None  # set by "Show Text in This Font"
        for row, f in enumerate(self.font_rows):
            embedding = (
                "Embedded subset" if f.subset else "Embedded" if f.embedded else "Not embedded"
            )
            editing, why = editing_use(f)
            cells = (f.name, f.type, f.encoding, embedding, page_ranges(f.pages), editing)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 5:
                    item.setToolTip(why)
                self.fonts_table.setItem(row, col, item)
        self.fonts_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.show_font_button = QPushButton("Show Text in This Font")
        self.show_font_button.setToolTip("Select the text set in this font, ready to edit")
        self.show_font_button.setEnabled(False)
        self.show_font_button.clicked.connect(self._show_selected_font)
        self.fonts_table.itemSelectionChanged.connect(
            lambda: self.show_font_button.setEnabled(self._selected_font() is not None)
        )
        fonts_page = QWidget()
        fonts_layout = QVBoxLayout(fonts_page)
        fonts_layout.addWidget(self.fonts_table, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.show_font_button)
        fonts_layout.addLayout(buttons)
        tabs.addTab(fonts_page, f"Fonts ({len(fonts)})")

        self.add_widget(tabs, 1)
        self.tabs = tabs

    def accept(self) -> None:
        """Apply description edits as one undoable command (nothing if unchanged)."""
        meta = copy.copy(self.original)
        for key, edit in self.fields.items():
            setattr(meta, key, edit.text())
        if meta != self.original:
            self.session.execute(SetMetadataCommand(meta))
        super().accept()

    def _selected_font(self) -> FontInfo | None:
        rows = {i.row() for i in self.fonts_table.selectedIndexes()}
        if len(rows) != 1:
            return None
        font = self.font_rows[rows.pop()]
        return font if font.pages and font.type != "Type3" else None

    def _show_selected_font(self) -> None:
        self.show_font = self._selected_font()
        if self.show_font is not None:
            self.accept()
