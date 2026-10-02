"""The scrollable, zoomable page view for one document."""

from __future__ import annotations

import bisect
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from itertools import accumulate

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QNativeGestureEvent,
    QPainter,
    QResizeEvent,
    QShowEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QMenu,
    QStyle,
    QToolTip,
    QWidget,
)

from pdfeditor.core.commands import ChangeKind
from pdfeditor.core.layout import (
    PAGE_GAP,
    LayoutMode,
    compute_layout,
    layout_bounds,
    page_at,
    view_matrix,
)
from pdfeditor.core.render_cache import THUMBNAIL_TILE, TileKey
from pdfeditor.core.session import DocumentSession, EventKind, SessionEvent
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.objects import PageObject
from pdfeditor.model.outline import Link, LinkKind
from pdfeditor.model.text import SearchHit
from pdfeditor.services.text import TextIndexCache, TextPos, TextSelection
from pdfeditor.ui.theme import current_colors, theme_manager
from pdfeditor.ui.tools.base import Tool
from pdfeditor.ui.tools.select import SelectTool
from pdfeditor.ui.view import motion
from pdfeditor.ui.view.page_item import PageItem, qrect, shadow_strips
from pdfeditor.ui.view.renderer import TileRenderer

log = logging.getLogger(__name__)

MIN_ZOOM, MAX_ZOOM = 0.05, 64.0
ZOOM_STEPS = (
    0.1, 0.25, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 32.0, 64.0
)  # fmt: skip


SELECTION_COLOR = QColor(51, 136, 255, 90)
SEARCH_HIT_COLOR = QColor(255, 225, 0, 130)
CURRENT_HIT_COLOR = QColor(255, 140, 0, 170)

# Page drop shadow, in device pixels: (spread, drop, alpha) per translucent layer. A few flat
# rounded rects per visible page cost next to nothing, unlike a blur effect per item.
SHADOW_LAYERS = ((1, 1, 30), (2, 2, 18), (4, 3, 10), (7, 4, 5))


class FitMode(Enum):
    NONE = "none"
    WIDTH = "width"
    PAGE = "page"


@dataclass(frozen=True, slots=True)
class ViewLocation:
    page_index: int
    y_fraction: float  # 0 = top of page, 1 = bottom


