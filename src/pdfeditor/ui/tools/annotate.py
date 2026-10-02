"""Comment tools: markup from text, shapes, pen, notes, text boxes, stamps, attachments.

Tools stay active after creating a comment (like Acrobat's "keep tool selected"); press
Escape or pick another tool to stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFileDialog, QGraphicsView, QInputDialog, QMessageBox

from pdfeditor.core.commands import AddAnnotationCommand, Command, MacroCommand
from pdfeditor.model.annotations import (
    IMAGE_STAMP,
    AnnotationModel,
    AnnotationType,
    callout_line,
)
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Quad, Rect, simplify
from pdfeditor.services.custom_stamps import StampLibrary, image_size, stamp_rect
from pdfeditor.services.text import TextPos, TextSelection
from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView


@dataclass(frozen=True)
class Style:
    color: Color | None
    fill: Color | None = None
    width: float = 1.0
    opacity: float = 1.0


STYLES: dict[AnnotationType, Style] = {
    AnnotationType.HIGHLIGHT: Style(Color(1, 0.92, 0)),
    AnnotationType.UNDERLINE: Style(Color(0, 0.6, 0.2)),
    AnnotationType.STRIKEOUT: Style(Color(0.85, 0.1, 0.1)),
    AnnotationType.SQUIGGLY: Style(Color(1, 0.45, 0)),
    AnnotationType.SQUARE: Style(Color(0.85, 0.1, 0.1), width=1.5),
    AnnotationType.CIRCLE: Style(Color(0.85, 0.1, 0.1), width=1.5),
    AnnotationType.LINE: Style(Color(0.85, 0.1, 0.1), width=1.5),
    AnnotationType.POLYGON: Style(Color(0.85, 0.1, 0.1), width=1.5),
    AnnotationType.POLYLINE: Style(Color(0.85, 0.1, 0.1), width=1.5),
    AnnotationType.INK: Style(Color(0.1, 0.3, 0.9), width=2.0),
    AnnotationType.TEXT: Style(Color(1, 0.85, 0.2)),
    AnnotationType.FREE_TEXT: Style(None),
    AnnotationType.STAMP: Style(None),
    AnnotationType.FILE_ATTACHMENT: Style(Color(0.2, 0.4, 0.8)),
}

NOTE_SIZE = 20.0
IMAGE_PREFIX = "image:"  # stamp names that refer to a custom image stamp
MIN_DRAG = 3.0  # points; smaller drags count as clicks


def ask_text(view: DocumentView, title: str, initial: str = "") -> str | None:
    """Multi-line text prompt (module-level so tests can replace it)."""
    text, ok = QInputDialog.getMultiLineText(view, title, "Text:", initial)
    return text if ok else None


def ask_file(view: DocumentView) -> Path | None:
    chosen, _ = QFileDialog.getOpenFileName(view, "Attach File")
    return Path(chosen) if chosen else None


def new_model(view: DocumentView, kind: AnnotationType, page: int, rect: Rect) -> AnnotationModel:
    style = STYLES.get(kind, Style(Color(1, 0, 0)))
    return AnnotationModel(
        kind,
        page,
        rect,
        color=style.color,
        fill=style.fill,
        border_width=style.width,
        opacity=style.opacity,
        author=view.author,
    )


def commit(view: DocumentView, models: list[AnnotationModel], label: str) -> None:
    commands: list[Command] = [AddAnnotationCommand(m) for m in models]
    if not commands:
        return
    command = commands[0] if len(commands) == 1 else MacroCommand(label, commands)
    command.label = label
    view.session.execute(command)
    names = [
        (m.page_index, c.model.name)
        for m, c in zip(models, commands, strict=True)
        if isinstance(c, AddAnnotationCommand)
    ]
    view.set_annotation_selection(names)
    view.tool_used.emit()


class AnnotationTool(Tool):
    kind = AnnotationType.SQUARE
    label = "Comment"
    respects_existing = True

    def activate(self, view: DocumentView) -> None:
        view.setDragMode(QGraphicsView.DragMode.NoDrag)
        view.set_annotation_selection([])

    def deactivate(self, view: DocumentView) -> None:
        view.set_annotation_preview({})

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        view.viewport().setCursor(Qt.CursorShape.CrossCursor)

    @staticmethod
    def page_point(view: DocumentView, event: QMouseEvent) -> tuple[int, Point] | None:
        return view.page_point_at(view.mapToScene(event.position().toPoint()))

    @staticmethod
    def clamp_to_page(view: DocumentView, page: int, event: QMouseEvent) -> Point:
        """Pointer position in ``page``'s space, clamped to the page (drags may leave it)."""
        p = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
        rect = view.page_rect(page)
        return Point(min(max(p.x, rect.x0), rect.x1), min(max(p.y, rect.y0), rect.y1))


