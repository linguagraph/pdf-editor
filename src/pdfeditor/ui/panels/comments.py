"""Comments panel: all comments as threads, with filters, replies, status and export."""

from __future__ import annotations

import functools
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import AddAnnotationCommand, ChangeKind
from pdfeditor.model.annotations import AnnotationModel, AnnotationType, ReviewState
from pdfeditor.services.comments import (
    Thread,
    comment_threads,
    summary_csv,
    summary_html,
    type_label,
)
from pdfeditor.services.xfdf import export_xfdf, import_command
from pdfeditor.ui.panels.base import EmptyState, ViewPanel
from pdfeditor.ui.printing import pdf_printer
from pdfeditor.ui.tools import annotate
from pdfeditor.ui.view.document_view import DocumentView

ROLE = Qt.ItemDataRole.UserRole
ALL = "All"


def add_reply(
    view: DocumentView, parent: AnnotationModel, text: str, state: ReviewState = ReviewState.NONE
) -> None:
    reply = AnnotationModel(
        AnnotationType.TEXT,
        parent.page_index,
        parent.rect,
        contents=text,
        author=view.author,
        state=state,
    )
    reply.extra = {"irt_name": parent.name}
    label = f"Set Status {state.value}" if state is not ReviewState.NONE else "Reply"
    view.session.execute(AddAnnotationCommand(reply, label))


