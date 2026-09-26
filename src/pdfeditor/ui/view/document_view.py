"""The scrollable, zoomable page view for one document."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QMouseEvent,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import QFrame, QGraphicsScene, QGraphicsView, QToolTip, QWidget

from pdfeditor.core.layout import (
    PAGE_GAP,
    LayoutMode,
    compute_layout,
    layout_bounds,
    page_at,
    view_matrix,
)
from pdfeditor.core.render_cache import THUMBNAIL_TILE, TileKey
from pdfeditor.core.session import DocumentSession
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.outline import Link, LinkKind
from pdfeditor.ui.view.page_item import PageItem, qrect
from pdfeditor.ui.view.renderer import TileRenderer

log = logging.getLogger(__name__)

MIN_ZOOM, MAX_ZOOM = 0.05, 64.0
ZOOM_STEPS = (
    0.1, 0.25, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 32.0, 64.0
)  # fmt: skip


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
        self.setBackgroundBrush(QColor(0x5A, 0x5D, 0x63))
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
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
        self._items: list[PageItem] = []
        self._links: dict[int, tuple[int, list[Link]]] = {}
        self._labels: list[str] | None = None
        self._history: list[ViewLocation] = []
        self._history_pos = -1
        self._hover_link: Link | None = None
        self._scrolling_programmatically = False

        renderer.tile_ready.connect(self._on_tile_ready)
        self.verticalScrollBar().valueChanged.connect(self._update_current_page)
        self.reload()

    # -- document -------------------------------------------------------------------------
    def reload(self) -> None:
        """Re-read page geometry (after open, page edits or save) and rebuild the layout."""
        with self.session.lock:
            doc = self.session.document
            self._page_rects = [doc.page(i).rect for i in range(doc.page_count)]
        self._links.clear()
        self._labels = None
        for item in self._items:
            self._scene.removeItem(item)
        self._items = [PageItem(self, i, r) for i, r in enumerate(self._page_rects)]
        for item in self._items:
            self._scene.addItem(item)
        self._current = min(self._current, max(0, len(self._items) - 1))
        self._relayout()

    def close_view(self) -> None:
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

    def set_night_mode(self, on: bool) -> None:
        self.night_mode = on
        self.renderer.cancel(lambda k: k.doc_id == self.session.id)
        self.viewport().update()

    def _relayout(self) -> None:
        sizes = [(r.width, r.height) for r in self._page_rects]
        self._scene_rects = compute_layout(sizes, self._mode, self.rotation)
        for item, rect in zip(self._items, self._scene_rects, strict=True):
            item.setPos(rect.x0, rect.y0)
        self._apply_single_page_visibility()
        if self._fit is not FitMode.NONE:
            self._apply_fit()
        self.layout_changed.emit()

    def _apply_single_page_visibility(self) -> None:
        if not self._scene_rects:
            self._scene.setSceneRect(0, 0, 1, 1)
            return
        if self._mode is LayoutMode.SINGLE:
            for i, item in enumerate(self._items):
                item.setVisible(i == self._current)
            self._scene.setSceneRect(qrect(self._scene_rects[self._current].inflated(PAGE_GAP)))
        else:
            for item in self._items:
                item.setVisible(True)
            self._scene.setSceneRect(qrect(layout_bounds(self._scene_rects)))

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

    def zoom_in(self) -> None:
        self.set_zoom(next((z for z in ZOOM_STEPS if z > self._zoom + 1e-6), MAX_ZOOM))

    def zoom_out(self) -> None:
        self.set_zoom(next((z for z in reversed(ZOOM_STEPS) if z < self._zoom - 1e-6), MIN_ZOOM))

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

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120
            if steps:
                self.set_zoom(self._zoom * (1.1**steps))
            event.accept()
            return
        super().wheelEvent(event)

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

    def go_to_page(self, index: int, point: Point | None = None, record: bool = True) -> None:
        """Scroll so page ``index`` (optionally the page-space ``point``) is at the top."""
        if not 0 <= index < self.page_count:
            return
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
        # In two-up rows the scroll position alone can't tell which page was requested.
        self._scrolling_programmatically = True
        try:
            self.verticalScrollBar().setValue(round((target_y - scene_top) * k))
            if target_x is not None and self.horizontalScrollBar().maximum() > 0:
                left = self.sceneRect().left()
                self.horizontalScrollBar().setValue(round((target_x - left) * k))
        finally:
            self._scrolling_programmatically = False
        if index != previous:
            self.current_page_changed.emit(index)
        if record:
            self._push_history()

    def next_page(self) -> None:
        step = 2 if self._mode in (LayoutMode.TWO_UP, LayoutMode.TWO_UP_COVER) else 1
        self.go_to_page(min(self.page_count - 1, self._current + step), record=False)

    def previous_page(self) -> None:
        step = 2 if self._mode in (LayoutMode.TWO_UP, LayoutMode.TWO_UP_COVER) else 1
        self.go_to_page(max(0, self._current - step), record=False)

    def first_page(self) -> None:
        self.go_to_page(0)

    def last_page(self) -> None:
        self.go_to_page(self.page_count - 1)

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

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
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
            self.go_to_page(link.dest.page_index, link.dest.point)
        else:
            self.link_activated.emit(link)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)
        if event.buttons():
            return
        link = self.link_at(self.mapToScene(event.position().toPoint()))
        if link is not self._hover_link:
            self._hover_link = link
            if link is None:
                self.viewport().unsetCursor()
                QToolTip.hideText()
            else:
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                tip = (
                    link.uri
                    or link.file
                    or (f"Go to page {self.page_label(link.dest.page_index)}" if link.dest else "")
                )
                if tip:
                    QToolTip.showText(event.globalPosition().toPoint(), tip, self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            link = self.link_at(self.mapToScene(event.position().toPoint()))
            if link is not None:
                self.activate_link(link)
                event.accept()
                return
        super().mousePressEvent(event)

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
