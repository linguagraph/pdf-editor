"""Shared dialog frame: header, token spacing, aligned forms, collapsible sections, buttons.

Every dialog built on :class:`FormDialog` looks the same: a title with an optional one-line
explanation, a body laid out on the 4 px spacing scale, forms with right-aligned labels and
muted help text under fields, and a button row whose primary button is the default one (the
app style sheet paints ``QPushButton:default`` in the accent color, or ``role=danger`` red).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QShowEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.style.tokens import METRICS

Std = QDialogButtonBox.StandardButton


def set_role(widget: QWidget, role: str) -> None:
    """Set the style-sheet ``role`` property and re-apply the style (the property is only
    read when a widget is polished, which may already have happened)."""
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def caption(text: str, parent: QWidget | None = None) -> QLabel:
    """Muted, word-wrapped help text (colored by the app style sheet's ``role=caption``)."""
    label = QLabel(text, parent)
    label.setProperty("role", "caption")
    label.setWordWrap(True)
    return label


def form_layout(parent: QWidget | None = None) -> QFormLayout:
    """A form with right-aligned labels and token spacing, the same in every dialog."""
    form = QFormLayout(parent) if parent is not None else QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setHorizontalSpacing(METRICS.space(3))
    form.setVerticalSpacing(METRICS.space(2))
    if parent is not None:
        form.setContentsMargins(0, 0, 0, 0)
    return form


def add_row(
    form: QFormLayout,
    label: str,
    field: QWidget | QLayout,
    help_text: str = "",
) -> QLabel | None:
    """``form.addRow`` plus optional help text under the field; returns the help label.

    The field is named after its label for screen readers when it has no name of its own.
    """
    form.addRow(label, field)
    _name_fields(field, label)
    if not help_text:
        return None
    help_label = caption(help_text)
    form.addRow("", help_label)
    return help_label


def _plain(label: str) -> str:
    return label.replace("&", "").rstrip(":").strip()


def _focusable(widgets: list[QWidget]) -> list[QWidget]:
    return [w for w in widgets if w.focusPolicy() != Qt.FocusPolicy.NoFocus]


def _name_fields(field: QWidget | QLayout, label: str) -> None:
    name = _plain(label)
    if not name:
        return
    if isinstance(field, QWidget):
        widgets = [field]
    else:
        widgets = []
        for i in range(field.count()):
            item = field.itemAt(i)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widgets.append(widget)
    for w in _focusable(widgets):
        named = isinstance(w, QAbstractButton) and bool(w.text())
        if not named and not w.accessibleName():
            w.setAccessibleName(name)


class Section(QWidget):
    """A titled block whose content can be collapsed with a chevron header button.

    Long forms keep their main options visible and fold the rest (advanced options start
    collapsed). Hidden fields keep their values, so dialogs read them the same either way.
    """

    toggled = Signal(bool)

    def __init__(self, title: str, parent: QWidget | None = None, *, expanded: bool = True) -> None:
        super().__init__(parent)
        self.header = QToolButton(self)
        self.header.setText(title)
        self.header.setProperty("role", "section")
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.setAccessibleName(f"{title} section")
        self.header.toggled.connect(self.set_expanded)
        self.content = QWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.space(1))
        layout.addWidget(self.header)
        layout.addWidget(self.content)
        self._sync(expanded)

    def form(self) -> QFormLayout:
        """The content as a form (created on first use, indented under the header)."""
        current = self.content.layout()
        if isinstance(current, QFormLayout):
            return current
        form = form_layout(self.content)
        form.setContentsMargins(METRICS.space(4), 0, 0, METRICS.space(1))
        return form

    def set_layout(self, layout: QLayout) -> None:
        layout.setContentsMargins(METRICS.space(4), 0, 0, METRICS.space(1))
        self.content.setLayout(layout)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        if self.header.isChecked() != expanded:
            self.header.setChecked(expanded)  # re-enters through the toggled signal
            return
        if self.content.isHidden() != expanded:
            return  # already in that state
        self._sync(expanded)
        self.toggled.emit(expanded)
        dialog = self.window()
        if isinstance(dialog, QDialog) and dialog.isVisible():
            # Grow for the opened content; shrink back when closing it.
            layout = dialog.layout()
            if layout is not None:
                layout.activate()
            dialog.adjustSize()

    def _sync(self, expanded: bool) -> None:
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.header.setToolTip(("Hide " if expanded else "Show ") + self.header.text().lower())
        self.content.setVisible(expanded)


