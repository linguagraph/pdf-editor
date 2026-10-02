"""Page thumbnails drawn as cards: the page with a drop shadow and its number below.

The current page gets a thick accent outline and its number on an accent chip; other selected
pages get a thinner, softer ring (``Colors.ring_selected``). Both reach 3:1 against the surface
and the canvas in either theme, and the two differ in width, color and label, so "current" and
"selected" stay apart for people who can't tell the colors apart. This replaces the item view's
selection fill (which washed out the page image).
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors
from pdfeditor.ui.view.page_item import shadow_strips

PAD = 8  # around the card: room for the outline and shadow
TEXT_GAP = 4  # between the card and the page number
RING = 3  # outline width of the current page, drawn just outside the page
RING_SELECTED = 2  # other selected pages
SHADOW_LAYERS = ((1, 1, 34), (2, 2, 18), (3, 3, 8))  # (spread, drop, alpha) in px

ModelIndex = QModelIndex | QPersistentModelIndex

# Optional model role: the page image's size (QSize) without building the image. Lists ask every
# item for its size when they lay out, which would otherwise make a pixmap per page.
PAGE_SIZE_ROLE = Qt.ItemDataRole.UserRole + 41


class PageCardDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

    @staticmethod
    def _logical(pixmap: QPixmap) -> QSize:
        size = pixmap.deviceIndependentSize()
        return QSize(round(size.width()), round(size.height()))

    def sizeHint(self, option: QStyleOptionViewItem, index: ModelIndex) -> QSize:
        size = index.data(PAGE_SIZE_ROLE)
        if isinstance(size, QSize) and size.isValid():
            page = size
            if page.width() > option.decorationSize.width() or (
                page.height() > option.decorationSize.height()
            ):
                page = page.scaled(option.decorationSize, Qt.AspectRatioMode.KeepAspectRatio)
        else:
            # initStyleOption shrinks decorationSize to the icon's actual size
            self.initStyleOption(option, index)
            page = option.decorationSize
        return QSize(
            page.width() + 2 * PAD,
            page.height() + 2 * PAD + TEXT_GAP + option.fontMetrics.height(),
        )

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: ModelIndex) -> None:
        self.initStyleOption(option, index)
        colors = current_colors()
        pixmap = option.icon.pixmap(option.decorationSize)
        page = self._logical(pixmap) if not pixmap.isNull() else option.decorationSize
        rect = option.rect
        text_h = option.fontMetrics.height()
        # Center the card in the cell (grid cells in the organizer are larger than the card).
        block_h = page.height() + TEXT_GAP + text_h
        top = rect.top() + max(PAD, (rect.height() - block_h) // 2)
        card = QRect(rect.left() + (rect.width() - page.width()) // 2, top, page.width(), 0)
        card.setHeight(page.height())

        view = option.widget
        current = isinstance(view, QAbstractItemView) and view.currentIndex() == index
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        shadow = QColor(colors.shadow)
        for spread, drop, alpha in SHADOW_LAYERS:  # thin fills around the card, as on the canvas
            shadow.setAlpha(alpha)
            for strip in shadow_strips(card, spread, drop):
                painter.fillRect(strip, shadow)
        if not pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(card, pixmap)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(colors.page_outline))
        painter.drawRect(card.adjusted(-1, -1, 0, 0))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        lead = selected and (current or not isinstance(view, QAbstractItemView))
        ring_color: QColor | None = None
        width = 1
        if lead:
            ring_color, width = QColor(colors.accent_text), RING
        elif selected:
            ring_color, width = QColor(colors.ring_selected), RING_SELECTED
        elif hovered:
            ring_color = QColor(colors.border_strong)
        if ring_color is not None:
            pen = QPen(ring_color)
            pen.setWidth(width)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setPen(pen)
            grow = width / 2 + 1.5
            radius = METRICS.radius_small / 2
            painter.drawRoundedRect(QRectF(card).adjusted(-grow, -grow, grow, grow), radius, radius)

        label = option.text  # initStyleOption read the display role
        if label:
            text_rect = QRect(rect.left(), card.bottom() + 1 + TEXT_GAP, rect.width(), text_h)
            elided = option.fontMetrics.elidedText(
                str(label), Qt.TextElideMode.ElideMiddle, rect.width() - 4
            )
            if lead:  # the number on an accent chip
                font = QFont(option.font)
                font.setBold(True)
                painter.setFont(font)
                chip_w = painter.fontMetrics().horizontalAdvance(elided) + 2 * METRICS.space(2)
                chip = QRectF(
                    text_rect.center().x() - chip_w / 2 + 0.5, text_rect.top(), chip_w, text_h
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors.accent))
                painter.drawRoundedRect(chip, text_h / 2, text_h / 2)
                painter.setPen(QColor(colors.on_accent))
            else:
                painter.setFont(option.font)
                painter.setPen(QColor(colors.text if selected else colors.text_muted))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter, elided)
        if option.state & QStyle.StateFlag.State_HasFocus and not selected:
            pen = QPen(QColor(colors.accent_text))
            pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.drawRect(card.adjusted(-3, -3, 2, 2))
        painter.restore()
