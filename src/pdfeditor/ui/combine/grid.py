"""The grid of file cards: painting, drag-and-drop with an insertion marker, keyboard."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
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
    QContextMenuEvent,
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QListView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from pdfeditor.ui.combine.model import (
    DETAIL_ROLE,
    ENTRY_ROLE,
    CombineEntry,
    CombineModel,
    EntryState,
)
from pdfeditor.ui.icons import icon
from pdfeditor.ui.reveal import show_in_folder
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors

CARD = QSize(176, 240)  # logical pixels
THUMB = QSize(132, 168)  # the preview's box; rendered at the screen's device pixel ratio
MARGIN = QSize(16, 8)  # between cards: the cell is the card plus this
CELL = CARD + MARGIN
BUTTON = 22  # the remove button's diameter
BAR_WIDTH = 3  # the insertion marker
EDGE = 28  # dragging this close to the viewport's edge scrolls it


def card_rect(cell: QRect) -> QRect:
    """The card inside a grid cell."""
    dx, dy = MARGIN.width() // 2, MARGIN.height() // 2
    return QRect(cell).adjusted(dx, dy, -dx, -dy)


def remove_rect(card: QRect) -> QRect:
    return QRect(card.right() - BUTTON - 5, card.y() + 7, BUTTON, BUTTON)


def _tr(text: str) -> str:
    return QCoreApplication.translate("CombineGrid", text)


class CombineCardDelegate(QStyledItemDelegate):
    """A file as a card: position, first-page preview, name, pages, and a remove button."""

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return CELL

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        c = current_colors()
        state = option.state
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        widget = option.widget
        focused = bool(state & QStyle.StateFlag.State_HasFocus) and (
            widget is not None and widget.hasFocus()
        )
        entry = index.data(ENTRY_ROLE)
        card = card_rect(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        background = c.accent_subtle if selected else c.hover if hovered else c.surface
        outline = c.accent_text if focused else c.ring_selected if selected else c.border
        painter.setPen(QPen(QColor(outline), 2 if focused or selected else 1))
        painter.setBrush(QColor(background))
        radius = METRICS.radius_large
        painter.drawRoundedRect(card.adjusted(1, 1, -1, -1), radius, radius)

        slot = QRect(
            card.x() + (card.width() - THUMB.width()) // 2,
            card.y() + 14,
            THUMB.width(),
            THUMB.height(),
        )
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            size = pixmap.deviceIndependentSize().toSize()
            size.scale(slot.size(), Qt.AspectRatioMode.KeepAspectRatio)
            page = QRect(
                slot.x() + (slot.width() - size.width()) // 2,
                slot.y() + (slot.height() - size.height()) // 2,
                size.width(),
                size.height(),
            )
            painter.drawPixmap(page, pixmap)
            painter.setPen(QPen(QColor(c.border_strong), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(page.adjusted(0, 0, -1, -1))
        else:
            page = QRect(slot.x() + 14, slot.y() + 6, slot.width() - 28, slot.height() - 12)
            painter.setPen(QPen(QColor(c.border_strong), 1))
            painter.setBrush(QColor(c.surface_alt))
            painter.drawRect(page)
            name = "file"
            if isinstance(entry, CombineEntry):
                name = {
                    EntryState.LOCKED: "file-lock",
                    EntryState.ERROR: "file-x",
                }.get(entry.state, "file")
            icon(name).paint(painter, QRect(page.center() - QPoint(16, 16), QSize(32, 32)))

        # position badge, top left
        badge = QRect(card.x() + 7, card.y() + 7, BUTTON, BUTTON)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c.accent))
        painter.drawEllipse(badge)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        painter.setPen(QColor(c.on_accent))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(index.row() + 1))

        # name and detail
        text_rect = QRect(card.x() + 10, slot.bottom() + 12, card.width() - 20, 18)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(c.text))
        name_text = painter.fontMetrics().elidedText(
            str(index.data(Qt.ItemDataRole.DisplayRole)),
            Qt.TextElideMode.ElideMiddle,
            text_rect.width(),
        )
        center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        painter.drawText(text_rect, center, name_text)
        font.setWeight(QFont.Weight.Normal)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        bad = isinstance(entry, CombineEntry) and entry.state in (
            EntryState.ERROR,
            EntryState.LOCKED,
        )
        painter.setPen(QColor(c.danger if bad else c.text_muted))
        detail = painter.fontMetrics().elidedText(
            str(index.data(DETAIL_ROLE)), Qt.TextElideMode.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect.translated(0, 19), center, detail)

        if hovered or selected:
            button = remove_rect(card)
            painter.setPen(QPen(QColor(c.border_strong), 1))
            painter.setBrush(QColor(c.surface))
            painter.drawEllipse(button)
            icon("x").paint(painter, button.adjusted(5, 5, -5, -5))
        painter.restore()


class CombineGrid(QListView):
    """Cards in the order they will be combined. Drag cards to reorder them, or drop files
    from the file manager between them."""

    activated_row = Signal(int)  # double-click, Enter
    pages_requested = Signal(int)
    unlock_requested = Signal(int)
    files_dropped = Signal(list, int)  # list[Path], insertion row

    def __init__(self, model: CombineModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CombineGrid")
        self.setAccessibleName(self.tr("Files to combine"))
        self.setAccessibleDescription(
            self.tr(
                "Ctrl+arrow keys move the selected files, Delete removes them, "
                "Enter chooses pages or unlocks"
            )
        )
        self.setModel(model)
        self._model = model
        self._drop_at: int | None = None  # where the insertion marker is
        self._swallow_release = False
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)  # before drag-and-drop: it resets those
        self.setDragEnabled(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)  # the marker is painted by this class
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setGridSize(CELL)
        self.setSpacing(0)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setItemDelegate(CombineCardDelegate(self))

    # -- geometry -------------------------------------------------------------------------
    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.selectionModel().selectedIndexes()})

    def card_at(self, row: int) -> QRect:
        return card_rect(self.visualRect(self._model.index(row)))

    def insertion_index(self, pos: QPoint) -> int:
        """Where a drop at ``pos`` (viewport coordinates) goes: before the card whose left half
        is under it, after the last card of its row for the right half, at the end below the
        cards."""
        count = self._model.rowCount()
        in_band: list[int] = []
        for row in range(count):
            rect = self.visualRect(self._model.index(row))
            if rect.top() <= pos.y() <= rect.bottom():
                in_band.append(row)
        if not in_band:
            last = self.visualRect(self._model.index(count - 1)) if count else QRect()
            return count if not count or pos.y() > last.bottom() else 0
        for row in in_band:
            if pos.x() < self.visualRect(self._model.index(row)).center().x():
                return row
        return in_band[-1] + 1

    def drop_indicator(self) -> QRect | None:
        """The insertion marker (viewport coordinates), if a drag is over the grid."""
        at = self._drop_at
        count = self._model.rowCount()
        if at is None or count == 0:
            return None
        gap = MARGIN.width() // 2
        if at < count:
            card = self.card_at(at)
            x = card.left() - gap // 2 - BAR_WIDTH // 2 - 1
        else:
            card = self.card_at(count - 1)
            x = card.right() + gap // 2 - BAR_WIDTH // 2 + 1
        return QRect(x, card.top(), BAR_WIDTH, card.height())

    def _set_drop(self, at: int | None) -> None:
        if at != self._drop_at:
            self._drop_at = at
            self.viewport().update()

    # -- painting -------------------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        bar = self.drop_indicator()
        if bar is not None:
            painter = QPainter(self.viewport())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(current_colors().accent_text))
            painter.drawRoundedRect(bar, 1.5, 1.5)

    # -- drag and drop --------------------------------------------------------------------
    def _acceptable(self, event: QDragEnterEvent | QDragMoveEvent) -> bool:
        mime = event.mimeData()
        return self._model.internal_rows(mime) is not None or bool(self._model.local_files(mime))

    def _over(self, event: QDragEnterEvent | QDragMoveEvent) -> None:
        if not self._acceptable(event):
            self._set_drop(None)
            event.ignore()
            return
        pos = event.position().toPoint()
        self._set_drop(self.insertion_index(pos))
        bar = self.verticalScrollBar()
        if pos.y() < EDGE:
            bar.setValue(bar.value() - bar.singleStep())
        elif pos.y() > self.viewport().height() - EDGE:
            bar.setValue(bar.value() + bar.singleStep())
        if self._model.internal_rows(event.mimeData()) is not None:
            event.setDropAction(Qt.DropAction.MoveAction)
        else:
            event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self._over(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._over(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drop(None)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drop(None)
        mime = event.mimeData()
        at = self.insertion_index(event.position().toPoint())
        rows = self._model.internal_rows(mime)
        if rows is not None:
            new_rows = self._model.move_rows(rows, at)
            self.select_rows(new_rows)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        elif self._model.local_files(mime):
            self.files_dropped.emit(self._model.local_files(mime), at)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        drag = QDrag(self)
        drag.setMimeData(self._model.mimeData([self._model.index(r) for r in rows]))
        first = self.card_at(rows[0])
        pixmap = self.viewport().grab(first)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(first.width() // 2, first.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)
        # Nothing after exec(): the drop already moved the rows, and the default
        # clearOrRemove would delete them from the model.
        self._set_drop(None)

    # -- selection ------------------------------------------------------------------------
    def select_rows(self, rows: Sequence[int]) -> None:
        selection = self.selectionModel()
        selection.clearSelection()
        for row in rows:
            selection.select(self._model.index(row), selection.SelectionFlag.Select)
        if rows:
            selection.setCurrentIndex(self._model.index(rows[0]), selection.SelectionFlag.NoUpdate)
            self.scrollTo(self._model.index(rows[0]))

    # -- mouse ----------------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            if index.isValid() and remove_rect(self.card_at(index.row())).contains(pos):
                self._swallow_release = True
                self.remove_rows([index.row()])
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._swallow_release:
            self._swallow_release = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and event.button() == Qt.MouseButton.LeftButton:
            self.activated_row.emit(index.row())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # -- editing --------------------------------------------------------------------------
    def remove_rows(self, rows: Sequence[int]) -> None:
        first = min(rows) if rows else 0
        self._model.remove_rows(rows)
        count = self._model.rowCount()
        if count:
            self.select_rows([min(first, count - 1)])

    def move_selected(self, where: str, rows: Sequence[int] | None = None) -> None:
        """Move the selected rows (or ``rows``): "start", "earlier", "later" or "end"."""
        chosen = sorted(rows if rows is not None else self.selected_rows())
        count = self._model.rowCount()
        if not chosen:
            return
        if where == "start":
            before = 0
        elif where == "earlier":
            before = max(0, chosen[0] - 1)
        elif where == "later":
            before = min(count, chosen[-1] + 2)
        else:
            before = count
        self.select_rows(self._model.move_rows(chosen, before))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if ctrl and not mods & Qt.KeyboardModifier.ShiftModifier:
            where = {
                Qt.Key.Key_Left: "earlier",
                Qt.Key.Key_Up: "earlier",
                Qt.Key.Key_Right: "later",
                Qt.Key.Key_Down: "later",
                Qt.Key.Key_Home: "start",
                Qt.Key.Key_End: "end",
            }.get(Qt.Key(key))
            if where is not None:
                self.move_selected(where)
                return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            rows = self.selected_rows()
            if rows:
                self.remove_rows(rows)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2):
            current = self.currentIndex()
            if current.isValid():
                self.activated_row.emit(current.row())
            return
        if key == Qt.Key.Key_Menu or (
            key == Qt.Key.Key_F10 and mods & Qt.KeyboardModifier.ShiftModifier
        ):
            self._show_menu(None)
            return
        super().keyPressEvent(event)

    # -- menu -----------------------------------------------------------------------------
    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        keyboard = event.reason() == QContextMenuEvent.Reason.Keyboard
        self._show_menu(None if keyboard else event.pos())

    def _show_menu(self, pos: QPoint | None) -> None:
        index = self.indexAt(pos) if pos is not None else self.currentIndex()
        if not index.isValid():
            return
        if index.row() not in self.selected_rows():
            self.select_rows([index.row()])
        where = pos if pos is not None else self.card_at(index.row()).center()
        self.build_menu(index.row()).exec(self.viewport().mapToGlobal(where))

    def build_menu(self, row: int) -> QMenu:
        entry = self._model.entry(row)
        count = self._model.rowCount()
        selected = self.selected_rows()
        rows = selected if row in selected else [row]
        menu = QMenu(self)
        menu.setAccessibleName(_tr("Actions for {name}").format(name=entry.path.name))
        pages = menu.addAction(
            icon("file-pen-line"), _tr("Choose &Pages…"), lambda: self.pages_requested.emit(row)
        )
        pages.setEnabled(not entry.image and entry.state is EntryState.READY)
        if entry.state is EntryState.LOCKED:
            menu.addAction(
                icon("lock-open"), _tr("&Unlock…"), lambda: self.unlock_requested.emit(row)
            )
        menu.addSeparator()
        start = menu.addAction(_tr("Move to &Start"), lambda: self.move_selected("start", rows))
        start.setEnabled(min(rows) > 0)
        earlier = menu.addAction(_tr("Move &Earlier"), lambda: self.move_selected("earlier", rows))
        earlier.setEnabled(min(rows) > 0)
        later = menu.addAction(_tr("Move &Later"), lambda: self.move_selected("later", rows))
        later.setEnabled(max(rows) < count - 1)
        end = menu.addAction(_tr("Move to &End"), lambda: self.move_selected("end", rows))
        end.setEnabled(max(rows) < count - 1)
        menu.addSeparator()
        menu.addAction(_tr("Show in &Folder"), lambda: show_in_folder(Path(entry.path))).setEnabled(
            entry.path.exists()
        )
        menu.addSeparator()
        remove = menu.addAction(icon("trash-2"), _tr("&Remove"), lambda: self.remove_rows(rows))
        remove.setShortcut(Qt.Key.Key_Delete)
        return menu
