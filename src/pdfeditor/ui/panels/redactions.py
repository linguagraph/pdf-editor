"""Review pending redaction marks: jump to, apply or remove them."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import ChangeKind
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.ui.panels.base import ViewPanel

ROLE = Qt.ItemDataRole.UserRole


class RedactionsPanel(ViewPanel):
    title = "Redactions"
    rebuild_on = frozenset({ChangeKind.STRUCTURE, ChangeKind.ANNOTATIONS})

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemClicked.connect(self._show)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.apply_selected = QPushButton("Apply Selected", self)
        self.remove_selected = QPushButton("Remove", self)
        self.apply_all = QPushButton("Apply All…", self)
        row = QHBoxLayout()
        for b in (self.apply_selected, self.remove_selected, self.apply_all):
            row.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.summary)
        layout.addWidget(self.list, 1)
        layout.addLayout(row)
        # wired by the Protect controller
        self.on_apply: Callable[[list[AnnotationModel] | None], object] = lambda marks: None
        self.on_remove: Callable[[list[AnnotationModel]], object] = lambda marks: None
        self.apply_selected.clicked.connect(lambda: self.on_apply(self.selected_marks()))
        self.remove_selected.clicked.connect(lambda: self.on_remove(self.selected_marks()))
        self.apply_all.clicked.connect(lambda: self.on_apply(None))

    def marks(self) -> list[AnnotationModel]:
        view = self.view
        if view is None:
            return []
        return [
            a
            for page in range(view.page_count)
            for a in view.page_annotations(page)
            if a.type is AnnotationType.REDACT
        ]

    def rebuild(self) -> None:
        self.list.clear()
        marks = self.marks()
        for mark in marks:
            label = self.view.page_label(mark.page_index) if self.view else str(mark.page_index + 1)
            what = mark.contents or "area"
            item = QListWidgetItem(f"Page {label}: {what}")
            item.setData(ROLE, mark)
            self.list.addItem(item)
        self.summary.setText(
            f"{len(marks)} pending redaction mark(s)." if marks else "No pending redaction marks."
        )
        for b in (self.apply_selected, self.remove_selected, self.apply_all):
            b.setEnabled(bool(marks))

    def selected_marks(self) -> list[AnnotationModel]:
        return [item.data(ROLE) for item in self.list.selectedItems()]

    def _show(self, item: QListWidgetItem) -> None:
        mark: AnnotationModel = item.data(ROLE)
        if self.view is not None:
            self.view.show_annotation(mark.page_index, mark.name)
