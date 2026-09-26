"""Selection tool: select text or comments.

Text: drag to select, double-click for a word, triple-click for a line, Shift+click extends.
Comments: click to select (Ctrl+click adds), drag to move, drag a handle to resize,
double-click to edit, right-click for a menu. Dragging on empty space scrolls.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QGraphicsView

from pdfeditor.core.commands import Command, MacroCommand, UpdateAnnotationCommand
from pdfeditor.model.annotations import AnnotationModel, AnnotationType, transformed
from pdfeditor.model.geometry import Matrix, Point, Rect
from pdfeditor.services.text import TextPos, TextSelection
from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView

# Icons keep a fixed size; text markup follows the text it marks.
NOT_RESIZABLE = {AnnotationType.TEXT, AnnotationType.FILE_ATTACHMENT}
NOT_MOVABLE = {
    AnnotationType.HIGHLIGHT,
    AnnotationType.UNDERLINE,
    AnnotationType.STRIKEOUT,
    AnnotationType.SQUIGGLY,
}
HANDLE_PX = 6
_CURSORS = [
    Qt.CursorShape.SizeFDiagCursor,
    Qt.CursorShape.SizeVerCursor,
    Qt.CursorShape.SizeBDiagCursor,
    Qt.CursorShape.SizeHorCursor,
    Qt.CursorShape.SizeHorCursor,
    Qt.CursorShape.SizeBDiagCursor,
    Qt.CursorShape.SizeVerCursor,
    Qt.CursorShape.SizeFDiagCursor,
]


def _bounds(model: AnnotationModel) -> Rect:
    from pdfeditor.ui.view.document_view import annotation_bounds

    return annotation_bounds(model)


def _handles(r: QRectF) -> list[QPointF]:
    """TL, T, TR, L, R, BL, B, BR in scene coordinates."""
    xs = (r.left(), r.center().x(), r.right())
    ys = (r.top(), r.center().y(), r.bottom())
    return [QPointF(x, y) for y in ys for x in xs if (x, y) != (xs[1], ys[1])]


def _fit(old: Rect, new: Rect) -> Matrix:
    """Matrix mapping ``old`` onto ``new`` (scale + translate)."""
    sx = new.width / old.width if old.width else 1.0
    sy = new.height / old.height if old.height else 1.0
    return (
        Matrix.translate(-old.x0, -old.y0) @ Matrix.scale(sx, sy) @ Matrix.translate(new.x0, new.y0)
    )


class SelectTool(Tool):
    name = "select"

    def __init__(self) -> None:
        self._dragging = False
        self._panning = False
        self._last_double: tuple[float, QPointF] | None = None
        # comment drag: (page, originals, press point in page space, handle index or None)
        self._annot_drag: tuple[int, list[AnnotationModel], Point, int | None] | None = None
        self._drag_matrix: Matrix | None = None

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

    # -- comments -------------------------------------------------------------------------
    def _handle_at(self, view: DocumentView, scene: QPointF) -> tuple[AnnotationModel, int] | None:
        selected = view.selected_models()
        if len(selected) != 1:
            return None
        model = selected[0]
        if model.type in NOT_RESIZABLE or model.locked:
            return None
        box = view.page_rect_to_scene(model.page_index, _bounds(model))
        tol = HANDLE_PX / max(view.transform().m11(), 0.01)
        for i, h in enumerate(_handles(box)):
            if abs(h.x() - scene.x()) <= tol and abs(h.y() - scene.y()) <= tol:
                return model, i
        return None

    def _press_annotation(self, view: DocumentView, event: QMouseEvent, scene: QPointF) -> bool:
        handle = self._handle_at(view, scene)
        if handle is not None:
            model, index = handle
            start = view.scene_to_page(model.page_index, scene)
            self._annot_drag = (model.page_index, [model], start, index)
            return True
        annot = view.annotation_at(scene)
        if annot is None:
            view.set_annotation_selection([])
            return False
        view.clear_selection()
        key = (annot.page_index, annot.name)
        current = list(view.selected_annotations)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            current = [k for k in current if k != key] if key in current else [*current, key]
            view.set_annotation_selection(current)
            return True
        if key not in current:
            view.set_annotation_selection([key])
        movable = [
            m
            for m in view.selected_models()
            if m.page_index == annot.page_index and not m.locked and m.type not in NOT_MOVABLE
        ]
        if movable:
            start = view.scene_to_page(annot.page_index, scene)
            self._annot_drag = (annot.page_index, movable, start, None)
        return True

    def _drag_to(self, view: DocumentView, scene: QPointF) -> Matrix:
        assert self._annot_drag is not None
        page, models, start, handle = self._annot_drag
        now = view.scene_to_page(page, scene)
        if handle is None:
            return Matrix.translate(now.x - start.x, now.y - start.y)
        # resize: move the grabbed edges in scene space, then map back to page space
        old = _bounds(models[0])
        box = view.page_rect_to_scene(page, old)
        left, top, right, bottom = box.left(), box.top(), box.right(), box.bottom()
        col, row = [(0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2)][handle]
        if col == 0:
            left = min(scene.x(), right - 4)
        elif col == 2:
            right = max(scene.x(), left + 4)
        if row == 0:
            top = min(scene.y(), bottom - 4)
        elif row == 2:
            bottom = max(scene.y(), top + 4)
        corners = [QPointF(left, top), QPointF(right, bottom)]
        new = Rect.from_points(view.scene_to_page(page, c) for c in corners)
        return _fit(old, new)

    def _finish_annotation_drag(self, view: DocumentView) -> None:
        assert self._annot_drag is not None
        _page, models, _start, handle = self._annot_drag
        m = self._drag_matrix
        self._annot_drag = None
        self._drag_matrix = None
        view.set_annotation_preview({})
        if m is None or (abs(m.e) < 0.5 and abs(m.f) < 0.5 and m.a == 1 and m.d == 1):
            return
        label = "Resize Comment" if handle is not None else "Move Comment"
        commands: list[Command] = [
            UpdateAnnotationCommand(a, transformed(a, m), label) for a in models
        ]
        view.session.execute(commands[0] if len(commands) == 1 else MacroCommand(label, commands))

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        scene = view.mapToScene(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            annot = view.annotation_at(scene)
            if annot is None:
                return False
            if (annot.page_index, annot.name) not in view.selected_annotations:
                view.set_annotation_selection([(annot.page_index, annot.name)])
            view.annotation_context_menu.emit(annot, event.globalPosition().toPoint())
            return True
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._press_annotation(view, event, scene):
            return True
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
        if self._annot_drag is not None:
            scene = view.mapToScene(event.position().toPoint())
            self._drag_matrix = self._drag_to(view, scene)
            page, models, _s, _h = self._annot_drag
            view.set_annotation_preview(
                {page: [_bounds(transformed(a, self._drag_matrix)) for a in models]}
            )
            return True
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
        if self._annot_drag is not None:
            self._finish_annotation_drag(view)
            return True
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
        scene = view.mapToScene(event.position().toPoint())
        annot = view.annotation_at(scene)
        if annot is not None:
            view.annotation_activated.emit(annot)
            return True
        hit = view.page_point_at(scene)
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
        handle = self._handle_at(view, scene)
        if handle is not None:
            view.viewport().setCursor(_CURSORS[handle[1]])
            return
        annot = view.annotation_at(scene)
        if annot is not None:
            movable = not annot.locked and annot.type not in NOT_MOVABLE
            cursor = Qt.CursorShape.SizeAllCursor if movable else Qt.CursorShape.ArrowCursor
            view.viewport().setCursor(cursor)
            return
        hit = view.page_point_at(scene)
        on_text = hit is not None and view.text_cache.get(hit[0]).hit_test(hit[1]) is not None
        if on_text:
            view.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            view.viewport().unsetCursor()