class FormDialog(QDialog):
    """Base for the app's dialogs.

    Subclasses add widgets to :attr:`content` (or use :meth:`add_form` / :meth:`add_section`).
    ``primary`` is the accept button's text (None for an information dialog with only Close);
    it's the default button. Override :meth:`primary_clicked` to validate before accepting.
    """

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
        *,
        window_title: str | None = None,
        primary: str | None = "OK",
        cancel: str | None = "Cancel",
        danger: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title or title)
        m = METRICS
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.space(6), m.space(5), m.space(6), m.space(5))
        outer.setSpacing(m.space(4))
        self.setMinimumWidth(m.space(115))  # 460 px: room for paths and help text

        header = QVBoxLayout()
        header.setSpacing(m.space(1))
        self.header_title = QLabel(title, self)
        self.header_title.setProperty("role", "title")
        font = QFont(self.header_title.font())
        font.setPointSizeF(font.pointSizeF() * 1.35)
        font.setWeight(QFont.Weight.DemiBold)
        self.header_title.setFont(font)
        self.header_title.setWordWrap(True)
        header.addWidget(self.header_title)
        self.header_subtitle = QLabel(subtitle, self)
        self.header_subtitle.setProperty("role", "muted")
        self.header_subtitle.setWordWrap(True)
        self.header_subtitle.setVisible(bool(subtitle))
        header.addWidget(self.header_subtitle)
        outer.addLayout(header)

        self.content = QVBoxLayout()
        self.content.setSpacing(m.space(3))
        outer.addLayout(self.content, 1)

        self.button_box = QDialogButtonBox(self)
        self.primary_button: QPushButton | None = None
        if primary is not None:
            if primary == "OK":  # standard buttons keep Qt's translations
                self.primary_button = self.button_box.addButton(Std.Ok)
            else:
                self.primary_button = self.button_box.addButton(
                    primary, QDialogButtonBox.ButtonRole.AcceptRole
                )
            self.primary_button.setDefault(True)
            if danger:
                set_role(self.primary_button, "danger")
        self.cancel_button: QPushButton | None = None
        if cancel is not None:
            standard = {"Cancel": Std.Cancel, "Close": Std.Close}.get(cancel)
            if standard is not None:
                self.cancel_button = self.button_box.addButton(standard)
            else:
                self.cancel_button = self.button_box.addButton(
                    cancel, QDialogButtonBox.ButtonRole.RejectRole
                )
            if primary is None:  # an information dialog: Enter closes it
                self.cancel_button.setDefault(True)
        self.button_box.accepted.connect(self.primary_clicked)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)

    def set_subtitle(self, text: str) -> None:
        self.header_subtitle.setText(text)
        self.header_subtitle.setVisible(bool(text))

    def add_form(self) -> QFormLayout:
        form = form_layout()
        self.content.addLayout(form)
        return form

    def add_section(self, title: str, *, expanded: bool = True) -> Section:
        section = Section(title, self, expanded=expanded)
        self.content.addWidget(section)
        return section

    def add_widget(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.content.addWidget(widget, stretch)
        return widget

    def primary_clicked(self) -> None:
        """The primary button was pressed: accept (subclasses may validate first)."""
        self.accept()

    def showEvent(self, event: QShowEvent) -> None:
        # Name what form labels describe but nothing named yet (fields added in nested forms).
        for form in self.findChildren(QFormLayout):
            for row in range(form.rowCount()):
                label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
                if label is None or field is None or not isinstance(label.widget(), QLabel):
                    continue
                target = field.widget() or field.layout()
                if target is not None:
                    _name_fields(target, label.widget().text())  # type: ignore[union-attr]
        self._align_forms()
        super().showEvent(event)

    def _align_forms(self) -> None:
        """Line up the field column of the dialog's top-level forms and section forms.

        Each QFormLayout sizes its label column on its own, so a section's fields would start
        at a different x than the main form's. Grouped forms (group boxes, tabs, stacked
        pages) keep their own column.
        """
        forms: list[tuple[QFormLayout, int]] = []
        for form in self.findChildren(QFormLayout):
            owner = form.parentWidget()
            boxed = False
            while owner is not None and owner is not self:
                if isinstance(owner, QGroupBox | QTabWidget | QStackedWidget):
                    boxed = True
                    break
                owner = owner.parentWidget()
            if not boxed:
                forms.append((form, form.contentsMargins().left()))
        labels: list[tuple[QWidget, int]] = []
        for form, indent in forms:
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    labels.append((widget, indent))
        if len(forms) < 2 or not labels:
            return
        column = max(w.sizeHint().width() + indent for w, indent in labels)
        for widget, indent in labels:
            widget.setMinimumWidth(column - indent)
            if isinstance(widget, QLabel):  # now wider than its text: keep it right-aligned
                widget.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for form, _indent in forms:
            form.invalidate()
        outer = self.layout()
        if outer is not None:
            outer.activate()  # lay out now, not on the next event loop pass
