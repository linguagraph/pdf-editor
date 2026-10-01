"""Page thumbnails: a virtualized list that renders images lazily."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QVBoxLayout, QWidget

from pdfeditor.core.layout import rotated_size
from pdfeditor.core.render_cache import THUMBNAIL_TILE, TileKey
from pdfeditor.ui.icons import page_icon
from pdfeditor.ui.panels.base import ViewPanel
from pdfeditor.ui.view import tiles
from pdfeditor.ui.view.document_view import DocumentView
from pdfeditor.ui.view.page_card import PAGE_SIZE_ROLE, PageCardDelegate

ModelIndex = QModelIndex | QPersistentModelIndex


class ThumbnailModel(QAbstractListModel):
    def __init__(self, view: DocumentView) -> None:
        super().__init__()
        self.view = view
        view.renderer.tile_ready.connect(self._on_tile_ready)

    def detach(self) -> None:
        self.view.renderer.tile_ready.disconnect(self._on_tile_ready)

    def rowCount(self, parent: ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self.view.page_count

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.view.page_label(row)
        if role == Qt.ItemDataRole.DecorationRole:
            return page_icon(self._pixmap(row))
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Page {row + 1} of {self.view.page_count}"
        if role == PAGE_SIZE_ROLE:
            page_rect = self.view.page_rect(row)
            w, h = rotated_size(page_rect.width, page_rect.height, self.view.rotation)
            width = tiles.THUMBNAIL_WIDTH_PX
            return QSize(width, max(1, round(width * h / max(w, 1.0))))
        return None

    def _pixmap(self, row: int) -> QPixmap:
        view = self.view
        page_rect = view._page_rects[row]
        image: QImage | None = tiles.thumbnail(
            view.renderer, view.session, row, page_rect, view.rotation, view.render_variant
        )
        if image is not None:
            return QPixmap.fromImage(image)
        w, h = rotated_size(page_rect.width, page_rect.height, view.rotation)
        placeholder = QPixmap(
            tiles.THUMBNAIL_WIDTH_PX, max(1, round(tiles.THUMBNAIL_WIDTH_PX * h / w))
        )
        placeholder.fill(QColor(235, 235, 235))
        return placeholder

    def _on_tile_ready(self, key: TileKey) -> None:
        if key.doc_id == self.view.session.id and key.tile_x == THUMBNAIL_TILE:
            idx = self.index(key.page_index)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def reset(self) -> None:
        self.beginResetModel()
        self.endResetModel()


class ThumbnailsPanel(ViewPanel):
    title = "Pages"
    rebuild_on = frozenset()  # follows layout_changed/content_changed instead

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListView(self)
        self.list.setAccessibleName("Page thumbnails")
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setFlow(QListView.Flow.TopToBottom)
        self.list.setWrapping(False)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setUniformItemSizes(False)
        self.list.setSpacing(2)  # the cards carry their own padding
        self.list.setItemDelegate(PageCardDelegate(self.list))
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.list.setIconSize(QSize(tiles.THUMBNAIL_WIDTH_PX, int(tiles.THUMBNAIL_WIDTH_PX * 1.5)))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.clicked.connect(self._on_clicked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list)
        self.model: ThumbnailModel | None = None

    def bind(self, view: DocumentView) -> None:
        view.current_page_changed.connect(self._select)
        view.layout_changed.connect(self._on_layout_changed)
        view.content_changed.connect(self._on_layout_changed)

    def unbind(self, view: DocumentView) -> None:
        view.current_page_changed.disconnect(self._select)
        view.layout_changed.disconnect(self._on_layout_changed)
        view.content_changed.disconnect(self._on_layout_changed)

    def rebuild(self) -> None:
        if self.model is not None:
            self.model.detach()
        self.model = ThumbnailModel(self.view) if self.view is not None else None
        self.list.setModel(self.model)
        if self.view is not None:
            self._select(self.view.current_page)

    def _on_layout_changed(self) -> None:
        # rotation or page count may have changed
        if self.model is not None:
            self.model.reset()
            if self.view is not None:
                self._select(self.view.current_page)

    def _select(self, page: int) -> None:
        if self.model is None:
            return
        idx = self.model.index(page)
        self.list.setCurrentIndex(idx)
        self.list.scrollTo(idx)

    def _on_clicked(self, index: QModelIndex) -> None:
        if self.view is not None:
            self.view.go_to_page(index.row())
