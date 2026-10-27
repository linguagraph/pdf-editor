"""Organize Pages: a grid of large thumbnails with drag-and-drop reordering and file drops."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QAbstractItemView, QListView, QMessageBox, QVBoxLayout, QWidget

from pdfeditor.core.layout import rotated_size
from pdfeditor.core.render_cache import THUMBNAIL_TILE, TileKey
from pdfeditor.engine.base import EngineError
from pdfeditor.services.assembly import IMAGE_SUFFIXES
from pdfeditor.ui import page_ops
from pdfeditor.ui.icons import page_icon
from pdfeditor.ui.theme import current_colors
from pdfeditor.ui.view import tiles
from pdfeditor.ui.view.document_view import DocumentView
from pdfeditor.ui.view.page_card import PAD, PageCardDelegate

MIME_PAGES = "application/x-pdfeditor-pages"
THUMB_PX = 200
ModelIndex = QModelIndex | QPersistentModelIndex


class OrganizerModel(QAbstractListModel):
    def __init__(self, view: DocumentView) -> None:
        super().__init__()
        self.view = view
        view.renderer.tile_ready.connect(self._on_tile_ready)

    def detach(self) -> None:
        self.view.renderer.tile_ready.disconnect(self._on_tile_ready)

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self.view.page_count

    def flags(self, index: ModelIndex) -> Qt.ItemFlag:
        # drops are handled by OrganizerGrid (between pages), not per item
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        )

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.view.page_label(row)
        if role == Qt.ItemDataRole.DecorationRole:
            return page_icon(self._pixmap(row))
        return None

    def _pixmap(self, row: int) -> QPixmap:
        v = self.view
        rect = v.page_rect(row)
        image = tiles.thumbnail(
            v.renderer, v.session, row, rect, v.rotation, v.render_variant, THUMB_PX
        )
        if image is not None:
            return QPixmap.fromImage(image)
        w, h = rotated_size(rect.width, rect.height, v.rotation)
        placeholder = QPixmap(THUMB_PX, max(1, round(THUMB_PX * h / w)))
        placeholder.fill(QColor(235, 235, 235))
        return placeholder

    def _on_tile_ready(self, key: TileKey) -> None:
        if key.doc_id == self.view.session.id and key.tile_x == THUMBNAIL_TILE:
            idx = self.index(key.page_index)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def reset(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    # -- drag and drop --------------------------------------------------------------------
    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def mimeTypes(self) -> list[str]:
        return [MIME_PAGES, "text/uri-list"]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:  # type: ignore[override]
        mime = QMimeData()
        rows = sorted({i.row() for i in indexes})
        payload = f"{self.view.session.id}:" + ",".join(map(str, rows))
        mime.setData(MIME_PAGES, QByteArray(payload.encode()))
        return mime


def _own_rows(view: DocumentView, data: QMimeData) -> list[int] | None:
    """Rows of our own document carried by ``data``, or None if it isn't ours."""
    if not data.hasFormat(MIME_PAGES):
        return None
    doc_id, _, rows = bytes(data.data(MIME_PAGES).data()).decode().partition(":")
    try:
        if int(doc_id) != view.session.id or not rows:
            return None
        return [int(r) for r in rows.split(",")]
    except ValueError:
        return None


def _droppable_files(data: QMimeData) -> list[Path]:
    paths = []
    for url in data.urls() if data.hasUrls() else []:
        path = Path(url.toLocalFile())
        if url.isLocalFile() and (
            path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_SUFFIXES
        ):
            paths.append(path)
    return paths


def acceptable(view: DocumentView, data: QMimeData) -> bool:
    """Whether a drag carries something the organizer can place: our pages or local files."""
    if data.hasFormat(MIME_PAGES):
        return _own_rows(view, data) is not None
    return bool(_droppable_files(data))


def drop_pages(view: DocumentView, data: QMimeData, before: int) -> list[int] | None:
    """Handle a drop at page index ``before``: reorder our own pages or insert dropped files.

    Returns the dropped pages' new indices, or None when the drop was refused."""
    session = view.session
    if data.hasFormat(MIME_PAGES):
        rows = _own_rows(view, data)
        return None if rows is None else page_ops.move_pages(session, rows, before)
    at = before
    for path in _droppable_files(data):
        at += page_ops.insert_file(session, path, at)
    return list(range(before, at)) if at != before else None


def drop(view: DocumentView, data: QMimeData, before: int) -> bool:
    return drop_pages(view, data, before) is not None


