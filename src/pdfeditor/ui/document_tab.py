"""One tab: the page view, plus the page organizer when it's switched on."""

from __future__ import annotations

from PySide6.QtWidgets import QStackedWidget, QWidget

from pdfeditor.ui.organizer.organizer import OrganizerWidget
from pdfeditor.ui.view.document_view import DocumentView


class DocumentTab(QStackedWidget):
    def __init__(self, view: DocumentView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self.organizer: OrganizerWidget | None = None
        self.addWidget(view)

    @property
    def organizing(self) -> bool:
        return self.organizer is not None and self.currentWidget() is self.organizer

    def set_organizing(self, on: bool) -> None:
        if on:
            if self.organizer is None:
                self.organizer = OrganizerWidget(self.view, self)
                self.organizer.page_activated.connect(self._read_page)
                self.addWidget(self.organizer)
            self.setCurrentWidget(self.organizer)
            self.organizer.select_pages([self.view.current_page])
            self.organizer.grid.setFocus()
        else:
            self.setCurrentWidget(self.view)
            self.view.setFocus()

    def _read_page(self, index: int) -> None:
        self.set_organizing(False)
        self.view.go_to_page(index)

    def target_pages(self) -> list[int]:
        """Pages an Organize command acts on: the organizer selection, else the current page."""
        if self.organizing and self.organizer is not None:
            pages = self.organizer.selected_pages()
            if pages:
                return pages
        return [self.view.current_page]

    def close_tab(self) -> None:
        if self.organizer is not None:
            self.organizer.close_organizer()
