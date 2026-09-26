"""Selection tool: drag over text to select it; drag elsewhere to scroll.

Double-click selects a word, triple-click a line, Shift+click extends the selection.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QGraphicsView

from pdfeditor.services.text import TextPos, TextSelection
from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView


class SelectTool(Tool):
    name = "select"

    def __init__(self) -> None:
        self._dragging = False
        self._panning = False
        self._last_double: tuple[float, QPointF] | None = None

    def activate(self, view: DocumentView) -> None:
        view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def _caret(self, view: DocumentView, scene: QPointF, strict: bool) -> TextPos | None:
        hit = view.page_point_at(scene) if strict else view.page_point_nearest(scene)
        if hit is None:
            return None
        page, point = hit
        index = view.text_cache.get(page)
        caret = index.hit_test(point) if strict else index.nearest(point)
        return None if caret is None else TextPos(page, caret)

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        scene = view.mapToScene(event.position().toPoint())
        if self._is_triple_click(event):
            hit = view.page_point_at(scene)
            if hit is not None:
                index = view.text_cache.get(hit[0])
                at = index.hit_test(hit[1])
                if at is not None:
                    start, end = index.line_range(min(at, len(index) - 1))
                    view.set_selection(TextSelection(TextPos(hit[0], start), TextPos(hit[0], end)))
                    return True
        caret = self._caret(view, scene, strict=True)
        if caret is None:
            view.clear_selection()
            # Not on text: behave like the hand tool for this drag.
            view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self._panning = True
            return False
        extend = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        anchor = view.selection.anchor if extend and view.selection is not None else caret
        view.set_selection(TextSelection(anchor, caret))
        self._dragging = True
        return True

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if not self._dragging or view.selection is None:
            return False
        pos = event.position().toPoint()
        scene = view.mapToScene(pos)
        caret = self._caret(view, scene, strict=False)
        if caret is not None:
            view.set_selection(TextSelection(view.selection.anchor, caret))
        view.ensureVisible(QRectF(scene.x(), scene.y(), 1, 1), 20, 20)
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._dragging:
            self._dragging = False
            return True
        return False

    def after_release(self, view: DocumentView) -> None:
        if self._panning:
            self._panning = False
            view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def double_click(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = view.page_point_at(view.mapToScene(event.position().toPoint()))
        if hit is None:
            return False
        index = view.text_cache.get(hit[0])
        caret = index.hit_test(hit[1])
        if caret is None:
            return False
        start, end = index.word_range(min(caret, len(index) - 1))
        view.set_selection(TextSelection(TextPos(hit[0], start), TextPos(hit[0], end)))
        self._last_double = (time.monotonic(), event.position())
        return True

    def _is_triple_click(self, event: QMouseEvent) -> bool:
        if self._last_double is None:
            return False
        when, where = self._last_double
        self._last_double = None
        interval = QApplication.doubleClickInterval() / 1000
        close = (event.position() - where).manhattanLength() < QApplication.startDragDistance()
        return time.monotonic() - when <= interval and close

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        scene = view.mapToScene(event.position().toPoint())
        hit = view.page_point_at(scene)
        on_text = hit is not None and view.text_cache.get(hit[0]).hit_test(hit[1]) is not None
        if on_text:
            view.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            view.viewport().unsetCursor()