def insertion_slot(
    count: int,
    rect_of: Callable[[int], QRect],
    pos: QPoint,
    viewport_width: int,
    fallback_gap: int,
) -> tuple[int, QRect | None]:
    """The gap (page index to insert before) nearest ``pos`` in a wrapping grid of ``count``
    cells, and the rectangle of the line that marks it."""
    if count == 0:
        return 0, None
    top0 = rect_of(0).top()
    per_line = 1
    while per_line < count and rect_of(per_line).top() == top0:
        per_line += 1
    pitch = rect_of(per_line).top() - top0 if count > per_line else rect_of(0).height()
    pitch = max(1, pitch)
    last_line = (count - 1) // per_line
    line = min(max((pos.y() - top0) // pitch, 0), last_line)
    start = line * per_line
    end = min(start + per_line, count)
    before = end
    for i in range(start, end):
        if pos.x() < rect_of(i).center().x():
            before = i
            break
    gap = max(rect_of(1).left() - rect_of(0).right(), 0) if per_line >= 2 else fallback_gap
    if start < before < end:
        x = (rect_of(before - 1).right() + rect_of(before).left()) // 2
    elif before == start:
        x = max(rect_of(start).left() - gap // 2, 2)
    else:
        x = min(rect_of(end - 1).right() + gap // 2, viewport_width - 3)
    first = rect_of(start)
    return before, QRect(x - 1, first.top() + PAD // 2, 3, first.height() - PAD)


class OrganizerGrid(QListView):
    """The thumbnail grid. Owns the whole drag-and-drop cycle so pages can be dropped in the gaps
    between pages (Qt's icon-mode drop handling only knows "on an item" or free placement), with
    a line showing where they will land."""

    def __init__(
        self,
        accepts: Callable[[QMimeData], bool],
        on_drop: Callable[[QMimeData, int], list[int] | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._accepts = accepts
        self._on_drop = on_drop
        self.drop_slot: int | None = None
        self.drop_indicator: QRect | None = None
        self._drag_mime: QMimeData | None = None
        self.verticalScrollBar().valueChanged.connect(self._rescan)

    # -- drag source ----------------------------------------------------------------------
    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        model = self.model()
        indexes = sorted(self.selectedIndexes(), key=lambda i: i.row())
        if model is None or not indexes:
            return
        drag = QDrag(self)
        drag.setMimeData(model.mimeData(indexes))
        pixmap = self._drag_pixmap(indexes)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        try:
            drag.exec(supportedActions, Qt.DropAction.MoveAction)
        finally:
            self._clear_indicator()

    def _drag_pixmap(self, indexes: list[QModelIndex]) -> QPixmap:
        icon = indexes[0].data(Qt.ItemDataRole.DecorationRole)
        src = icon.pixmap(QSize(THUMB_PX, THUMB_PX * 2)) if hasattr(icon, "pixmap") else QPixmap()
        if src.isNull():
            src = QPixmap(96, 136)
            src.fill(QColor(235, 235, 235))
        src = src.scaledToWidth(96, Qt.TransformationMode.SmoothTransformation)
        out = QPixmap(src.size())
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setOpacity(0.8)
        p.drawPixmap(0, 0, src)
        p.setOpacity(1.0)
        if len(indexes) > 1:
            colors = current_colors()
            badge = QRect(out.width() - 24, 0, 24, 24)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(colors.accent))
            p.drawEllipse(badge)
            p.setPen(QColor(colors.on_accent))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(len(indexes)))
        p.end()
        return out

    # -- drop target ----------------------------------------------------------------------
    def _slot_at(self, pos: QPoint) -> tuple[int, QRect | None]:
        model = self.model()
        return insertion_slot(
            model.rowCount(),
            lambda r: self.visualRect(model.index(r, 0)),
            pos,
            self.viewport().width(),
            self.spacing(),
        )

    def _set_indicator(self, slot: int | None, rect: QRect | None) -> None:
        old = self.drop_indicator
        self.drop_slot, self.drop_indicator = slot, rect
        for r in (old, rect):
            if r is not None:
                self.viewport().update(r.adjusted(-6, -6, 6, 6))

    def _clear_indicator(self) -> None:
        self._drag_mime = None
        self._set_indicator(None, None)

    def _track(self, event: QDragEnterEvent | QDragMoveEvent) -> None:
        mime = event.mimeData()
        if not self._accepts(mime):
            self._clear_indicator()
            event.ignore()
            return
        self._drag_mime = mime
        pos = event.position().toPoint()
        self._set_indicator(*self._slot_at(pos))
        event.setDropAction(
            Qt.DropAction.MoveAction if mime.hasFormat(MIME_PAGES) else Qt.DropAction.CopyAction
        )
        event.accept()
        margin = self.autoScrollMargin()
        if pos.y() < margin or pos.y() > self.viewport().height() - margin:
            self.startAutoScroll()
        else:
            self.stopAutoScroll()

    def _rescan(self) -> None:
        """The view scrolled under a still mouse (autoscroll): the gap under it changed."""
        if self._drag_mime is not None:
            self._set_indicator(*self._slot_at(self.viewport().mapFromGlobal(QCursor.pos())))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self.setState(QAbstractItemView.State.DraggingState)
        self._track(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._track(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._clear_indicator()
        self.stopAutoScroll()
        self.setState(QAbstractItemView.State.NoState)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        before, _ = self._slot_at(event.position().toPoint())
        self._clear_indicator()
        self.stopAutoScroll()
        self.setState(QAbstractItemView.State.NoState)
        if not self._accepts(mime):
            event.ignore()
            return
        self._on_drop(mime, before)
        event.setDropAction(
            Qt.DropAction.MoveAction if mime.hasFormat(MIME_PAGES) else Qt.DropAction.CopyAction
        )
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        rect = self.drop_indicator
        if rect is None:
            return
        p = QPainter(self.viewport())
        pen = QPen(QColor(current_colors().accent_text), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        cx = rect.center().x()
        p.drawLine(cx, rect.top(), cx, rect.bottom())
        p.drawLine(cx - 4, rect.top(), cx + 4, rect.top())
        p.drawLine(cx - 4, rect.bottom(), cx + 4, rect.bottom())
        p.end()


class OrganizerWidget(QWidget):
    """Shown in place of the page view while organizing pages."""

    page_activated = Signal(int)  # double-clicked: go back to reading at this page
    selection_changed = Signal()

    def __init__(self, view: DocumentView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self.grid = OrganizerGrid(lambda m: acceptable(view, m), self._on_drop, self)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(THUMB_PX, int(THUMB_PX * 1.42)))
        self.grid.setGridSize(QSize(THUMB_PX + 40, int(THUMB_PX * 1.42) + 44))
        self.grid.setSpacing(8)
        self.grid.setUniformItemSizes(True)
        self.grid.setItemDelegate(PageCardDelegate(self.grid))
        self.grid.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # setMovement resets the drag-and-drop flags, so these come after it
        self.grid.setDragEnabled(True)
        self.grid.setAcceptDrops(True)
        self.grid.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.grid.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.grid.setDropIndicatorShown(False)  # OrganizerGrid draws its own insertion line
        self.grid.viewport().setAcceptDrops(True)  # the viewport is what receives drag events
        self.grid.doubleClicked.connect(lambda idx: self.page_activated.emit(idx.row()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.grid)
        self.model = OrganizerModel(view)
        self.grid.setModel(self.model)
        self.grid.selectionModel().selectionChanged.connect(
            lambda *_: self.selection_changed.emit()
        )
        view.layout_changed.connect(self._refresh)
        view.content_changed.connect(self._refresh)

    def _on_drop(self, data: QMimeData, before: int) -> list[int] | None:
        try:
            new = drop_pages(self.view, data, before)
        except (page_ops.PageOpError, EngineError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Organize Pages", str(exc))
            return None
        if new is not None:
            self.select_pages(new)
        return new

    def _refresh(self) -> None:
        keep = self.selected_pages()
        self.model.reset()
        self.select_pages([p for p in keep if p < self.view.page_count])

    def close_organizer(self) -> None:
        self.view.layout_changed.disconnect(self._refresh)
        self.view.content_changed.disconnect(self._refresh)
        self.model.detach()

    def selected_pages(self) -> list[int]:
        return sorted(i.row() for i in self.grid.selectionModel().selectedIndexes())

    def select_pages(self, pages: list[int]) -> None:
        sel = self.grid.selectionModel()
        sel.clearSelection()
        for p in pages:
            sel.select(self.model.index(p), sel.SelectionFlag.Select)
        if pages:
            # NoUpdate: plain setCurrentIndex would replace the multi-page selection
            sel.setCurrentIndex(self.model.index(pages[0]), sel.SelectionFlag.NoUpdate)
            self.grid.scrollTo(self.model.index(pages[0]))
