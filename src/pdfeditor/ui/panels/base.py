"""Common base for navigation-pane panels that follow the active document view."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from pdfeditor.core.commands import Change, ChangeKind
from pdfeditor.ui.view.document_view import DocumentView


class ViewPanel(QWidget):
    title = "Panel"
    # Rebuild when the document changes in one of these ways (STRUCTURE = reloaded).
    rebuild_on: frozenset[ChangeKind] = frozenset({ChangeKind.STRUCTURE})

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view: DocumentView | None = None

    def set_view(self, view: DocumentView | None) -> None:
        """Bind to a view (or ``None`` when no document is open)."""
        if self.view is not None:
            self.view.document_changed.disconnect(self._on_document_changed)
            self.unbind(self.view)
        self.view = view
        if view is not None:
            view.document_changed.connect(self._on_document_changed)
            self.bind(view)
        self.rebuild()

    def _on_document_changed(self, changes: tuple[Change, ...]) -> None:
        if any(c.kind in self.rebuild_on for c in changes):
            self.rebuild()

    def bind(self, view: DocumentView) -> None:
        """Connect to the view's signals."""

    def unbind(self, view: DocumentView) -> None:
        """Disconnect from the view's signals."""

    def rebuild(self) -> None:
        """Reload content from ``self.view`` (which may be ``None``)."""