class MarkupTool(AnnotationTool):
    """Highlight / underline / strike out / squiggly: drag across text."""

    def __init__(self, kind: AnnotationType) -> None:
        self.kind = kind
        self.label = {
            AnnotationType.HIGHLIGHT: "Highlight",
            AnnotationType.UNDERLINE: "Underline",
            AnnotationType.STRIKEOUT: "Strikethrough",
            AnnotationType.SQUIGGLY: "Squiggly Underline",
        }[kind]
        self._anchor: TextPos | None = None

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        view.viewport().setCursor(Qt.CursorShape.IBeamCursor)

    def _caret(self, view: DocumentView, event: QMouseEvent, strict: bool) -> TextPos | None:
        scene = view.mapToScene(event.position().toPoint())
        hit = view.page_point_at(scene) if strict else view.page_point_nearest(scene)
        if hit is None:
            return None
        index = view.text_cache.get(hit[0])
        caret = index.hit_test(hit[1]) if strict else index.nearest(hit[1])
        return None if caret is None else TextPos(hit[0], caret)

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self._anchor = self._caret(view, event, strict=False)
        if self._anchor is not None:
            view.set_selection(TextSelection(self._anchor, self._anchor))
        return True

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._anchor is None:
            return False
        caret = self._caret(view, event, strict=False)
        if caret is not None:
            view.set_selection(TextSelection(self._anchor, caret))
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._anchor is None:
            return False
        self._anchor = None
        apply_markup(view, self.kind, self.label)
        return True


def apply_markup(
    view: DocumentView, kind: AnnotationType, label: str, note: str | None = None
) -> bool:
    """Turn the current text selection into one markup comment per page.

    The first one's text is the selected text, or ``note`` when given (a note on the text)."""
    selection = view.selection
    if selection is None or selection.is_empty:
        return False
    models: list[AnnotationModel] = []
    for page, rects in selection.rects(view.text_cache).items():
        if not rects:
            continue
        box = rects[0]
        for r in rects[1:]:
            box = box.union(r)
        model = new_model(view, kind, page, box)
        model.quads = tuple(Quad.from_rect(r) for r in rects)
        if models:
            model.contents = ""
        else:
            model.contents = selection.text(view.text_cache) if note is None else note
        models.append(model)
    view.clear_selection()
    commit(view, models, label)
    return True


class ShapeTool(AnnotationTool):
    """Rectangle, oval, line and arrow: drag from corner to corner (or end to end)."""

    def __init__(self, kind: AnnotationType, arrow: bool = False) -> None:
        self.kind = kind
        self.arrow = arrow
        self.label = (
            "Arrow"
            if arrow
            else {
                AnnotationType.SQUARE: "Rectangle",
                AnnotationType.CIRCLE: "Oval",
                AnnotationType.LINE: "Line",
            }[kind]
        )
        self._start: tuple[int, Point] | None = None

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self._start = self.page_point(view, event)
        return self._start is not None

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        b = self.clamp_to_page(view, page, event)
        view.set_annotation_preview({page: [Rect.from_points([a, b])]})
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        self._start = None
        b = self.clamp_to_page(view, page, event)
        view.set_annotation_preview({})
        if abs(b.x - a.x) < MIN_DRAG and abs(b.y - a.y) < MIN_DRAG:
            return True
        model = new_model(view, self.kind, page, Rect.from_points([a, b]))
        if self.kind is AnnotationType.LINE:
            model.vertices = (a, b)
            model.line_endings = ("None", "OpenArrow" if self.arrow else "None")
        commit(view, [model], f"Add {self.label}")
        return True