class DocumentView(QGraphicsView):
    current_page_changed = Signal(int)
    zoom_changed = Signal(float)
    layout_changed = Signal()
    history_changed = Signal()
    content_changed = Signal()  # page pixels changed (layers, edits): thumbnails must refresh
    link_activated = Signal(object)  # Link that isn't an internal jump (URI, launch, ...)
    selection_changed = Signal()
    annotation_selection_changed = Signal()
    object_selection_changed = Signal()
    escape_pressed = Signal()  # the window switches back to the Select tool
    back_to_select = Signal()  # chosen from the right-click menu of a tool
    tool_used = Signal()  # a creation tool made something (one-shot tools end)
    annotation_activated = Signal(object)  # AnnotationModel double-clicked
    annotation_context_menu = Signal(object, object)  # AnnotationModel, global QPoint
    note_clicked = Signal(object, object)  # AnnotationModel (sticky note), global QPoint
    document_changed = Signal(object)  # tuple[Change, ...] after the view has updated itself
    user_activity = Signal()  # mouse moved, wheel turned or pinched over the pages
    geometry_changed = Signal()  # resized, shown or hidden (floating overlays re-place)
    pointer_pressed = Signal()  # a mouse button went down on the pages
    pointer_released = Signal()  # ...and came up again (a drag or click is complete)

    def __init__(
        self, session: DocumentSession, renderer: TileRenderer, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.renderer = renderer
        renderer.register(session)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.page_outline = QColor()
        self._shadow = QColor()
        self._apply_theme()
        theme_manager().changed.connect(self._apply_theme)
        # Horizontal centring is done by _update_scene_rect: QGraphicsView's own centring is
        # off by half a scroll bar while the vertical scroll bar shows.
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.viewport().setMouseTracking(True)

        self.rotation = 0
        self.night_mode = False
        self._zoom = 1.0
        self._fit = FitMode.WIDTH
        self._mode = LayoutMode.CONTINUOUS
        self._current = 0
        self._page_rects: list[Rect] = []
        self._scene_rects: list[Rect] = []
        self._max_bottoms: list[float] = []  # running max of page bottoms, for bisecting
        self._items: list[PageItem] = []
        self._links: dict[int, tuple[int, list[Link]]] = {}
        self._labels: list[str] | None = None
        self._history: list[ViewLocation] = []
        self._history_pos = -1
        self._hover_link: Link | None = None
        self._scrolling_programmatically = False
        self.text_cache = TextIndexCache(session)
        self.selection: TextSelection | None = None
        self._selection_rects: dict[int, list[Rect]] = {}
        self._search_rects: dict[int, list[Rect]] = {}
        self._current_hit: SearchHit | None = None
        self._annots: dict[int, tuple[int, list[AnnotationModel]]] = {}
        self._objects: dict[int, tuple[int, list[PageObject]]] = {}
        self.selected_objects: list[tuple[int, str]] = []  # (page, object key)
        self.show_object_outlines = False
        self._tip_for: str | None = None
        self.selected_annotations: list[tuple[int, str]] = []  # (page, /NM name)
        self.annotation_preview: dict[int, list[Rect]] = {}  # drag outlines per page
        self.extra_overlays: dict[int, list[tuple[Rect, QColor]]] = {}  # e.g. compare changes
        self.author = ""
        self.tool: Tool = SelectTool()
        self.tool.activate(self)
        # Animated zoom: PageItem paints only cached tiles while it runs (no render requests
        # for the in-between scales), then the final scale is rendered as usual.
        self.zoom_animating = False
        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(motion.DURATION_MS)
        self._zoom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._zoom_anim.valueChanged.connect(self._on_zoom_frame)
        self._zoom_anim.finished.connect(self._on_zoom_animation_done)
        self._zoom_target = 1.0
        self._zoom_anchor = (QPointF(), QPointF())  # (scene point, viewport point)
        self._scroll_anim = QVariantAnimation(self)
        self._scroll_anim.setDuration(motion.DURATION_MS)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.valueChanged.connect(self._on_scroll_frame)
        self._scroll_anim.finished.connect(self._on_scroll_animation_done)
        self._after_scroll: Callable[[], None] | None = None

        renderer.tile_ready.connect(self._on_tile_ready)
        self.verticalScrollBar().valueChanged.connect(self._update_current_page)
        self._unsubscribe = session.subscribe(self._on_session_event)
        self.reload()

    # -- document -------------------------------------------------------------------------
    def reload(self) -> None:
        """Re-read page geometry (after open, page edits or save) and rebuild the layout."""
        with self.session.lock:
            doc = self.session.document
            self._page_rects = [doc.page(i).rect for i in range(doc.page_count)]
        self._links.clear()
        self._labels = None
        self.text_cache.clear()
        self.selection = None
        self._selection_rects = {}
        self._search_rects = {}
        self._current_hit = None
        self._annots.clear()
        self._objects.clear()
        self.selected_objects = []
        self.selected_annotations = []
        for item in self._items:
            self._scene.removeItem(item)
        self._items = [PageItem(self, i, r) for i, r in enumerate(self._page_rects)]
        for item in self._items:
            self._scene.addItem(item)
        self._current = min(self._current, max(0, len(self._items) - 1))
        self._relayout()

    def _on_session_event(self, event: SessionEvent) -> None:
        if event.kind is not EventKind.CHANGED:
            return
        kinds = {c.kind for c in event.changes}
        if ChangeKind.STRUCTURE in kinds:
            page, location = self._current, self.location()
            self.reload()
            if self.page_count:
                self._current = min(page, self.page_count - 1)
                self.go_to_page(min(location.page_index, self.page_count - 1), record=False)
        else:
            pages: set[int] = set()
            for change in event.changes:
                if change.kind in (ChangeKind.CONTENT, ChangeKind.ANNOTATIONS):
                    pages |= change.pages
            if pages:
                self._links.clear()
                for page in pages:
                    if 0 <= page < len(self._items):
                        self._items[page].update()
                self.content_changed.emit()
        if self.selected_objects:
            self.set_object_selection([])  # object keys belong to the previous page revision
        if self.selected_annotations:
            alive = [
                (page, name)
                for page, name in self.selected_annotations
                if 0 <= page < self.page_count and self.annotation_by_name(page, name) is not None
            ]
            if alive != self.selected_annotations:
                self.set_annotation_selection(alive)
        self.document_changed.emit(tuple(event.changes))

    def close_view(self) -> None:
        self._zoom_anim.stop()
        self._scroll_anim.stop()
        with contextlib.suppress(RuntimeError, TypeError):  # already disconnected
            theme_manager().changed.disconnect(self._apply_theme)
        self._unsubscribe()
        self.renderer.tile_ready.disconnect(self._on_tile_ready)
        self.renderer.unregister(self.session)

    @property
    def page_count(self) -> int:
        return len(self._page_rects)

    @property
    def render_variant(self) -> str:
        return "night" if self.night_mode else ""

    def page_label(self, index: int) -> str:
        if self._labels is None:
            with self.session.lock:
                doc = self.session.document
                self._labels = [doc.page_label(i) for i in range(doc.page_count)]
        return self._labels[index] if 0 <= index < len(self._labels) else str(index + 1)

    def page_index_for_label(self, text: str) -> int | None:
        """Resolve user input: an exact page label first, then a 1-based page number."""
        text = text.strip()
        if not text:
            return None
        for i in range(self.page_count):
            if self.page_label(i) == text:
                return i
        if text.isdigit() and 1 <= int(text) <= self.page_count:
            return int(text) - 1
        return None

    # -- layout ---------------------------------------------------------------------------
    @property
    def layout_mode(self) -> LayoutMode:
        return self._mode

    def set_layout_mode(self, mode: LayoutMode) -> None:
        if mode is self._mode:
            return
        page = self._current
        self._mode = mode
        self._relayout()
        self.go_to_page(page, record=False)

    def rotate_view(self, delta: int) -> None:
        page = self._current
        self.rotation = (self.rotation + delta) % 360
        self.renderer.cancel(lambda k: k.doc_id == self.session.id)
        for item in self._items:
            item.set_rotation(self.rotation)
        self._relayout()
        self.go_to_page(page, record=False)

    def _apply_theme(self) -> None:
        colors = current_colors()
        self.setBackgroundBrush(QColor(colors.canvas))
        self.page_outline = QColor(colors.page_outline)
        self._shadow = QColor(colors.shadow)
        self.viewport().update()

    def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:
        super().drawBackground(painter, rect)
        if not self._scene_rects:
            return
        transform = painter.worldTransform()
        if transform.m11() * PAGE_GAP < 2:  # pages nearly touch: a shadow would only smudge
            return
        # Drawn in whole device pixels so the shadow is the same size at every zoom, and only
        # around each page (never under it): a handful of thin non-antialiased fills.
        painter.save()
        painter.resetTransform()
        color = QColor(self._shadow)
        for i in self._pages_in(QRectF(rect)):
            page = transform.mapRect(qrect(self._scene_rects[i])).toAlignedRect()
            for spread, drop, alpha in SHADOW_LAYERS:
                color.setAlpha(alpha)
                for strip in shadow_strips(page, spread, drop):
                    painter.fillRect(strip, color)
        painter.restore()

    def _pages_in(self, rect: QRectF) -> list[int]:
        """Visible pages whose shadow may reach into scene ``rect`` (a bisect, not a scan)."""
        if self._mode is LayoutMode.SINGLE:
            return [self._current] if self._scene_rects else []
        margin = PAGE_GAP
        top, bottom = rect.top() - margin, rect.bottom() + margin
        out: list[int] = []
        i = bisect.bisect_left(self._max_bottoms, top)
        while i < len(self._scene_rects) and self._scene_rects[i].y0 <= bottom:
            out.append(i)
            i += 1
        # the right-hand page of a two-up row can start above its left neighbour
        if (
            i < len(self._scene_rects)
            and self._mode is not LayoutMode.CONTINUOUS
            and self._scene_rects[i].y0 - margin <= bottom
        ):
            out.append(i)
        return out

    def set_night_mode(self, on: bool) -> None:
        self.night_mode = on
        self.renderer.cancel(lambda k: k.doc_id == self.session.id)
        self.viewport().update()

    def _relayout(self) -> None:
        sizes = [(r.width, r.height) for r in self._page_rects]
        self._scene_rects = compute_layout(sizes, self._mode, self.rotation)
        self._max_bottoms = list(accumulate((r.y1 for r in self._scene_rects), max))
        for item, rect in zip(self._items, self._scene_rects, strict=True):
            item.setPos(rect.x0, rect.y0)
        self._apply_single_page_visibility()
        if self._fit is not FitMode.NONE:
            self._apply_fit()
        self.layout_changed.emit()

    def _apply_single_page_visibility(self) -> None:
        if self._scene_rects:
            single = self._mode is LayoutMode.SINGLE
            for i, item in enumerate(self._items):
                item.setVisible(not single or i == self._current)
        self._update_scene_rect()

    def _update_scene_rect(self) -> None:
        """Scene rect: the pages plus gaps, widened to the viewport so pages sit centred."""
        if not self._scene_rects:
            self._scene.setSceneRect(0, 0, 1, 1)
            return
        if self._mode is LayoutMode.SINGLE:
            r = self._scene_rects[self._current].inflated(PAGE_GAP)
        else:
            r = layout_bounds(self._scene_rects)
        k = self.transform().m11()
        vw = self.viewport().width()
        # QGraphicsView decides on the horizontal scroll bar with the style's default scroll
        # bar width, not the (narrower) styled one: stay within both.
        fits = self.maximumViewportSize().width()
        if self.verticalScrollBar().isVisible():
            fits -= self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, self)
        width = min(vw, fits)
        if k > 0 and r.width * k < width:
            # Qt also rounds the scene's pixel edges outwards, so start on a whole pixel and
            # end a hair inside.
            left_px = round((r.x0 + r.x1) / 2 * k - vw / 2)
            r = Rect((left_px + 1e-6) / k, r.y0, (left_px + width - 1e-6) / k, r.y1)
        rect = qrect(r)
        if rect != self._scene.sceneRect():
            self._scene.setSceneRect(rect)

    # -- zoom -----------------------------------------------------------------------------
    @property
    def zoom(self) -> float:
        """1.0 = 100% = physical size (1 pt = 1/72 inch at the screen's logical DPI)."""
        return self._zoom

    @property
    def fit_mode(self) -> FitMode:
        return self._fit

    def _points_to_pixels(self) -> float:
        return self.logicalDpiX() / 72.0

    def set_zoom(self, zoom: float, fit: FitMode = FitMode.NONE) -> None:
        zoom = min(MAX_ZOOM, max(MIN_ZOOM, zoom))
        self._fit = fit
        if abs(zoom - self._zoom) < 1e-9 and not self.transform().isIdentity():
            return
        self._zoom = zoom
        k = zoom * self._points_to_pixels()
        self.setTransform(QTransform.fromScale(k, k))
        self._update_scene_rect()
        current_scale = k * self.devicePixelRatioF()
        # Drop queued tiles for other zoom levels; they'd only delay the ones we need now.
        self.renderer.cancel(
            lambda key: (
                key.doc_id == self.session.id
                and key.tile_x != THUMBNAIL_TILE
                and abs(key.scale - current_scale) > 0.05 * current_scale
            )
        )
        self.zoom_changed.emit(zoom)

    def zoom_in(self, animated: bool = True) -> None:
        base = self._zoom_target if self.zoom_animating else self._zoom
        target = next((z for z in ZOOM_STEPS if z > base + 1e-6), MAX_ZOOM)
        self.zoom_at(target, self._viewport_center(), animated)

    def zoom_out(self, animated: bool = True) -> None:
        base = self._zoom_target if self.zoom_animating else self._zoom
        target = next((z for z in reversed(ZOOM_STEPS) if z < base - 1e-6), MIN_ZOOM)
        self.zoom_at(target, self._viewport_center(), animated)

    def _viewport_center(self) -> QPointF:
        return QPointF(self.viewport().width() / 2, self.viewport().height() / 2)

    def zoom_at(self, zoom: float, pos: QPointF, animated: bool = False) -> None:
        """Zoom keeping the document point under viewport position ``pos`` where it is.

        ``animated`` eases there over ~150 ms (when the system allows animations); a zoom
        requested while one runs continues from the current value towards the new target.
        """
        zoom = min(MAX_ZOOM, max(MIN_ZOOM, zoom))
        running = self._zoom_anim.state() is QAbstractAnimation.State.Running
        if running and self._zoom_anchor[1] == pos:
            scene = self._zoom_anchor[0]  # same anchor: no rounding drift between frames
        else:
            scene = self.mapToScene(0, 0) + QPointF(pos) / max(self.transform().m11(), 1e-9)
        self._zoom_anchor = (scene, QPointF(pos))
        if not animated or not motion.animations_enabled():
            self._zoom_anim.stop()
            self._set_zoom_anchored(zoom)
            return
        self._zoom_target = zoom
        self._zoom_anim.stop()
        self._zoom_anim.setStartValue(self._zoom)
        self._zoom_anim.setEndValue(zoom)
        self.zoom_animating = True
        self._zoom_anim.start()

    def _set_zoom_anchored(self, zoom: float) -> None:
        scene, pos = self._zoom_anchor
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        try:
            self.set_zoom(zoom)
        finally:
            self.setTransformationAnchor(anchor)
        # where the anchor ended up (mapFromScene would round to whole pixels)
        now = (scene - self.mapToScene(0, 0)) * self.transform().m11()
        h, v = self.horizontalScrollBar(), self.verticalScrollBar()
        h.setValue(h.value() + round(now.x() - pos.x()))
        v.setValue(v.value() + round(now.y() - pos.y()))

    def _on_zoom_frame(self, value: object) -> None:
        if self.zoom_animating and isinstance(value, float):
            self._set_zoom_anchored(value)

    def _on_zoom_animation_done(self) -> None:
        self.zoom_animating = False
        self.viewport().update()  # now request tiles at the final scale

    def stop_animations(self) -> None:
        """Jump to the end of running zoom/scroll animations (the user took over)."""
        if self._zoom_anim.state() is QAbstractAnimation.State.Running:
            self._zoom_anim.stop()
            self._set_zoom_anchored(self._zoom_target)
            self._on_zoom_animation_done()
        if self._scroll_anim.state() is QAbstractAnimation.State.Running:
            end = self._scroll_anim.endValue()
            self._scroll_anim.stop()
            self._scrolling_programmatically = True  # keep the page the jump chose
            try:
                self._on_scroll_frame(end)
            finally:
                self._scrolling_programmatically = False
            self._on_scroll_animation_done()

    def fit_width(self) -> None:
        self._fit = FitMode.WIDTH
        self._apply_fit()

    def fit_page(self) -> None:
        self._fit = FitMode.PAGE
        self._apply_fit()

    def _apply_fit(self) -> None:
        if not self._scene_rects:
            return
        vw = max(self.viewport().width() - 2, 50)
        vh = max(self.viewport().height() - 2, 50)
        bounds = layout_bounds(self._scene_rects)
        k_per_zoom = self._points_to_pixels()
        page = self._scene_rects[self._current]
        if self._fit is FitMode.WIDTH:
            width = (
                bounds.width if self._mode is not LayoutMode.SINGLE else page.width + 2 * PAGE_GAP
            )
            zoom = vw / width / k_per_zoom
        else:
            row = [r for r in self._scene_rects if r.y0 < page.y1 and r.y1 > page.y0]
            row_bounds = row[0]
            for r in row[1:]:
                row_bounds = row_bounds.union(r)
            zoom = (
                min(vw / (row_bounds.width + 2 * PAGE_GAP), vh / (row_bounds.height + 2 * PAGE_GAP))
                / k_per_zoom
            )
        fit = self._fit
        self.set_zoom(zoom, fit)
        if fit is FitMode.PAGE:
            self.go_to_page(self._current, record=False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._fit is not FitMode.NONE:
            self._apply_fit()
        self.geometry_changed.emit()

    def set_top_margin(self, px: int) -> None:
        """Room above the pages for a row of controls (the mode banner), outside the page
        area so it never covers a page."""
        if self.viewportMargins().top() == px:
            return
        self.setViewportMargins(0, px, 0, 0)
        if self._fit is not FitMode.NONE:
            self._apply_fit()
        self.geometry_changed.emit()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.geometry_changed.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self.geometry_changed.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120
            if steps:
                if self._scroll_anim.state() is QAbstractAnimation.State.Running:
                    self.stop_animations()
                base = self._zoom_target if self.zoom_animating else self._zoom
                # high-resolution wheels and touchpads send many small steps: don't animate
                # those, they already arrive as a smooth stream
                self.zoom_at(base * (1.1**steps), event.position(), animated=abs(steps) >= 1)
            event.accept()
            return
        self.stop_animations()
        super().wheelEvent(event)

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:  # also when a scroll bar comes or goes
            self._update_scene_rect()
            self.geometry_changed.emit()  # overlays in the viewport re-place
        if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Wheel, QEvent.Type.NativeGesture):
            self.user_activity.emit()
        if (
            event.type() == QEvent.Type.NativeGesture
            and isinstance(event, QNativeGestureEvent)
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            self.stop_animations()
            self.zoom_at(self._zoom * (1.0 + event.value()), event.position())
            event.accept()
            return True
        return super().viewportEvent(event)

    # -- navigation -----------------------------------------------------------------------
    @property
    def current_page(self) -> int:
        return self._current

    def _set_current(self, index: int) -> None:
        if index != self._current and 0 <= index < self.page_count:
            self._current = index
            self.current_page_changed.emit(index)

    def _update_current_page(self) -> None:
        if self._mode is LayoutMode.SINGLE or not self._scene_rects:
            return
        if self._scrolling_programmatically:
            return
        if self._scroll_anim.state() is QAbstractAnimation.State.Running:
            return  # a page jump is under way; it already set the current page
        top = self.mapToScene(0, 0).y()
        bottom = self.mapToScene(0, self.viewport().height()).y()
        self._set_current(page_at(self._scene_rects, top + (bottom - top) / 3))

    def location(self) -> ViewLocation:
        if not self._scene_rects:
            return ViewLocation(0, 0.0)
        rect = self._scene_rects[self._current]
        top = self.mapToScene(0, 0).y()
        frac = (top - rect.y0) / rect.height if rect.height else 0.0
        return ViewLocation(self._current, min(1.0, max(0.0, frac)))

    def go_to_page(
        self, index: int, point: Point | None = None, record: bool = True, animated: bool = False
    ) -> None:
        """Scroll so page ``index`` (optionally the page-space ``point``) is at the top.

        ``animated`` (user-initiated jumps) eases the last stretch of the scroll over ~150 ms
        when the system allows animations; the current page changes immediately either way.
        """
        if not 0 <= index < self.page_count:
            return
        self.stop_animations()
        if record:
            self._push_history()
        previous = self._current
        self._current = index
        if self._mode is LayoutMode.SINGLE:
            self._apply_single_page_visibility()
        rect = self._scene_rects[index]
        target_y = rect.y0 - PAGE_GAP / 2
        target_x: float | None = None
        if point is not None:
            local = point.transform(view_matrix(self._page_rects[index], self.rotation, 1.0))
            target_y = rect.y0 + local.y
            target_x = rect.x0 + local.x
        k = self.transform().m11()
        scene_top = self.sceneRect().top()
        bar = self.verticalScrollBar()
        target = min(bar.maximum(), max(bar.minimum(), round((target_y - scene_top) * k)))
        animate = (
            animated
            and self._mode is not LayoutMode.SINGLE
            and target != bar.value()
            and motion.animations_enabled()
        )
        # In two-up rows the scroll position alone can't tell which page was requested.
        self._scrolling_programmatically = True
        try:
            if animate:
                # Long jumps start a screen away from the target, so only pages near it get
                # painted (and rendered) on the way.
                reach = self.viewport().height()
                start = max(target - reach, min(target + reach, bar.value()))
                bar.setValue(start)
            else:
                bar.setValue(target)
            if target_x is not None and self.horizontalScrollBar().maximum() > 0:
                left = self.sceneRect().left()
                self.horizontalScrollBar().setValue(round((target_x - left) * k))
        finally:
            self._scrolling_programmatically = False
        if index != previous:
            self.current_page_changed.emit(index)
        if animate:
            self._after_scroll = self._push_history if record else None
            self._scroll_anim.setStartValue(bar.value())
            self._scroll_anim.setEndValue(target)
            self._scroll_anim.start()
        elif record:
            self._push_history()

    def _on_scroll_frame(self, value: object) -> None:
        if isinstance(value, int):
            self.verticalScrollBar().setValue(value)

    def _on_scroll_animation_done(self) -> None:
        after, self._after_scroll = self._after_scroll, None
        if after is not None:
            after()

    # The commands behind buttons, menus and keys: user-initiated, so they animate.
    def next_page(self, animated: bool = True) -> None:
        step = 2 if self._mode in (LayoutMode.TWO_UP, LayoutMode.TWO_UP_COVER) else 1
        target = min(self.page_count - 1, self._current + step)
        self.go_to_page(target, record=False, animated=animated)

    def previous_page(self, animated: bool = True) -> None:
        step = 2 if self._mode in (LayoutMode.TWO_UP, LayoutMode.TWO_UP_COVER) else 1
        self.go_to_page(max(0, self._current - step), record=False, animated=animated)

    def first_page(self, animated: bool = True) -> None:
        self.go_to_page(0, animated=animated)

    def last_page(self, animated: bool = True) -> None:
        self.go_to_page(self.page_count - 1, animated=animated)

    # history: a browser-style list; go_to_page records where we were and where we went
    def _push_history(self) -> None:
        loc = self.location()
        if self._history_pos >= 0 and self._history[self._history_pos] == loc:
            return
        del self._history[self._history_pos + 1 :]
        self._history.append(loc)
        self._history_pos = len(self._history) - 1
        self.history_changed.emit()

    @property
    def can_go_back(self) -> bool:
        return self._history_pos > 0

    @property
    def can_go_forward(self) -> bool:
        return self._history_pos < len(self._history) - 1

    def go_back(self) -> None:
        if self.can_go_back:
            if self._history_pos == len(self._history) - 1 and self._history[-1] != self.location():
                self._push_history()  # remember where we are so forward returns here
            self._history_pos -= 1
            self._restore(self._history[self._history_pos])

    def go_forward(self) -> None:
        if self.can_go_forward:
            self._history_pos += 1
            self._restore(self._history[self._history_pos])

    def _restore(self, loc: ViewLocation) -> None:
        self.go_to_page(loc.page_index, record=False)
        scene_rect = self._scene_rects[loc.page_index]
        k = self.transform().m11()
        y = scene_rect.y0 + loc.y_fraction * scene_rect.height - self.sceneRect().top()
        self.verticalScrollBar().setValue(round(y * k))
        self.history_changed.emit()

    def delete_selected_annotations(self) -> bool:
        from pdfeditor.core.commands import Command, DeleteAnnotationsCommand, MacroCommand

        by_page: dict[int, list[str]] = {}
        for model in self.selected_models():
            if not model.locked:
                by_page.setdefault(model.page_index, []).append(model.name)
        if not by_page:
            return False
        commands: list[Command] = [DeleteAnnotationsCommand(p, n) for p, n in by_page.items()]
        label = "Delete Comments" if sum(map(len, by_page.values())) > 1 else "Delete Comment"
        self.set_annotation_selection([])
        self.session.execute(commands[0] if len(commands) == 1 else MacroCommand(label, commands))
        return True

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_annotations:
            self.delete_selected_annotations()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_objects:
            from pdfeditor.ui.tools.edit import delete_selected

            delete_selected(self)
            return
        if key == Qt.Key.Key_Escape:
            self.set_annotation_selection([])
            self.set_object_selection([])
            self.clear_selection()
            if self.tool.name != "select":
                self.escape_pressed.emit()
            return
        self.stop_animations()
        single = self._mode is LayoutMode.SINGLE
        if key == Qt.Key.Key_Home and not event.modifiers():
            self.first_page()
        elif key == Qt.Key.Key_End and not event.modifiers():
            self.last_page()
        elif single and key in (Qt.Key.Key_PageDown, Qt.Key.Key_Right):
            self.next_page()
        elif single and key in (Qt.Key.Key_PageUp, Qt.Key.Key_Left):
            self.previous_page()
        else:
            super().keyPressEvent(event)

    # -- hit testing & links --------------------------------------------------------------
    def page_point_at(self, scene_pos: QPointF) -> tuple[int, Point] | None:
        """Page index and page-space point under a scene position, if it's over a page."""
        for i, rect in enumerate(self._scene_rects):
            if not self._items[i].isVisible():
                continue
            if rect.contains(Point(scene_pos.x(), scene_pos.y())):
                local = Point(scene_pos.x() - rect.x0, scene_pos.y() - rect.y0)
                m = view_matrix(self._page_rects[i], self.rotation, 1.0).inverted()
                return i, local.transform(m)
        return None

    def page_links(self, index: int) -> list[Link]:
        with self.session.lock:
            page = self.session.document.page(index)
            rev = page.revision
            cached = self._links.get(index)
            if cached is None or cached[0] != rev:
                cached = (rev, page.links())
                self._links[index] = cached
        return cached[1]

    def link_at(self, scene_pos: QPointF) -> Link | None:
        hit = self.page_point_at(scene_pos)
        if hit is None:
            return None
        index, point = hit
        return next((ln for ln in self.page_links(index) if ln.rect.contains(point)), None)

    def activate_link(self, link: Link) -> None:
        if link.kind is LinkKind.GOTO and link.dest is not None:
            self.go_to_page(link.dest.page_index, link.dest.point, animated=True)
        else:
            self.link_activated.emit(link)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() and self.tool.move(self, event):
            event.accept()
            return
        super().mouseMoveEvent(event)
        if event.buttons():
            return
        link = self.link_at(self.mapToScene(event.position().toPoint()))
        if link is not self._hover_link:
            self._hover_link = link
            if link is not None:
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                tip = (
                    link.uri
                    or link.file
                    or (f"Go to page {self.page_label(link.dest.page_index)}" if link.dest else "")
                )
                if tip:
                    QToolTip.showText(event.globalPosition().toPoint(), tip, self)
            else:
                QToolTip.hideText()
        if link is None:
            self.tool.hover(self, event)
            self._annotation_tip(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.stop_animations()
        self.pointer_pressed.emit()
        if event.button() == Qt.MouseButton.LeftButton:
            link = self.link_at(self.mapToScene(event.position().toPoint()))
            if link is not None:
                self.activate_link(link)
                event.accept()
                return
        if self._select_existing(event):
            event.accept()
            return
        if self.tool.press(self, event):
            event.accept()
            return
        super().mousePressEvent(event)

    def _select_existing(self, event: QMouseEvent) -> bool:
        """With a creation tool, a click on an existing comment selects it (no new comment)."""
        if not self.tool.respects_existing or self.tool.busy:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        annot = self.annotation_at(self.mapToScene(event.position().toPoint()))
        if annot is None:
            return False
        self.set_annotation_selection([(annot.page_index, annot.name)])
        return True

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.tool.release(self, event):
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        self.tool.after_release(self)
        self.pointer_released.emit()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self.tool.respects_existing and not self.tool.busy:
            annot = self.annotation_at(self.mapToScene(event.position().toPoint()))
            if annot is not None:
                self.annotation_activated.emit(annot)
                event.accept()
                return
        if self.tool.double_click(self, event):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _annotation_tip(self, event: QMouseEvent) -> None:
        """Hovering a comment shows its text and replies (any tool)."""
        annot = self.annotation_at(self.mapToScene(event.position().toPoint()))
        if annot is None:
            if self._tip_for is not None:
                self._tip_for = None
                QToolTip.hideText()
            return
        if self._tip_for == annot.name:
            return
        self._tip_for = annot.name
        text = annotation_tip_text(annot, self.page_annotations(annot.page_index))
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text, self)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self.tool.name in ("select", "hand"):
            super().contextMenuEvent(event)
            return
        menu = QMenu(self)
        menu.addAction("Back to Select Tool", self.back_to_select.emit)
        menu.exec(event.globalPos())

    # -- tools ----------------------------------------------------------------------------
    def set_tool(self, tool: Tool) -> None:
        self.tool.deactivate(self)
        self.tool = tool
        tool.activate(self)
        self.viewport().unsetCursor()

    def page_point_nearest(self, scene_pos: QPointF) -> tuple[int, Point] | None:
        """Like :meth:`page_point_at`, but snaps to the closest visible page (for drags)."""
        visible = [i for i, item in enumerate(self._items) if item.isVisible()]
        if not visible:
            return None
        p = Point(scene_pos.x(), scene_pos.y())

        def dist(i: int) -> float:
            r = self._scene_rects[i]
            return max(r.y0 - p.y, 0.0, p.y - r.y1) * 4 + max(r.x0 - p.x, 0.0, p.x - r.x1)

        i = min(visible, key=dist)
        r = self._scene_rects[i]
        clamped = QPointF(min(max(p.x, r.x0), r.x1), min(max(p.y, r.y0), r.y1))
        return self.page_point_at(clamped) or (i, Point(0, 0))

    def page_rect_to_scene(self, index: int, rect: Rect) -> QRectF:
        m = view_matrix(self._page_rects[index], self.rotation, 1.0)
        scene = self._scene_rects[index]
        return qrect(rect.transform(m).translated(scene.x0, scene.y0))

    # -- selection ------------------------------------------------------------------------
    def set_selection(self, selection: TextSelection | None) -> None:
        if selection == self.selection:
            return
        pages = self._selection_pages(self.selection) | self._selection_pages(selection)
        self.selection = selection
        # highlight boxes are worked out per page when it's painted: "select all" on a
        # 1000-page document then costs nothing until pages scroll into view
        self._selection_rects = {}
        for page in pages:
            self._items[page].update()
        self.selection_changed.emit()

    @staticmethod
    def _selection_pages(selection: TextSelection | None) -> set[int]:
        if selection is None or selection.is_empty:
            return set()
        return set(range(selection.start.page, selection.end.page + 1))

    def _selection_rects_for(self, index: int) -> list[Rect]:
        if self.selection is None or not self.selection.covers(index):
            return []
        rects = self._selection_rects.get(index)
        if rects is None:
            rects = self._selection_rects[index] = self.selection.page_rects(self.text_cache, index)
        return rects

    def clear_selection(self) -> None:
        self.set_selection(None)

    def visible_pages(self) -> list[int]:
        """Pages at least partly inside the viewport."""
        if not self._scene_rects:
            return []
        area = self.mapToScene(self.viewport().rect()).boundingRect()
        return [
            i
            for i in self._pages_in(area)
            if self._items[i].isVisible() and qrect(self._scene_rects[i]).intersects(area)
        ]

    def viewport_rect(self, rects: list[tuple[int, Rect]]) -> QRect | None:
        """Bounds in viewport pixels of page-space ``rects`` (page, rect), clipped to the
        viewport; None when none of them is in view."""
        port = self.viewport().rect()
        out = QRect()
        for page, rect in rects:
            if 0 <= page < self.page_count:
                box = self.mapFromScene(self.page_rect_to_scene(page, rect)).boundingRect()
                out = out.united(box & port)
        return out if out.isValid() and not out.isEmpty() else None

    def selection_viewport_rect(self) -> QRect | None:
        """Where the selected text is on screen (its visible part), in viewport pixels."""
        if not self.has_selection():
            return None
        visible = [p for p in self.visible_pages() if self.selection and self.selection.covers(p)]
        return self.viewport_rect([(p, r) for p in visible for r in self._selection_rects_for(p)])

    def has_selection(self) -> bool:
        return self.selection is not None and not self.selection.is_empty

    def selected_text(self) -> str:
        if self.selection is None or self.selection.is_empty:
            return ""
        return self.selection.text(self.text_cache)

    def copy_selection(self) -> bool:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)  # may extract many pages
        try:
            text = self.selected_text()
        finally:
            QApplication.restoreOverrideCursor()
        if text:
            QApplication.clipboard().setText(text)
        return bool(text)

    def select_all(self) -> None:
        if not self.page_count:
            return
        last = self.page_count - 1
        end = TextPos(last, len(self.text_cache.get(last)))
        self.set_selection(TextSelection(TextPos(0, 0), end))

    # -- search highlights ----------------------------------------------------------------
    def clear_search_hits(self) -> None:
        pages = set(self._search_rects)
        if self._current_hit is not None:
            pages.add(self._current_hit.page_index)
        self._search_rects = {}
        self._current_hit = None
        for page in pages:
            self._items[page].update()

    def add_search_hits(self, hits: list[SearchHit]) -> None:
        for hit in hits:
            self._search_rects.setdefault(hit.page_index, []).extend(q.rect for q in hit.quads)
            self._items[hit.page_index].update()

    def show_search_hit(self, hit: SearchHit) -> None:
        previous = self._current_hit
        self._current_hit = hit
        if previous is not None:
            self._items[previous.page_index].update()
        if self._mode is LayoutMode.SINGLE and hit.page_index != self._current:
            self.go_to_page(hit.page_index, record=False)
        self.ensureVisible(self.page_rect_to_scene(hit.page_index, hit.rect), 60, 120)
        self._set_current(hit.page_index)
        self._items[hit.page_index].update()

    # -- annotations ----------------------------------------------------------------------
    def page_annotations(self, index: int) -> list[AnnotationModel]:
        """Annotations on a page (cached per page revision)."""
        with self.session.lock:
            page = self.session.document.page(index)
            rev = page.revision
            cached = self._annots.get(index)
            if cached is None or cached[0] != rev:
                cached = (rev, page.annotations())
                self._annots[index] = cached
        return cached[1]

    def annotation_by_name(self, index: int, name: str) -> AnnotationModel | None:
        return next((a for a in self.page_annotations(index) if a.name == name), None)

    def annotation_at(self, scene_pos: QPointF) -> AnnotationModel | None:
        """Topmost visible comment under ``scene_pos`` (replies and popups are skipped)."""
        hit = self.page_point_at(scene_pos)
        if hit is None:
            return None
        index, point = hit
        tolerance = 3 / max(self.transform().m11(), 0.01)  # ~3 screen pixels
        for a in reversed(self.page_annotations(index)):
            if a.in_reply_to is not None or not a.type.is_comment or a.flags & 2:
                continue
            if annotation_bounds(a).inflated(tolerance).contains(point):
                return a
        return None

    def show_annotation(self, index: int, name: str) -> None:
        """Scroll to a comment and select it."""
        model = self.annotation_by_name(index, name)
        if model is None:
            return
        if self._mode is LayoutMode.SINGLE and index != self._current:
            self.go_to_page(index, record=False)
        self.ensureVisible(self.page_rect_to_scene(index, annotation_bounds(model)), 60, 120)
        self._set_current(index)
        self.set_annotation_selection([(index, name)])

    def selected_models(self) -> list[AnnotationModel]:
        out = []
        for page, name in self.selected_annotations:
            model = self.annotation_by_name(page, name)
            if model is not None:
                out.append(model)
        return out

    def set_annotation_selection(self, items: list[tuple[int, str]]) -> None:
        if items == self.selected_annotations:
            return
        pages = {p for p, _ in self.selected_annotations} | {p for p, _ in items}
        self.selected_annotations = list(items)
        for page in pages:
            if 0 <= page < len(self._items):
                self._items[page].update()
        self.annotation_selection_changed.emit()

    def set_annotation_preview(self, preview: dict[int, list[Rect]]) -> None:
        pages = set(self.annotation_preview) | set(preview)
        self.annotation_preview = preview
        for page in pages:
            if 0 <= page < len(self._items):
                self._items[page].update()

    def selection_frames(self, index: int) -> list[Rect]:
        """Bounds of selected annotations and content objects on a page (PageItem draws frames
        and handles)."""
        frames = [annotation_bounds(a) for a in self.selected_models() if a.page_index == index]
        keys = {key for page, key in self.selected_objects if page == index}
        if keys:
            frames.extend(o.bbox for o in self.page_objects(index) if o.key in keys)
        return frames

    # -- content objects (Edit mode) ------------------------------------------------------
    def page_objects(self, index: int) -> list[PageObject]:
        """Editable content on a page (cached per page revision)."""
        if not self.session.engine.capabilities.content_edit:
            return []
        with self.session.lock:
            page = self.session.document.page(index)
            rev = page.revision
            cached = self._objects.get(index)
            if cached is None or cached[0] != rev:
                try:
                    cached = (rev, page.content_objects())
                except Exception:  # malformed content: nothing editable on this page
                    log.exception("can't list content objects on page %d", index)
                    cached = (rev, [])
                self._objects[index] = cached
        return cached[1]

    def object_at(self, scene_pos: QPointF) -> PageObject | None:
        """The smallest object under ``scene_pos`` (so an image wins over a big text block)."""
        hit = self.page_point_at(scene_pos)
        if hit is None:
            return None
        index, point = hit
        tol = 2 / max(self.transform().m11(), 0.01)
        under = [o for o in self.page_objects(index) if o.bbox.inflated(tol).contains(point)]
        return min(under, key=lambda o: o.bbox.width * o.bbox.height, default=None)

    def selected_page_objects(self) -> list[PageObject]:
        out = []
        for page, key in self.selected_objects:
            obj = next((o for o in self.page_objects(page) if o.key == key), None)
            if obj is not None:
                out.append(obj)
        return out

    def selected_object_pages(self) -> dict[int, list[PageObject]]:
        by_page: dict[int, list[PageObject]] = {}
        for page, key in self.selected_objects:
            obj = next((o for o in self.page_objects(page) if o.key == key), None)
            if obj is not None:
                by_page.setdefault(page, []).append(obj)
        return by_page

    def set_object_selection(self, items: list[tuple[int, str]]) -> None:
        if items == self.selected_objects:
            return
        pages = {p for p, _ in self.selected_objects} | {p for p, _ in items}
        self.selected_objects = list(items)
        for page in pages:
            if 0 <= page < len(self._items):
                self._items[page].update()
        self.object_selection_changed.emit()

    def set_object_outlines(self, on: bool) -> None:
        self.show_object_outlines = on
        self.viewport().update()

    def object_outlines(self, index: int) -> list[Rect]:
        if not self.show_object_outlines:
            return []
        return [o.bbox for o in self.page_objects(index)]

    def page_rect(self, index: int) -> Rect:
        """Visible page rectangle (page space)."""
        return self._page_rects[index]

    def scene_to_page(self, index: int, scene_pos: QPointF) -> Point:
        """Scene position in ``index``'s page space, even when outside that page."""
        scene = self._scene_rects[index]
        local = Point(scene_pos.x() - scene.x0, scene_pos.y() - scene.y0)
        return local.transform(view_matrix(self._page_rects[index], self.rotation, 1.0).inverted())

    def page_point_to_scene(self, index: int, point: Point) -> QPointF:
        m = view_matrix(self._page_rects[index], self.rotation, 1.0)
        local = point.transform(m)
        scene = self._scene_rects[index]
        return QPointF(scene.x0 + local.x, scene.y0 + local.y)

    def overlays(self, index: int) -> list[tuple[Rect, QColor]]:
        """Highlight rectangles (page space) that PageItem draws over the page."""
        out = [(r, SEARCH_HIT_COLOR) for r in self._search_rects.get(index, ())]
        if self._current_hit is not None and self._current_hit.page_index == index:
            out.extend((q.rect, CURRENT_HIT_COLOR) for q in self._current_hit.quads)
        out.extend((r, SELECTION_COLOR) for r in self._selection_rects_for(index))
        out.extend(self.extra_overlays.get(index, ()))
        return out

    def set_extra_overlays(self, overlays: dict[int, list[tuple[Rect, QColor]]]) -> None:
        self.extra_overlays = overlays
        for item in self._items:
            item.update()

    # -- rendering ------------------------------------------------------------------------
    def _on_tile_ready(self, key: TileKey) -> None:
        if key.doc_id == self.session.id and 0 <= key.page_index < len(self._items):
            self._items[key.page_index].update()

    def refresh(self) -> None:
        """Repaint everything, e.g. after layer visibility or page content changed."""
        self._links.clear()
        for item in self._items:
            item.update()
        self.content_changed.emit()


