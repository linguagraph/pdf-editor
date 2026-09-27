"""Dialogs for redaction and sanitizing."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.redaction import (
    GraphicsRedaction,
    ImageRedaction,
    RedactOptions,
    SanitizeOptions,
)
from pdfeditor.model.text import SearchHit
from pdfeditor.services.redaction import PRESETS, MarkStyle
from pdfeditor.ui.dialogs.pages import ColorButton


def _buttons(dialog: QDialog, ok_text: str) -> QDialogButtonBox:
    box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog
    )
    box.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


class RedactionPropertiesDialog(QDialog):
    def __init__(self, style: MarkStyle, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Redaction Properties")
        self.fill = ColorButton(style.fill)
        self.overlay = QLineEdit(style.overlay_text)
        self.overlay.setPlaceholderText("e.g. REDACTED (optional)")
        self.text_color = ColorButton(style.text_color)
        form = QFormLayout()
        form.addRow("Box color:", self.fill)
        form.addRow("Overlay text:", self.overlay)
        form.addRow("Overlay text color:", self.text_color)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(_buttons(self, "OK"))

    def mark_style(self) -> MarkStyle:
        return MarkStyle(self.fill.color, self.overlay.text(), self.text_color.color)


class MarkTextDialog(QDialog):
    """Find sensitive text (presets or a custom pattern), review matches, mark the chosen ones."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find Text to Redact")
        self.resize(560, 520)
        self.preset_boxes: dict[str, QCheckBox] = {}
        presets = QGroupBox("Patterns")
        presets_layout = QVBoxLayout(presets)
        for name in PRESETS:
            box = QCheckBox(name)
            self.preset_boxes[name] = box
            presets_layout.addWidget(box)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("Custom regular expression or plain words")
        self.custom_is_regex = QCheckBox("Custom text is a regular expression")
        presets_layout.addWidget(self.custom)
        presets_layout.addWidget(self.custom_is_regex)
        self.search_button = QPushButton("Find")
        self.results = QListWidget()
        self.summary = QLabel()
        select_all = QPushButton("Select All")
        select_all.clicked.connect(lambda: self._check_all(True))
        select_none = QPushButton("Select None")
        select_none.clicked.connect(lambda: self._check_all(False))
        layout = QVBoxLayout(self)
        layout.addWidget(presets)
        layout.addWidget(self.search_button)
        layout.addWidget(self.summary)
        layout.addWidget(self.results, 1)
        row = QVBoxLayout()
        row.addWidget(select_all)
        row.addWidget(select_none)
        layout.addLayout(row)
        layout.addWidget(_buttons(self, "Mark Selected"))
        self.hits: list[SearchHit] = []

    def patterns(self) -> list[str]:
        import re

        chosen = [name for name, box in self.preset_boxes.items() if box.isChecked()]
        custom = self.custom.text().strip()
        if custom:
            chosen.append(custom if self.custom_is_regex.isChecked() else re.escape(custom))
        return chosen

    def show_hits(
        self, hits: list[SearchHit], page_label: Callable[[int], str] = lambda i: str(i + 1)
    ) -> None:
        self.hits = hits
        self.results.clear()
        for hit in hits:
            item = QListWidgetItem(f"Page {page_label(hit.page_index)}: {hit.text}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.results.addItem(item)
        self.summary.setText(f"{len(hits)} match(es). Uncheck any you want to keep.")

    def _check_all(self, on: bool) -> None:
        for i in range(self.results.count()):
            self.results.item(i).setCheckState(
                Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
            )

    def selected_hits(self) -> list[SearchHit]:
        return [
            hit
            for i, hit in enumerate(self.hits)
            if self.results.item(i).checkState() == Qt.CheckState.Checked
        ]


class ApplyRedactionsDialog(QDialog):
    def __init__(self, total: int, selected: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply Redactions")
        intro = QLabel(
            "Applying removes the content under the marks permanently. It can be undone until you "
            "close the document, but once saved the removed content is gone from the file."
        )
        intro.setWordWrap(True)
        self.all = QRadioButton(f"All marks in the document ({total})")
        self.selected = QRadioButton(f"Selected marks only ({selected})")
        self.selected.setEnabled(selected > 0)
        (self.selected if selected else self.all).setChecked(True)
        self.images = QComboBox()
        for value, text in (
            (ImageRedaction.PIXELS, "Blank the covered image pixels"),
            (ImageRedaction.REMOVE, "Remove images that touch a mark"),
            (ImageRedaction.NONE, "Leave images unchanged"),
        ):
            self.images.addItem(text, value)
        self.graphics = QComboBox()
        for gvalue, text in (
            (GraphicsRedaction.COVERED, "Remove vector graphics fully covered"),
            (GraphicsRedaction.TOUCHED, "Remove vector graphics that touch a mark"),
            (GraphicsRedaction.NONE, "Leave vector graphics unchanged"),
        ):
            self.graphics.addItem(text, gvalue)
        form = QFormLayout()
        form.addRow("Images:", self.images)
        form.addRow("Vector graphics:", self.graphics)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.all)
        layout.addWidget(self.selected)
        layout.addLayout(form)
        layout.addWidget(_buttons(self, "Apply"))

    def options(self) -> RedactOptions:
        return RedactOptions(images=self.images.currentData(), graphics=self.graphics.currentData())


class SanitizeDialog(QDialog):
    ITEMS = (
        ("metadata", "Document properties (title, author, …)"),
        ("xmp", "XMP metadata"),
        ("javascript", "JavaScript and automatic actions"),
        ("attachments", "Attached files"),
        ("links", "Links"),
        ("hidden_text", "Hidden (invisible) text"),
        ("comments", "Comments and markups"),
        ("form_data", "Form field values"),
        ("thumbnails", "Embedded page thumbnails"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sanitize Document")
        intro = QLabel("Remove hidden information that isn't visible on the pages:")
        intro.setWordWrap(True)
        self.boxes: dict[str, QCheckBox] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        for key, text in self.ITEMS:
            box = QCheckBox(text)
            box.setChecked(True)
            self.boxes[key] = box
            layout.addWidget(box)
        layout.addWidget(_buttons(self, "Sanitize"))

    def options(self) -> SanitizeOptions:
        return SanitizeOptions(**{key: box.isChecked() for key, box in self.boxes.items()})
