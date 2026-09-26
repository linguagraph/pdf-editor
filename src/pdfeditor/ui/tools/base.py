"""Interaction tools for the document view (hand, text select; annotation tools later)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QMouseEvent

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView


class Tool:
    """Mouse behavior plugged into a DocumentView.

    Handlers return True when they consumed the event; otherwise the view's default
    QGraphicsView behavior runs (e.g. hand-drag scrolling).
    """

    name = "tool"

    def activate(self, view: DocumentView) -> None:
        pass

    def deactivate(self, view: DocumentView) -> None:
        pass

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        return False

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        return False

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        return False

    def after_release(self, view: DocumentView) -> None:
        """Called after the view's default release handling (restore drag modes etc.)."""

    def double_click(self, view: DocumentView, event: QMouseEvent) -> bool:
        return False

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        """Mouse moved with no button pressed and not over a link: set the cursor."""
