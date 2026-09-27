"""Compare Files: choose the two documents, then review the changes side by side."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.geometry import Point
from pdfeditor.services.compare import Change, CompareOptions, CompareResult, change_color
from pdfeditor.ui.view.document_view import DocumentView

PDF_FILTER = "PDF documents (*.pdf)"


class CompareFilesDialog(QDialog):
    def __init__(self, old: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare Files")
        self.old = QLineEdit(str(old) if old else "", self)
        self.new = QLineEdit(self)
        self.text = QCheckBox("Compare text (word by word)", self)
        self.text.setChecked(True)
        self.visual = QCheckBox("Compare appearance where the text is the same", self)
        self.visual.setChecked(True)
        form = QFormLayout()
        form.addRow("Old version:", self._row(self.old))
        form.addRow("New version:", self._row(self.new))
        form.addRow("", self.text)
        form.addRow("", self.visual)
        note = QLabel("Files are compared as saved on disk.", self)
        note.setEnabled(False)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Compare")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.resize(560, self.sizeHint().height())

    def _row(self, edit: QLineEdit) -> QHBoxLayout:
        browse = QPushButton("Browse…", self)

        def pick() -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Choose PDF", edit.text(), PDF_FILTER)
            if path:
                edit.setText(path)

        browse.clicked.connect(pick)
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(browse)
        return row

    def paths(self) -> tuple[Path, Path]:
        return Path(self.old.text().strip()), Path(self.new.text().strip())

    def options(self) -> CompareOptions:
        return CompareOptions(text=self.text.isChecked(), visual=self.visual.isChecked())


def _swatch(change: Change) -> QIcon:
    pix = QPixmap(12, 12)
    pix.fill(QColor.fromRgbF(*change_color(change.kind).rgb()))
    return QIcon(pix)


class CompareWindow(QDialog):
    """Old and new document side by side with the change list; the views follow each other."""

    def __init__(
        self,
        old_view: DocumentView,
        new_view: DocumentView,
        result: CompareResult,
        names: tuple[str, str],
        on_export: Callable[[], object],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Compare: {names[0]} ↔ {names[1]}")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.old_view, self.new_view, self.compare_result = old_view, new_view, result
        self._syncing = False
        self.changes = QListWidget(self)
        for n, c in enumerate(result.changes):
            where = []
            if c.page_a is not None:
                where.append(f"old p.{c.page_a + 1}")
            if c.page_b is not None:
                where.append(f"new p.{c.page_b + 1}")
            item = QListWidgetItem(_swatch(c), f"{' / '.join(where)}: {c.summary}")
            item.setData(Qt.ItemDataRole.UserRole, n)
            self.changes.addItem(item)
        if result.identical:
            self.changes.addItem("No differences found.")
        self.changes.currentRowChanged.connect(self._show_change)
        heading = QLabel(self._counts(), self)
        export = QPushButton("Export Report…", self)
        export.clicked.connect(lambda: on_export())
        side = QWidget(self)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(heading)
        side_layout.addWidget(self.changes, 1)
        side_layout.addWidget(export)
        split = QSplitter(self)
        for label, view in ((names[0], old_view), (names[1], new_view)):
            pane = QWidget(split)
            pane_layout = QVBoxLayout(pane)
            pane_layout.setContentsMargins(0, 0, 0, 0)
            pane_layout.addWidget(QLabel(label, pane))
            pane_layout.addWidget(view, 1)
            split.addWidget(pane)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        self.resize(1400, 900)
        old_view.set_extra_overlays(self._overlays(old=True))
        new_view.set_extra_overlays(self._overlays(old=False))
        old_view.current_page_changed.connect(lambda p: self._follow(p, old=True))
        new_view.current_page_changed.connect(lambda p: self._follow(p, old=False))

    def _counts(self) -> str:
        n = len(self.compare_result.changes)
        return "No differences" if not n else f"{n} change{'s' if n != 1 else ''}"

    def _overlays(self, old: bool) -> dict[int, list[tuple]]:
        out: dict[int, list[tuple]] = {}
        for c in self.compare_result.changes:
            page = c.page_a if old else c.page_b
            if page is None:
                continue
            color = QColor.fromRgbF(*change_color(c.kind).rgb())
            color.setAlpha(90)
            for r in c.rects_a if old else c.rects_b:
                out.setdefault(page, []).append((r, color))
        return out

    def partner(self, page: int, old: bool) -> int | None:
        """The aligned page on the other side (nearest one for inserted/deleted pages)."""
        pairs = self.compare_result.pairs
        for n, (a, b) in enumerate(pairs):
            if (a if old else b) != page:
                continue
            other = b if old else a
            if other is not None:
                return other
            for m in [*range(n - 1, -1, -1), *range(n + 1, len(pairs))]:
                cand = pairs[m][1] if old else pairs[m][0]
                if cand is not None:
                    return cand
        return None

    def _follow(self, page: int, old: bool) -> None:
        if self._syncing:
            return
        target_view = self.new_view if old else self.old_view
        other = self.partner(page, old)
        if other is None or other == target_view.current_page:
            return
        self._syncing = True
        try:
            target_view.go_to_page(other, record=False)
        finally:
            self._syncing = False

    def _show_change(self, row: int) -> None:
        item = self.changes.item(row)
        if item is None or item.data(Qt.ItemDataRole.UserRole) is None:
            return
        c = self.compare_result.changes[int(item.data(Qt.ItemDataRole.UserRole))]
        self._syncing = True
        try:
            for view, page, rects in (
                (self.old_view, c.page_a, c.rects_a),
                (self.new_view, c.page_b, c.rects_b),
            ):
                if page is None:
                    continue
                top = Point(0, max(0.0, rects[0].y0 - 40)) if rects else None
                view.go_to_page(page, top, record=False)
        finally:
            self._syncing = False
