"""Properties inspector for the selected comment."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import ChangeKind, UpdateAnnotationCommand
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.services.comments import type_label
from pdfeditor.ui.color_picker import pick_color, swatch_icon
from pdfeditor.ui.panels.base import EmptyState, ViewPanel
from pdfeditor.ui.view.document_view import DocumentView

FILL_TYPES = {
    AnnotationType.SQUARE,
    AnnotationType.CIRCLE,
    AnnotationType.POLYGON,
    AnnotationType.FREE_TEXT,
}
NO_BORDER = {AnnotationType.TEXT, AnnotationType.FILE_ATTACHMENT, AnnotationType.STAMP}


def restyled(before: AnnotationModel, field: str, value: object) -> AnnotationModel | None:
    """``before`` with ``field`` set to ``value``, or None when that changes nothing or the
    comment is locked (locked comments only allow unlocking).

    A text box's "color" is its text color: its border keeps its own."""
    if field != "locked" and before.locked:
        return None
    current = before.text_color if _is_text_color(before, field) else getattr(before, field)
    if current == value:
        return None
    after = copy.deepcopy(before)
    if _is_text_color(before, field):
        after.text_color = value  # type: ignore[assignment]
    else:
        setattr(after, field, value)
    return after


def _is_text_color(model: AnnotationModel, field: str) -> bool:
    return field == "color" and model.type is AnnotationType.FREE_TEXT


def shown_color(model: AnnotationModel) -> Color | None:
    """The color a comment's "Color" control shows (a text box: its text)."""
    return model.color or (model.text_color if model.type is AnnotationType.FREE_TEXT else None)


