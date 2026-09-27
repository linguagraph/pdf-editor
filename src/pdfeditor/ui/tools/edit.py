"""Content-editing tools: edit objects (select/move/resize/delete, edit text in place), add text,
add image and add shapes. Every change is one undoable step (document snapshot)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFileDialog, QGraphicsView

from pdfeditor.core.commands import SnapshotCommand
from pdfeditor.engine.base import Document, EngineError, UnsupportedFeature
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Point, Rect
from pdfeditor.model.objects import (
    FontChoice,
    ObjectType,
    PageObject,
    ShapeKind,
    ShapeSpec,
    TextStyle,
)
from pdfeditor.model.pages import ImageStamp
from pdfeditor.ui.tools.base import Tool
from pdfeditor.ui.view.text_editor import InlineTextEditor, viewport_rect

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView

HANDLE_PX = 6
MIN_DRAG = 3.0
DEFAULT_TEXT_STYLE = TextStyle(font="Helvetica", size=12)
# Reports to the window (status bar / message box); replaced by the window at runtime.
notify: Callable[[str], None] = lambda message: None  # noqa: E731
error: Callable[[str], None] = lambda message: None  # noqa: E731


def run_edit(view: DocumentView, label: str, operation: Callable[[Document], object]) -> bool:
    """Execute a content edit as one undo step; report refusals instead of raising."""
    result: list[object] = []

    def op(doc: Document) -> None:
        result.append(operation(doc))

    try:
        view.session.execute(SnapshotCommand(label, op, view.session.snapshots))
    except (UnsupportedFeature, EngineError) as exc:
        error(str(exc))
        return False
    choice = result[0] if result else None
    if isinstance(choice, FontChoice) and choice.substituted:
        notify(f"The document's font can't show this text; used {choice.name} instead.")
    return True


def _used(view: DocumentView, ok: bool) -> None:
    """Tell the window a creation tool finished (one-shot tools go back to Select)."""
    if ok:
        view.tool_used.emit()


def _fit(old: Rect, new: Rect) -> Matrix:
    sx = new.width / old.width if old.width else 1.0
    sy = new.height / old.height if old.height else 1.0
    return (
        Matrix.translate(-old.x0, -old.y0) @ Matrix.scale(sx, sy) @ Matrix.translate(new.x0, new.y0)
    )


def _handles(r: QRectF) -> list[QPointF]:
    xs = (r.left(), r.center().x(), r.right())
    ys = (r.top(), r.center().y(), r.bottom())
    return [QPointF(x, y) for y in ys for x in xs if (x, y) != (xs[1], ys[1])]


class EditObjectsTool(Tool):
    """Acrobat's "Edit text & images": outlines every editable object on the page."""

    name = "edit"

    def __init__(self) -> None:
        # (page, objects, press point, handle index or None)
        self._drag: tuple[int, list[PageObject], Point, int | None] | None = None
        self._matrix: Matrix | None = None
        self._marquee: tuple[int, Point] | None = None
        self.editor: InlineTextEditor | None = None

    def activate(self, view: DocumentView) -> None:
        view.setDragMode(QGraphicsView.DragMode.NoDrag)
        view.set_object_outlines(True)
        view.clear_selection()
        view.set_annotation_selection([])

    def deactivate(self, view: DocumentView) -> None:
        if self.editor is not None:
            self.editor.commit()
        view.set_object_outlines(False)
        view.set_object_selection([])
        view.set_annotation_preview({})

    # -- hit testing ----------------------------------------------------------------------
    def _handle_at(self, view: DocumentView, scene: QPointF) -> tuple[int, PageObject, int] | None:
        by_page = view.selected_object_pages()
        if sum(len(v) for v in by_page.values()) != 1:
            return None
        page, (obj,) = next(iter(by_page.items()))
        box = view.page_rect_to_scene(page, obj.bbox)
        tol = HANDLE_PX / max(view.transform().m11(), 0.01)
        for i, h in enumerate(_handles(box)):
            if abs(h.x() - scene.x()) <= tol and abs(h.y() - scene.y()) <= tol:
                return page, obj, i
        return None

    # -- mouse ----------------------------------------------------------------------------
    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        scene = view.mapToScene(event.position().toPoint())
        handle = self._handle_at(view, scene)
        if handle is not None:
            page, grabbed, index = handle
            self._drag = (page, [grabbed], view.scene_to_page(page, scene), index)
            return True
        hit = view.page_point_at(scene)
        obj = view.object_at(scene)
        if obj is None or hit is None:
            if hit is not None:
                self._marquee = (hit[0], hit[1])
            if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                view.set_object_selection([])
            return True
        page = hit[0]
        key = (page, obj.key)
        current = list(view.selected_objects)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            view.set_object_selection(
                [k for k in current if k != key] if key in current else [*current, key]
            )
            return True
        if key not in current:
            view.set_object_selection([key])
        movable = [o for o in view.selected_object_pages().get(page, []) if o.editable]
        if movable:
            self._drag = (page, movable, view.scene_to_page(page, scene), None)
        return True

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        scene = view.mapToScene(event.position().toPoint())
        if self._drag is not None:
            page, objs, start, handle = self._drag
            now = view.scene_to_page(page, scene)
            if handle is None:
                self._matrix = Matrix.translate(now.x - start.x, now.y - start.y)
            else:
                self._matrix = self._resize_matrix(view, page, objs[0], handle, scene)
            view.set_annotation_preview({page: [o.bbox.transform(self._matrix) for o in objs]})
            return True
        if self._marquee is not None:
            page, a = self._marquee
            b = view.scene_to_page(page, scene)
            view.set_annotation_preview({page: [Rect.from_points([a, b])]})
            return True
        return False

    def _resize_matrix(
        self, view: DocumentView, page: int, obj: PageObject, handle: int, scene: QPointF
    ) -> Matrix:
        box = view.page_rect_to_scene(page, obj.bbox)
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
        new = Rect.from_points(
            [
                view.scene_to_page(page, QPointF(left, top)),
                view.scene_to_page(page, QPointF(right, bottom)),
            ]
        )
        return _fit(obj.bbox, new)

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        view.set_annotation_preview({})
        if self._drag is not None:
            page, objs, _start, handle = self._drag
            m = self._matrix
            self._drag, self._matrix = None, None
            if m is None or (abs(m.e) < 0.5 and abs(m.f) < 0.5 and m.a == 1 and m.d == 1):
                return True
            keys = [o.key for o in objs]
            label = "Resize" if handle is not None else "Move"
            what = "Text" if all(o.type is ObjectType.TEXT for o in objs) else "Object"
            moved = [o.bbox.transform(m) for o in objs]
            if run_edit(
                view, f"{label} {what}", lambda doc: doc.page(page).transform_objects(keys, m)
            ):
                self._reselect(view, page, moved)
            return True
        if self._marquee is not None:
            page, a = self._marquee
            self._marquee = None
            b = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
            if abs(b.x - a.x) > MIN_DRAG or abs(b.y - a.y) > MIN_DRAG:
                box = Rect.from_points([a, b])
                view.set_object_selection(
                    [(page, o.key) for o in view.page_objects(page) if o.bbox.intersects(box)]
                )
            return True
        return False

    @staticmethod
    def _reselect(view: DocumentView, page: int, boxes: list[Rect]) -> None:
        """After an edit keys change: select the objects now closest to where they went."""
        chosen = []
        for box in boxes:
            best = min(
                view.page_objects(page),
                key=lambda o: abs(o.bbox.x0 - box.x0) + abs(o.bbox.y0 - box.y0),
                default=None,
            )
            if best is not None:
                chosen.append((page, best.key))
        view.set_object_selection(chosen)

    def double_click(self, view: DocumentView, event: QMouseEvent) -> bool:
        scene = view.mapToScene(event.position().toPoint())
        obj = view.object_at(scene)
        hit = view.page_point_at(scene)
        if obj is None or hit is None or obj.type is not ObjectType.TEXT:
            return obj is not None
        if not obj.editable:
            error(f"This text can't be edited: {obj.reason}.")
            return True
        self.edit_text(view, hit[0], obj)
        return True

    def edit_text(self, view: DocumentView, page: int, obj: PageObject) -> None:
        style = obj.style or DEFAULT_TEXT_STYLE
        k = view.transform().m11()

        def commit(text: str) -> None:
            self.editor = None
            if text != obj.text:
                run_edit(
                    view, "Edit Text", lambda doc: doc.page(page).replace_text(obj.key, text, style)
                )

        def cancel() -> None:
            self.editor = None

        self.editor = InlineTextEditor(view.viewport(), obj.text, style, k, commit, cancel)
        self.editor.place(viewport_rect(view, page, obj.bbox))

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        scene = view.mapToScene(event.position().toPoint())
        if self._handle_at(view, scene) is not None:
            view.viewport().setCursor(Qt.CursorShape.SizeFDiagCursor)
            return
        obj = view.object_at(scene)
        if obj is None:
            view.viewport().unsetCursor()
        elif obj.type is ObjectType.TEXT:
            view.viewport().setCursor(
                Qt.CursorShape.IBeamCursor if obj.editable else Qt.CursorShape.ForbiddenCursor
            )
        else:
            view.viewport().setCursor(Qt.CursorShape.SizeAllCursor)