class InkTool(AnnotationTool):
    """Freehand pen; strokes are smoothed by simplification when the mouse is released."""

    kind = AnnotationType.INK
    label = "Drawing"

    def __init__(self) -> None:
        self._page: int | None = None
        self._points: list[Point] = []

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = self.page_point(view, event)
        if hit is None:
            return False
        self._page, first = hit
        self._points = [first]
        return True

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._page is None:
            return False
        self._points.append(self.clamp_to_page(view, self._page, event))
        pts = self._points
        segments = [Rect.from_points([pts[i], pts[i + 1]]) for i in range(len(pts) - 1)]
        view.set_annotation_preview({self._page: segments})
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._page is None:
            return False
        page, points = self._page, self._points
        self._page, self._points = None, []
        view.set_annotation_preview({})
        stroke = simplify(points, 0.6)
        if len(stroke) < 2:
            return True
        model = new_model(view, AnnotationType.INK, page, Rect.from_points(stroke))
        model.ink = (tuple(stroke),)
        commit(view, [model], "Add Drawing")
        return True


class PolyTool(AnnotationTool):
    """Polygon / polyline: click to add vertices, double-click (or click the first point) to
    finish, Escape cancels."""

    def __init__(self, kind: AnnotationType) -> None:
        self.kind = kind
        self.label = "Polygon" if kind is AnnotationType.POLYGON else "Polyline"
        self._page: int | None = None
        self._points: list[Point] = []
        self._last_click = 0.0

    def deactivate(self, view: DocumentView) -> None:
        self._page, self._points = None, []
        super().deactivate(view)

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = self.page_point(view, event)
        if hit is None:
            return True
        page, p = hit
        if self._page is None:
            self._page, self._points = page, [p]
            self.busy = True
        elif page == self._page:
            close = self._points and abs(p.x - self._points[0].x) + abs(p.y - self._points[0].y) < 4
            if close and len(self._points) >= 3:
                return self.finish(view)
            self._points.append(p)
        self._show(view)
        return True

    def double_click(self, view: DocumentView, event: QMouseEvent) -> bool:
        return self.finish(view)

    def _show(self, view: DocumentView) -> None:
        if self._page is None:
            return
        pts = self._points
        segments = [Rect.from_points([pts[i], pts[i + 1]]) for i in range(len(pts) - 1)]
        view.set_annotation_preview({self._page: segments or [Rect.from_points(pts)]})

    def finish(self, view: DocumentView) -> bool:
        page, points = self._page, self._points
        self._page, self._points = None, []
        self.busy = False
        view.set_annotation_preview({})
        # a double-click also delivered a press at the same spot: drop the duplicate
        deduped: list[Point] = []
        for p in points:
            if not deduped or abs(p.x - deduped[-1].x) + abs(p.y - deduped[-1].y) > 1:
                deduped.append(p)
        if page is None or len(deduped) < 2:
            return True
        model = new_model(view, self.kind, page, Rect.from_points(deduped))
        model.vertices = tuple(deduped)
        commit(view, [model], f"Add {self.label}")
        return True


class NoteTool(AnnotationTool):
    kind = AnnotationType.TEXT
    label = "Sticky Note"

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = self.page_point(view, event)
        if hit is None:
            return True
        page, p = hit
        text = ask_text(view, "Sticky Note")
        if text is None:
            return True
        model = new_model(
            view, AnnotationType.TEXT, page, Rect(p.x, p.y, p.x + NOTE_SIZE, p.y + NOTE_SIZE)
        )
        model.contents = text
        model.icon = "Comment"
        commit(view, [model], "Add Sticky Note")
        return True


