"""Dialogs for redaction and sanitizing."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
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
from pdfeditor.ui.dialogs.base import FormDialog, add_row
from pdfeditor.ui.dialogs.pages import ColorButton
from pdfeditor.ui.style.tokens import METRICS


class RedactionPropertiesDialog(FormDialog):
    def __init__(self, style: MarkStyle, parent: QWidget | None = None) -> None:
        super().__init__("Redaction Properties", "How applied redactions look on the page.", parent)
        self.fill = ColorButton(style.fill)
        self.overlay = QLineEdit(style.overlay_text)
        self.overlay.setPlaceholderText("e.g. REDACTED (optional)")
        self.text_color = ColorButton(style.text_color)
        form = self.add_form()
        add_row(form, "Box color:", self.fill)
        add_row(form, "Overlay text:", self.overlay, "Printed on each box, e.g. an exemption code.")
        add_row(form, "Overlay text color:", self.text_color)

    def mark_style(self) -> MarkStyle:
        return MarkStyle(self.fill.color, self.overlay.text(), self.text_color.color)


class MarkTextDialog(FormDialog):
    """Find sensitive text (presets or a custom pattern), review matches, mark the chosen ones."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Find Text to Redact",
            "Find, review, then mark matches for redaction. Nothing is removed until you "
            "apply the marks.",
            parent,
            primary="Mark Selected",
        )
        self.resize(580, 600)
        self.preset_boxes: dict[str, QCheckBox] = {}
        self.patterns_section = self.add_section("Patterns")
        presets_layout = QVBoxLayout()
        presets_layout.setSpacing(METRICS.space(1))
        for name in PRESETS:
            box = QCheckBox(name)
            self.preset_boxes[name] = box
            presets_layout.addWidget(box)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("Custom regular expression or plain words")
        self.custom.setAccessibleName("Custom search text")
        self.custom_is_regex = QCheckBox("Custom text is a regular expression")
        presets_layout.addWidget(self.custom)
        presets_layout.addWidget(self.custom_is_regex)
        self.patterns_section.set_layout(presets_layout)
        self.search_button = QPushButton("Find")
        self.results = QListWidget()
        self.results.setAccessibleName("Matches")
        self.summary = QLabel()
        self.summary.setProperty("role", "muted")
        select_all = QPushButton("Select All")
        select_all.clicked.connect(lambda: self._check_all(True))
        select_none = QPushButton("Select None")
        select_none.clicked.connect(lambda: self._check_all(False))
        find_row = QHBoxLayout()
        find_row.addWidget(self.search_button)
        find_row.addWidget(self.summary, 1)
        self.content.addLayout(find_row)
        self.add_widget(self.results, 1)
        row = QHBoxLayout()
        row.addWidget(select_all)
        row.addWidget(select_none)
        row.addStretch()
        self.content.addLayout(row)
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


class ApplyRedactionsDialog(FormDialog):
    def __init__(self, total: int, selected: int, parent: QWidget | None = None) -> None:
        super().__init__(
            "Apply Redactions",
            "Applying removes the content under the marks permanently. It can be undone until "
            "you close the document, but once saved the removed content is gone from the file.",
            parent,
            primary="Apply",
            danger=True,
        )
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
        scope = self.add_form()
        marks = QVBoxLayout()
        marks.setSpacing(METRICS.space(1))
        marks.addWidget(self.all)
        marks.addWidget(self.selected)
        scope.addRow("Apply:", marks)
        self.content_section = self.add_section("Images and graphics under the marks")
        form = self.content_section.form()
        add_row(form, "Images:", self.images)
        add_row(form, "Vector graphics:", self.graphics)

    def options(self) -> RedactOptions:
        return RedactOptions(images=self.images.currentData(), graphics=self.graphics.currentData())


class SanitizeDialog(FormDialog):
    ITEMS = (
        ("metadata", "Document properties (title, author, …)"),
        ("xmp", "XMP metadata"),
        ("javascript", "JavaScript and automatic actions"),
        ("attachments", "Attached files"),
        ("links", "Links"),
        ("hidden_text", "Hidden (invisible) text"),
        ("hidden_layers", "Content on hidden layers"),
        ("off_page_text", "Text outside the visible page area"),
        ("comments", "Comments and markups"),
        ("form_data", "Form field values"),
        ("thumbnails", "Embedded page thumbnails"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Sanitize Document",
            "Remove hidden information that isn't visible on the pages. Everything checked "
            "is deleted from the document.",
            parent,
            primary="Sanitize",
            danger=True,
        )
        self.boxes: dict[str, QCheckBox] = {}
        items = QVBoxLayout()
        items.setSpacing(METRICS.space(1))
        for key, text in self.ITEMS:
            box = QCheckBox(text)
            box.setChecked(True)
            self.boxes[key] = box
            items.addWidget(box)
        self.content.addLayout(items)

    def options(self) -> SanitizeOptions:
        return SanitizeOptions(**{key: box.isChecked() for key, box in self.boxes.items()})
