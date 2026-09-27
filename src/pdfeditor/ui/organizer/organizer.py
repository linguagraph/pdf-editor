"""Organize Pages: a grid of large thumbnails with drag-and-drop reordering and file drops."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QVBoxLayout, QWidget

from pdfeditor.core.layout import rotated_size
from pdfeditor.core.render_cache import THUMBNAIL_TILE, TileKey
from pdfeditor.services.assembly import IMAGE_SUFFIXES
from pdfeditor.ui import page_ops
from pdfeditor.ui.view import tiles
from pdfeditor.ui.view.document_view import DocumentView

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
        base = Qt.ItemFlag.ItemIsDropEnabled
        if not index.isValid():
            return base
        return (
            base
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.view.page_label(row)
        if role == Qt.ItemDataRole.DecorationRole:
            return self._pixmap(row)
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

    def dropMimeData(
        self, data: QMimeData, action: Qt.DropAction, row: int, column: int, parent: ModelIndex
    ) -> bool:
        before = row if row >= 0 else (parent.row() if parent.isValid() else self.rowCount())
        return drop(self.view, data, before)


def drop(view: DocumentView, data: QMimeData, before: int) -> bool:
    """Handle a drop at page index ``before``: reorder our own pages or insert dropped files."""
    session = view.session
    if data.hasFormat(MIME_PAGES):
        doc_id, _, rows = bytes(data.data(MIME_PAGES).data()).decode().partition(":")
        if int(doc_id) != session.id or not rows:
            return False
        page_ops.move_pages(session, [int(r) for r in rows.split(",")], before)
        return True
    if data.hasUrls():
        at = before
        for url in data.urls():
            path = Path(url.toLocalFile())
            if url.isLocalFile() and (
                path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_SUFFIXES
            ):
                at += page_ops.insert_file(session, path, at)
        return at != before
    return False


class OrganizerWidget(QWidget):
    """Shown in place of the page view while organizing pages."""

    page_activated = Signal(int)  # double-clicked: go back to reading at this page
    selection_changed = Signal()

    def __init__(self, view: DocumentView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self.grid = QListView(self)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Snap)
        self.grid.setIconSize(QSize(THUMB_PX, int(THUMB_PX * 1.42)))
        self.grid.setGridSize(QSize(THUMB_PX + 40, int(THUMB_PX * 1.42) + 44))
        self.grid.setSpacing(8)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.grid.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.grid.setDragEnabled(True)
        self.grid.setAcceptDrops(True)
        self.grid.setDropIndicatorShown(True)
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