def annotation_bounds(a: AnnotationModel) -> Rect:
    """Tight bounds of an annotation's geometry (its /Rect can include line-ending margins)."""
    points: list[Point] = []
    if a.type.is_markup:
        points = [p for q in a.quads for p in (q.ul, q.ur, q.ll, q.lr)]
    elif a.type is AnnotationType.INK:
        points = [p for stroke in a.ink for p in stroke]
    elif a.type in (AnnotationType.LINE, AnnotationType.POLYGON, AnnotationType.POLYLINE):
        points = list(a.vertices)
    if points:
        return Rect.from_points(points)
    return a.rect.normalized()


def annotation_tip_text(annot: AnnotationModel, page_annots: list[AnnotationModel]) -> str:
    """Tooltip text for a comment: author, text and replies (HTML-escaped plain text)."""
    import html

    lines: list[str] = []
    if annot.contents:
        who = f"<b>{html.escape(annot.author)}</b>: " if annot.author else ""
        lines.append(who + html.escape(annot.contents).replace("\n", "<br>"))
    for reply in page_annots:
        if reply.in_reply_to == annot.id and reply.contents:
            who = html.escape(reply.author or "Reply")
            lines.append(f"&nbsp;&nbsp;↳ <b>{who}</b>: {html.escape(reply.contents)}")
    return "<br>".join(lines)
