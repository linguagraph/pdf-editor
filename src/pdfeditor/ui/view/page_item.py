"""QGraphicsItem that draws one page from cached tiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from pdfeditor.core.layout import rotated_size, view_matrix
from pdfeditor.model.geometry import Rect
from pdfeditor.ui.view import tiles

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView


def qrect(r: Rect) -> QRectF:
    return QRectF(r.x0, r.y0, r.width, r.height)


class PageItem(QGraphicsItem):
    """Item coordinates are the page's visible space rotated by the view rotation, in points."""

    def __init__(self, view: DocumentView, index: int, page_rect: Rect) -> None:
        super().__init__()
        self._view = view
        self.index = index
        self.page_rect = page_rect
        self._w, self._h = page_rect.width, page_rect.height
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

    def set_rotation(self, rotation: int) -> None:
        self.prepareGeometryChange()
        self._w, self._h = rotated_size(self.page_rect.width, self.page_rect.height, rotation)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    def paint(
        self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None
    ) -> None:
        view = self._view
        bounds = self.boundingRect()
        painter.fillRect(bounds, QColor(0, 0, 0) if view.night_mode else QColor(255, 255, 255))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        variant = view.render_variant

        thumb = tiles.thumbnail(
            view.renderer, view.session, self.index, self.page_rect, view.rotation, variant
        )
        if thumb is not None:
            painter.drawImage(bounds, thumb)

        lod = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
        dpr = widget.devicePixelRatioF() if widget is not None else 1.0
        scale = max(lod * dpr, 0.01)
        exposed_q = option.exposedRect.intersected(bounds)
        if not exposed_q.isEmpty() and (thumb is None or scale > thumb.width() / self._w * 1.05):
            exposed = Rect(exposed_q.left(), exposed_q.top(), exposed_q.right(), exposed_q.bottom())
            specs = tiles.tile_specs(
                view.session, self.index, self.page_rect, view.rotation, scale, exposed, variant
            )
            for spec in specs:
                image = tiles.request_tile(view.renderer, spec)
                if image is not None:
                    target = spec.target
                    s = spec.key.scale
                    painter.drawImage(
                        QRectF(target.x0, target.y0, image.width() / s, image.height() / s), image
                    )

        overlays = view.overlays(self.index)
        if overlays:
            m = view_matrix(self.page_rect, view.rotation, 1.0)
            painter.save()
            # Multiply keeps the glyphs under a highlight dark and readable.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            for rect, color in overlays:
                painter.fillRect(qrect(rect.transform(m).inflated(0.5)), color)
            painter.restore()

        pen = QPen(QColor(0, 0, 0, 70))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(bounds)