class _RectTool(Tool):
    """Drag out a rectangle on one page (click for a default size)."""

    default_size = (200.0, 40.0)

    def __init__(self) -> None:
        self._start: tuple[int, Point] | None = None

    def activate(self, view: DocumentView) -> None:
        view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        view.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self._start = view.page_point_at(view.mapToScene(event.position().toPoint()))
        return self._start is not None

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        b = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
        view.set_annotation_preview({page: [Rect.from_points([a, b])]})
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        self._start = None
        view.set_annotation_preview({})
        b = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
        if abs(b.x - a.x) < MIN_DRAG and abs(b.y - a.y) < MIN_DRAG:
            w, h = self.default_size
            b = Point(a.x + w, a.y + h)
        bounds = view.page_rect(page)
        rect = Rect.from_points([a, b]).intersection(bounds)
        self.finish(view, page, rect, a, b)
        return True

    def finish(self, view: DocumentView, page: int, rect: Rect, a: Point, b: Point) -> None:
        raise NotImplementedError


class AddTextTool(_RectTool):
    name = "add_text"

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        """Clicking existing text edits that paragraph instead of starting a new text box."""
        if event.button() == Qt.MouseButton.LeftButton:
            scene = view.mapToScene(event.position().toPoint())
            obj = view.object_at(scene)
            hit = view.page_point_at(scene)
            if obj is not None and hit is not None and obj.type is ObjectType.TEXT and obj.editable:
                EditObjectsTool().edit_text(view, hit[0], obj)
                return True
        return super().press(view, event)

    def __init__(self) -> None:
        super().__init__()
        self.editor: InlineTextEditor | None = None
        self.style = DEFAULT_TEXT_STYLE

    def deactivate(self, view: DocumentView) -> None:
        if self.editor is not None:
            self.editor.commit()

    def finish(self, view: DocumentView, page: int, rect: Rect, a: Point, b: Point) -> None:
        style = self.style

        def commit(text: str) -> None:
            self.editor = None
            if text.strip():
                _used(
                    view,
                    run_edit(
                        view, "Add Text", lambda doc: doc.page(page).add_text(rect, text, style)
                    ),
                )

        def cancel() -> None:
            self.editor = None

        self.editor = InlineTextEditor(
            view.viewport(), "", style, view.transform().m11(), commit, cancel
        )
        self.editor.place(viewport_rect(view, page, rect))


