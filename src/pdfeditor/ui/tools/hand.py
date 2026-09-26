"""Hand tool: drag to scroll."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QGraphicsView

from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView


class HandTool(Tool):
    name = "hand"

    def activate(self, view: DocumentView) -> None:
        view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        view.viewport().unsetCursor()