class CommentsPanel(ViewPanel):
    title = "Comments"
    rebuild_on = frozenset({ChangeKind.STRUCTURE, ChangeKind.ANNOTATIONS})

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search comments")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._fill)
        self.type_filter = QComboBox(self)
        self.author_filter = QComboBox(self)
        self.status_filter = QComboBox(self)
        self.status_filter.addItems([ALL, *(s.value for s in ReviewState)])
        for combo in (self.type_filter, self.author_filter, self.status_filter):
            combo.currentIndexChanged.connect(self._fill)
            # let the navigation pane stay narrow: filters shrink instead of widening it
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(3)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setWordWrap(True)
        self.tree.itemClicked.connect(self._on_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)

        self.reply_button = QPushButton("Reply…", self)
        self.reply_button.clicked.connect(self.reply_to_current)
        export = QPushButton("Export", self)
        menu = QMenu(export)
        menu.addAction("Comments as XFDF…", self.export_xfdf)
        menu.addAction("Summary as CSV…", self.export_csv)
        menu.addAction("Summary as PDF…", self.export_summary_pdf)
        export.setMenu(menu)
        import_button = QPushButton("Import…", self)
        import_button.clicked.connect(self.import_xfdf)

        self.empty = EmptyState(
            "messages-square",
            "No comments yet",
            "Notes, highlights and other comments in this document are listed here, "
            "with their replies and review status.",
            self,
        )
        self.export_button = export

        self.filters = QWidget(self)
        filters = QGridLayout(self.filters)  # two rows keep the navigation pane narrow
        filters.setContentsMargins(0, 0, 0, 0)
        filters.addWidget(self.type_filter, 0, 0)
        filters.addWidget(self.author_filter, 0, 1)
        filters.addWidget(self.status_filter, 1, 0, 1, 2)
        buttons = QHBoxLayout()
        for button in (self.reply_button, import_button, export):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.search)
        layout.addWidget(self.filters)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.empty, 1)
        layout.addLayout(buttons)
        self.threads: list[Thread] = []
        self._show_empty(True)

    # -- content --------------------------------------------------------------------------
    def rebuild(self) -> None:
        self.threads = []
        if self.view is not None:
            with self.view.session.lock:
                self.threads = comment_threads(self.view.session.document)
        self._reset_filter(
            self.type_filter, sorted({type_label(t.comment.type) for t in self.threads})
        )
        authors = {a.author for t in self.threads for a in (t.comment, *t.replies) if a.author}
        self._reset_filter(self.author_filter, sorted(authors))
        self._fill()
        self._show_empty(not self.threads)

    def _show_empty(self, empty: bool) -> None:
        # Searching and filtering an empty list is pointless; importing still makes sense.
        for w in (self.search, self.filters, self.tree, self.reply_button, self.export_button):
            w.setVisible(not empty)
        self.empty.setVisible(empty)

    def badge_count(self) -> int:
        return len(self.threads)

    @staticmethod
    def _reset_filter(combo: QComboBox, values: list[str]) -> None:
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems([ALL, *values])
        combo.setCurrentIndex(max(0, combo.findText(current)))
        combo.blockSignals(False)

    def _matches(self, thread: Thread) -> bool:
        kind, author, status = (
            self.type_filter.currentText(),
            self.author_filter.currentText(),
            self.status_filter.currentText(),
        )
        entries = [thread.comment, *thread.replies]
        if kind not in ("", ALL) and type_label(thread.comment.type) != kind:
            return False
        if author not in ("", ALL) and all(a.author != author for a in entries):
            return False
        if status not in ("", ALL) and thread.status.value != status:
            return False
        needle = self.search.text().strip().lower()
        return not needle or any(needle in (a.contents + " " + a.author).lower() for a in entries)

    def _fill(self) -> None:
        self.tree.clear()
        view = self.view
        for thread in self.threads:
            if not self._matches(thread):
                continue
            c = thread.comment
            page = view.page_label(c.page_index) if view is not None else str(c.page_index + 1)
            status = f" [{thread.status.value}]" if thread.status is not ReviewState.NONE else ""
            head = f"Page {page} · {type_label(c.type)} · {c.author or 'Unknown'}{status}"
            item = QTreeWidgetItem([head + (f"\n{c.contents}" if c.contents else "")])
            item.setData(0, ROLE, c)
            item.setToolTip(0, c.contents)
            for r in thread.replies:
                if not r.contents and r.state is not ReviewState.NONE:
                    text = f"{r.author or 'Unknown'} set status to {r.state.value}"
                else:
                    text = f"{r.author or 'Unknown'}: {r.contents}"
                child = QTreeWidgetItem([text])
                child.setData(0, ROLE, r)
                item.addChild(child)
            self.tree.addTopLevelItem(item)
            item.setExpanded(True)

    def visible_count(self) -> int:
        return self.tree.topLevelItemCount()

    # -- actions --------------------------------------------------------------------------
    def current_comment(self) -> AnnotationModel | None:
        item = self.tree.currentItem()
        while item is not None and item.parent() is not None:
            item = item.parent()
        if item is None:
            return None
        model: AnnotationModel = item.data(0, ROLE)
        return model

    def _on_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        comment = self.current_comment()
        if comment is not None and self.view is not None:
            self.view.show_annotation(comment.page_index, comment.name)

    def reply_to_current(self) -> None:
        comment = self.current_comment()
        if comment is None or self.view is None:
            return
        text = annotate.ask_text(self.view, "Reply")
        if text:
            add_reply(self.view, comment, text)

    def set_status(self, state: ReviewState) -> None:
        comment = self.current_comment()
        if comment is not None and self.view is not None:
            add_reply(self.view, comment, "", state)

    def _context_menu(self, pos: QPoint) -> None:
        if self.current_comment() is None:
            return
        menu = QMenu(self)
        menu.addAction("Reply…", self.reply_to_current)
        status = menu.addMenu("Set Status")
        for state in ReviewState:
            if state is not ReviewState.NONE:
                status.addAction(state.value, functools.partial(self.set_status, state))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # -- import / export ------------------------------------------------------------------
    def _save_path(self, title: str, suffix: str, flt: str) -> Path | None:
        if self.view is None:
            return None
        base = Path(self.view.session.display_name).stem
        chosen, _ = QFileDialog.getSaveFileName(self, title, f"{base}{suffix}", flt)
        return Path(chosen) if chosen else None

    def export_xfdf(self, path: Path | None = None) -> Path | None:
        path = path or self._save_path("Export Comments", ".xfdf", "XFDF (*.xfdf)")
        if path is None or self.view is None:
            return None
        with self.view.session.lock:
            text = export_xfdf(self.view.session.document, self.view.session.display_name)
        path.write_text(text, encoding="utf-8")
        return path

    def export_csv(self, path: Path | None = None) -> Path | None:
        path = path or self._save_path("Export Comments Summary", ".csv", "CSV (*.csv)")
        if path is None or self.view is None:
            return None
        labels = [self.view.page_label(i) for i in range(self.view.page_count)]
        path.write_text(summary_csv(self.threads, labels), encoding="utf-8-sig", newline="")
        return path

    def export_summary_pdf(self, path: Path | None = None) -> Path | None:
        path = path or self._save_path("Export Comments Summary", "-comments.pdf", "PDF (*.pdf)")
        if path is None or self.view is None:
            return None
        labels = [self.view.page_label(i) for i in range(self.view.page_count)]
        doc = QTextDocument()
        doc.setHtml(summary_html(self.threads, self.view.session.display_name, labels))
        doc.print_(pdf_printer(str(path)))
        return path

    def import_xfdf(self, path: Path | None = None) -> int:
        if self.view is None:
            return 0
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Import Comments", "", "XFDF (*.xfdf *.xml)"
            )
            if not chosen:
                return 0
            path = Path(chosen)
        try:
            text = path.read_text(encoding="utf-8")
            with self.view.session.lock:
                command = import_command(text, self.view.session.document)
        except Exception as exc:  # malformed XML, unreadable file
            QMessageBox.warning(self, "Import Comments", f"Couldn't import comments:\n\n{exc}")
            return 0
        if command.commands:
            self.view.session.execute(command)
        return len(command.commands)