class FreeTextTool(ShapeTool):
    """Text box: drag a rectangle (or click for a default size), then type."""

    def __init__(self) -> None:
        super().__init__(AnnotationType.SQUARE)
        self.kind = AnnotationType.FREE_TEXT
        self.label = "Text Box"

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        self._start = None
        b = self.clamp_to_page(view, page, event)
        view.set_annotation_preview({})
        if abs(b.x - a.x) < MIN_DRAG and abs(b.y - a.y) < MIN_DRAG:
            b = Point(a.x + 180, a.y + 40)
        text = ask_text(view, "Text Box")
        if not text:
            return True
        model = new_model(view, AnnotationType.FREE_TEXT, page, Rect.from_points([a, b]))
        model.contents = text
        model.text_color = Color(0, 0, 0)
        model.font_size = 12
        commit(view, [model], "Add Text Box")
        return True


class StampTool(AnnotationTool):
    """Click to place a stamp: a standard one by name, or a custom image stamp
    (``image:<key>`` names an image in the custom stamp library)."""

    kind = AnnotationType.STAMP
    label = "Stamp"

    def __init__(self, stamp: str = "Approved", library: StampLibrary | None = None) -> None:
        self.stamp = stamp
        self.library = library

    def image(self) -> tuple[bytes, tuple[int, int]] | None:
        """The custom stamp's image and pixel size, or ``None`` for a standard stamp."""
        if not self.stamp.startswith(IMAGE_PREFIX):
            return None
        stamp = (self.library or StampLibrary()).get(self.stamp[len(IMAGE_PREFIX) :])
        if stamp is None:
            return None
        try:
            data = stamp.read()
        except OSError:
            return None
        size = image_size(data)
        return (data, size) if size is not None else None

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = self.page_point(view, event)
        if hit is None:
            return True
        page, p = hit
        custom = self.image()
        if self.stamp.startswith(IMAGE_PREFIX) and custom is None:
            QMessageBox.warning(view, "Stamp", "The custom stamp's image can't be read.")
            return True
        if custom is not None:
            data, size = custom
            model = new_model(view, AnnotationType.STAMP, page, stamp_rect(p, size))
            model.image = data
            model.icon = IMAGE_STAMP
            model.contents = self.stamp[len(IMAGE_PREFIX) :].rsplit(".", 1)[0]
        else:
            w, h = 160.0, 50.0
            model = new_model(
                view,
                AnnotationType.STAMP,
                page,
                Rect(p.x - w / 2, p.y - h / 2, p.x + w / 2, p.y + h / 2),
            )
            model.icon = self.stamp
        commit(view, [model], "Add Stamp")
        return True


