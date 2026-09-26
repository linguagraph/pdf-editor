"""Search panel: whole-document search with streaming results and hit navigation."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.text import SearchHit, SearchOptions
from pdfeditor.services.search import SearchQuery, compile_query, search_document
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.panels.base import ViewPanel
from pdfeditor.ui.view.document_view import DocumentView


class SearchPanel(ViewPanel):
    title = "Search"
    search_finished = Signal(int)  # number of hits
    hits_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.query_edit = QLineEdit(self)
        self.query_edit.setPlaceholderText("Find in document")
        self.query_edit.setClearButtonEnabled(True)
        self.query_edit.returnPressed.connect(self.start_search)
        self.case = QCheckBox("Match case", self)
        self.whole = QCheckBox("Whole words", self)
        self.regex = QCheckBox("Regular expression", self)
        self.search_button = QPushButton("Search", self)
        self.search_button.clicked.connect(self.start_search)
        self.prev_button = QPushButton("Previous", self)
        self.prev_button.clicked.connect(self.previous_hit)
        self.next_button = QPushButton("Next", self)
        self.next_button.clicked.connect(self.next_hit)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.progress = QProgressBar(self)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        self.progress.hide()
        self.results = QListWidget(self)
        self.results.setWordWrap(True)
        self.results.setUniformItemSizes(False)
        self.results.currentRowChanged.connect(self._on_row_changed)

        row = QHBoxLayout()
        row.addWidget(self.query_edit, 1)
        row.addWidget(self.search_button)
        nav = QHBoxLayout()
        nav.addWidget(self.prev_button)
        nav.addWidget(self.next_button)
        nav.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(row)
        for w in (self.case, self.whole, self.regex):
            layout.addWidget(w)
        layout.addLayout(nav)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.results, 1)

        self.hits: list[SearchHit] = []
        self.job: Job | None = None
        self._update_buttons()

    # -- binding --------------------------------------------------------------------------
    def unbind(self, view: DocumentView) -> None:
        self.cancel()
        view.clear_search_hits()

    def rebuild(self) -> None:
        self.hits = []
        self.results.clear()
        self.status.clear()
        self._update_buttons()
        self.hits_changed.emit()

    # -- searching ------------------------------------------------------------------------
    def query(self) -> SearchQuery:
        return SearchQuery(
            self.query_edit.text(),
            SearchOptions(
                case_sensitive=self.case.isChecked(),
                whole_word=self.whole.isChecked(),
                regex=self.regex.isChecked(),
            ),
        )

    def start_search(self) -> None:
        view = self.view
        if view is None:
            return
        self.cancel()
        view.clear_search_hits()
        self.rebuild()
        query = self.query()
        try:
            compile_query(query)
        except ValueError as exc:
            self.status.setText(str(exc).capitalize())
            return
        cache = view.text_cache
        start = view.current_page
        self.status.setText("Searching…")
        self.progress.setRange(0, view.page_count)
        self.progress.setValue(0)
        self.progress.show()

        def work(job: Job) -> int:
            hits = search_document(
                cache,
                query,
                token=job.token,
                progress=job.progress,
                on_page=lambda _page, page_hits: job.report(page_hits) if page_hits else None,
                start_page=start,
            )
            return len(hits)

        job = Job(work)
        job.partial.connect(self._on_hits)
        job.progress_changed.connect(lambda done, _total: self.progress.setValue(done))
        job.finished.connect(self._on_finished)
        job.failed.connect(lambda msg: self._on_stopped(f"Search failed: {msg}"))
        job.cancelled.connect(lambda: self._on_stopped("Search cancelled."))
        self.job = job
        job.start()

    def cancel(self) -> None:
        if self.job is not None and not self.job.done:
            self.job.cancel()
        self.job = None

    def _on_hits(self, hits: list[SearchHit]) -> None:
        if self.sender() is not self.job or self.view is None:
            return  # results from a superseded search
        first = not self.hits
        for hit in hits:
            self.hits.append(hit)
            label = self.view.page_label(hit.page_index)
            item = QListWidgetItem(f"Page {label}: {hit.context}")
            item.setToolTip(hit.context)
            self.results.addItem(item)
        self.view.add_search_hits(hits)
        self.status.setText(f"{len(self.hits)} results so far…")
        if first and self.hits:
            self.results.setCurrentRow(0)
        self._update_buttons()
        self.hits_changed.emit()

    def _on_finished(self, count: int) -> None:
        if self.sender() is not self.job:
            return
        self.progress.hide()
        self.status.setText(
            "No matches." if count == 0 else f"{count} result{'s' if count != 1 else ''}."
        )
        self._update_buttons()
        self.search_finished.emit(count)

    def _on_stopped(self, message: str) -> None:
        self.progress.hide()
        self.status.setText(message)

    # -- navigation -----------------------------------------------------------------------
    def _on_row_changed(self, row: int) -> None:
        if self.view is not None and 0 <= row < len(self.hits):
            self.view.show_search_hit(self.hits[row])
        self._update_buttons()

    def next_hit(self) -> None:
        if self.hits:
            self.results.setCurrentRow((self.results.currentRow() + 1) % len(self.hits))

    def previous_hit(self) -> None:
        if self.hits:
            self.results.setCurrentRow((self.results.currentRow() - 1) % len(self.hits))

    def _update_buttons(self) -> None:
        has = bool(self.hits)
        self.prev_button.setEnabled(has)
        self.next_button.setEnabled(has)

    def focus_query(self, text: str = "") -> None:
        if text and "\n" not in text:
            self.query_edit.setText(text)
        self.query_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.query_edit.selectAll()