def ask_image(view: DocumentView) -> Path | None:
    chosen, _ = QFileDialog.getOpenFileName(
        view, "Add Image", "", "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp)"
    )
    return Path(chosen) if chosen else None


class AddImageTool(_RectTool):
    name = "add_image"
    default_size = (200.0, 150.0)

    def finish(self, view: DocumentView, page: int, rect: Rect, a: Point, b: Point) -> None:
        path = ask_image(view)
        if path is None:
            return
        data = path.read_bytes()
        _used(
            view,
            run_edit(
                view, "Add Image", lambda doc: doc.page(page).stamp_image(ImageStamp(data, rect))
            ),
        )


class AddShapeTool(_RectTool):
    default_size = (120.0, 80.0)

    def __init__(self, kind: ShapeKind) -> None:
        super().__init__()
        self.kind = kind
        self.name = f"add_{kind.value}"
        self.stroke = Color(0, 0, 0)
        self.fill: Color | None = None
        self.width = 1.5

    def finish(self, view: DocumentView, page: int, rect: Rect, a: Point, b: Point) -> None:
        spec = ShapeSpec(
            self.kind,
            rect,
            a,
            b,
            self.stroke,
            None if self.kind is ShapeKind.LINE else self.fill,
            self.width,
        )
        label = {
            ShapeKind.RECTANGLE: "Add Rectangle",
            ShapeKind.ELLIPSE: "Add Ellipse",
            ShapeKind.LINE: "Add Line",
        }[self.kind]
        _used(view, run_edit(view, label, lambda doc: doc.page(page).add_shape(spec)))


def delete_selected(view: DocumentView) -> bool:
    by_page = view.selected_object_pages()
    if not by_page:
        return False

    def op(doc: Document) -> None:
        for page, objs in by_page.items():
            doc.page(page).delete_objects([o.key for o in objs])

    count = sum(len(v) for v in by_page.values())
    view.set_object_selection([])
    return run_edit(view, "Delete Object" if count == 1 else f"Delete {count} Objects", op)