class CalloutTool(AnnotationTool):
    """Text box with a leader line: click the point to call out, drag out the box, then type.

    Dragging straight from the point also works: the box goes where the drag ends.
    """

    kind = AnnotationType.FREE_TEXT
    label = "Callout"

    def __init__(self) -> None:
        self._tip: tuple[int, Point] | None = None
        self._box_start: Point | None = None
        self._pressed_at: Point | None = None

    def deactivate(self, view: DocumentView) -> None:
        self._reset()
        super().deactivate(view)

    def _reset(self) -> None:
        self._tip, self._box_start, self._pressed_at = None, None, None
        self.busy = False

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._tip is None:
            hit = self.page_point(view, event)
            if hit is not None:
                self._tip, self._pressed_at = hit, hit[1]
                self.busy = True
                self._show(view, None)
            return True
        page = self._tip[0]
        self._box_start = self.clamp_to_page(view, page, event)
        return True

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._tip is None:
            return False
        page = self._tip[0]
        now = self.clamp_to_page(view, page, event)
        if self._box_start is not None:
            self._show(view, Rect.from_points([self._box_start, now]))
        elif self._pressed_at is not None:  # dragging from the tip
            self._show(view, _default_box(self._tip[1], now))
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._tip is None:
            return False
        page, tip = self._tip
        end = self.clamp_to_page(view, page, event)
        if self._box_start is None:
            pressed, self._pressed_at = self._pressed_at, None
            if pressed is None or _is_click(pressed, end):
                return True  # the point is set; now drag out the box
            box = _default_box(tip, end)
        elif _is_click(self._box_start, end):
            box = _default_box(tip, end)
        else:
            box = Rect.from_points([self._box_start, end])
        self._reset()
        view.set_annotation_preview({})
        text = ask_text(view, "Callout")
        if not text:
            return True
        model = new_model(view, AnnotationType.FREE_TEXT, page, box)
        model.contents = text
        model.color = Color(0, 0, 0)  # box border and leader line
        model.text_color = Color(0, 0, 0)
        model.font_size = 12
        model.vertices = callout_line(tip, box)
        model.line_endings = ("OpenArrow", "None")
        commit(view, [model], "Add Callout")
        return True

    def _show(self, view: DocumentView, box: Rect | None) -> None:
        if self._tip is None:
            return
        page, tip = self._tip
        marker = Rect(tip.x - 2, tip.y - 2, tip.x + 2, tip.y + 2)
        if box is None:
            view.set_annotation_preview({page: [marker]})
            return
        line = callout_line(tip, box)
        segments = [Rect.from_points([line[i], line[i + 1]]) for i in range(len(line) - 1)]
        view.set_annotation_preview({page: [marker, box, *segments]})


def _is_click(a: Point, b: Point) -> bool:
    return abs(b.x - a.x) < MIN_DRAG and abs(b.y - a.y) < MIN_DRAG


def _default_box(tip: Point, at: Point) -> Rect:
    """A default-size box placed at ``at``, extending away from the tip."""
    w, h = 180.0, 40.0
    x0 = at.x if at.x >= tip.x else at.x - w
    return Rect(x0, at.y - h / 2, x0 + w, at.y + h / 2)


class AttachTool(AnnotationTool):
    kind = AnnotationType.FILE_ATTACHMENT
    label = "Attach File"

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = self.page_point(view, event)
        if hit is None:
            return True
        path = ask_file(view)
        if path is None:
            return True
        page, p = hit
        model = new_model(
            view,
            AnnotationType.FILE_ATTACHMENT,
            page,
            Rect(p.x, p.y, p.x + NOTE_SIZE, p.y + NOTE_SIZE),
        )
        model.file_name = path.name
        model.file_data = path.read_bytes()
        model.contents = path.name
        commit(view, [model], "Attach File")
        return True


def tool_for(name: str) -> Tool | None:
    """Factory used by the window's tool actions."""
    factories = {
        "highlight": lambda: MarkupTool(AnnotationType.HIGHLIGHT),
        "underline": lambda: MarkupTool(AnnotationType.UNDERLINE),
        "strikeout": lambda: MarkupTool(AnnotationType.STRIKEOUT),
        "squiggly": lambda: MarkupTool(AnnotationType.SQUIGGLY),
        "rectangle": lambda: ShapeTool(AnnotationType.SQUARE),
        "oval": lambda: ShapeTool(AnnotationType.CIRCLE),
        "line": lambda: ShapeTool(AnnotationType.LINE),
        "arrow": lambda: ShapeTool(AnnotationType.LINE, arrow=True),
        "polygon": lambda: PolyTool(AnnotationType.POLYGON),
        "polyline": lambda: PolyTool(AnnotationType.POLYLINE),
        "pen": InkTool,
        "note": NoteTool,
        "textbox": FreeTextTool,
        "callout": CalloutTool,
        "attach": AttachTool,
    }
    if name.startswith("stamp:"):
        return StampTool(name.split(":", 1)[1])
    factory = factories.get(name)
    return factory() if factory else None