class ColorButton(QPushButton):
    def __init__(self, allow_none: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.allow_none = allow_none
        self.color: Color | None = None
        self.setFixedWidth(80)

    def set_color(self, color: Color | None) -> None:
        self.color = color
        self.setText("None" if color is None else "")
        self.setIcon(swatch_icon(color))
        self.setToolTip("" if color is None else color.to_hex())


class InspectorPanel(ViewPanel):
    title = "Properties"
    rebuild_on = frozenset({ChangeKind.STRUCTURE, ChangeKind.ANNOTATIONS})

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.heading = QLabel("No comment selected.", self)
        self.heading.setWordWrap(True)
        self.color = ColorButton(False, self)
        self.color.clicked.connect(lambda: self._pick("color"))
        self.fill = ColorButton(True, self)
        self.fill.clicked.connect(lambda: self._pick("fill"))
        self.no_fill = QCheckBox("No fill", self)
        self.no_fill.toggled.connect(self._on_no_fill)
        self.opacity = QSlider(Qt.Orientation.Horizontal, self)
        self.opacity.setRange(5, 100)
        self.opacity.valueChanged.connect(
            lambda v: self._apply("opacity", v / 100, "Change Opacity")
        )
        self.line_width = QDoubleSpinBox(self)
        self.line_width.setRange(0, 20)
        self.line_width.setSingleStep(0.5)
        self.line_width.valueChanged.connect(
            lambda v: self._apply("border_width", v, "Change Line Width")
        )
        self.font_size = QDoubleSpinBox(self)
        self.font_size.setRange(4, 144)
        self.font_size.valueChanged.connect(
            lambda v: self._apply("font_size", v, "Change Font Size")
        )
        self.author = QLineEdit(self)
        self.author.editingFinished.connect(
            lambda: self._apply("author", self.author.text(), "Change Author")
        )
        self.subject = QLineEdit(self)
        self.subject.editingFinished.connect(
            lambda: self._apply("subject", self.subject.text(), "Change Subject")
        )
        self.contents = QPlainTextEdit(self)
        self.contents.setMaximumHeight(110)
        apply_text = QPushButton("Apply Text", self)
        apply_text.clicked.connect(
            lambda: self._apply("contents", self.contents.toPlainText(), "Edit Comment Text")
        )
        self.locked = QCheckBox("Locked", self)
        self.locked.toggled.connect(
            lambda v: self._apply("locked", v, "Lock Comment" if v else "Unlock Comment")
        )

        self.form = QFormLayout()
        self.form.addRow("Color:", self.color)
        self.form.addRow("Fill:", self.fill)
        self.form.addRow("", self.no_fill)
        self.form.addRow("Opacity:", self.opacity)
        self.form.addRow("Line width:", self.line_width)
        self.form.addRow("Font size:", self.font_size)
        self.form.addRow("Author:", self.author)
        self.form.addRow("Subject:", self.subject)
        self.form.addRow("Text:", self.contents)
        self.form.addRow("", apply_text)
        self.form.addRow("", self.locked)
        self.editor = QWidget(self)
        self.editor.setLayout(self.form)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.empty = EmptyState(
            "sliders-horizontal",
            "Nothing selected",
            "Select a comment or shape on the page to change its color, line, opacity and text.",
            self,
        )
        layout.addWidget(self.heading)
        layout.addWidget(self.editor)
        layout.addWidget(self.empty, 1)
        layout.addStretch()
        self.model: AnnotationModel | None = None
        self._loading = False

    def bind(self, view: DocumentView) -> None:
        view.annotation_selection_changed.connect(self.rebuild)

    def unbind(self, view: DocumentView) -> None:
        view.annotation_selection_changed.disconnect(self.rebuild)

    def rebuild(self) -> None:
        selected = self.view.selected_models() if self.view is not None else []
        self.model = selected[0] if len(selected) == 1 else None
        self.editor.setVisible(self.model is not None)
        self.empty.setVisible(not selected)
        self.heading.setVisible(bool(selected))
        if self.model is None:
            self.heading.setText(
                f"{len(selected)} comments selected." if selected else "No comment selected."
            )
            return
        m = self.model
        self._loading = True
        try:
            self.heading.setText(f"<b>{type_label(m.type)}</b> on page {m.page_index + 1}")
            self.color.set_color(shown_color(m))
            self.fill.set_color(m.fill)
            self.no_fill.setChecked(m.fill is None)
            for w in (self.fill, self.no_fill):
                self.form.setRowVisible(w, m.type in FILL_TYPES)
            self.form.setRowVisible(
                self.line_width, m.type not in NO_BORDER and not m.type.is_markup
            )
            self.form.setRowVisible(self.font_size, m.type is AnnotationType.FREE_TEXT)
            self.opacity.setValue(round(m.opacity * 100))
            self.line_width.setValue(m.border_width)
            self.font_size.setValue(m.font_size)
            self.author.setText(m.author)
            self.subject.setText(m.subject)
            self.contents.setPlainText(m.contents)
            self.locked.setChecked(m.locked)
            self.editor.setEnabled(True)
        finally:
            self._loading = False

    def _apply(self, field: str, value: object, label: str) -> None:
        if self._loading or self.model is None or self.view is None:
            return
        before = self.model
        after = restyled(before, field, value)
        if after is None:
            return
        self.view.session.execute(
            UpdateAnnotationCommand(before, after, label, merge_key=f"inspector:{field}")
        )

    def _pick(self, field: str) -> None:
        if self.model is None:
            return
        current = getattr(self.model, field) or Color(1, 0, 0)
        chosen = pick_color(current, self, "Fill Color" if field == "fill" else "Color")
        if chosen is not None:
            self.set_color(field, chosen)

    def set_color(self, field: str, color: Color | None) -> None:
        """Apply a color (split out of the dialog so tests can call it)."""
        label = "Change Fill" if field == "fill" else "Change Color"
        self._apply(field, color, label)

    def _on_no_fill(self, checked: bool) -> None:
        if checked:
            self.set_color("fill", None)
        elif self.model is not None and self.model.fill is None:
            self.set_color("fill", Color(1, 1, 1))
