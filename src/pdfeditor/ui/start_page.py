"""Start screen, shown while no document is open.

A large Open button and a drop zone, quick actions for the tools that start without a document,
and the recent files as cards with a preview of their first page. Previews are rendered by a
background :class:`Job` (engine work holds the engine lock and never runs on the GUI thread) and
kept in memory per path and modification time.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFont,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.engine.base import Engine
from pdfeditor.services.thumbnails import first_page_thumbnail
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.recent_files import RecentFiles
from pdfeditor.ui.reveal import show_in_folder
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors, theme_manager
from pdfeditor.ui.view.renderer import to_qimage

log = logging.getLogger(__name__)

THUMB = QSize(112, 144)  # logical pixels; rendered at the screen's device pixel ratio
CARD = QSize(160, 216)
MAX_WIDTH = 1040  # of the content column
PATH_ROLE = Qt.ItemDataRole.UserRole
PINNED_ROLE = Qt.ItemDataRole.UserRole + 1
MISSING_ROLE = Qt.ItemDataRole.UserRole + 2


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime if path.is_file() else None
    except OSError:
        return None


class RecentFileDelegate(QStyledItemDelegate):
    """Draws a recent file as a card: page preview, name, folder, pin and missing state."""

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return CARD

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        c = current_colors()
        state = option.state
        # The current card is only highlighted while the grid has focus: on arrival, the
        # first card shouldn't look picked.
        widget = option.widget
        active = widget is not None and widget.hasFocus()
        selected = active and bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        focused = bool(state & QStyle.StateFlag.State_HasFocus)
        missing = bool(index.data(MISSING_ROLE))
        pinned = bool(index.data(PINNED_ROLE))
        card = QRect(option.rect).adjusted(4, 4, -4, -4)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        background = c.accent_subtle if selected else c.hover if hovered else c.surface
        outline = c.accent_text if focused else c.border
        painter.setPen(QPen(QColor(outline), 2 if focused else 1))
        painter.setBrush(QColor(background))
        radius = METRICS.radius_large
        painter.drawRoundedRect(card.adjusted(1, 1, -1, -1), radius, radius)

        # page preview, centered in its slot, with a thin page border
        slot = QRect(
            card.x() + (card.width() - THUMB.width()) // 2,
            card.y() + 10,
            THUMB.width(),
            THUMB.height(),
        )
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        painter.setOpacity(0.45 if missing else 1.0)
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
            page = QRect(slot.x() + 12, slot.y() + 4, slot.width() - 24, slot.height() - 8)
            painter.setPen(QPen(QColor(c.border_strong), 1))
            painter.setBrush(QColor(c.surface_alt))
            painter.drawRect(page)
            glyph = icon("file-x" if missing else "file")
            glyph.paint(painter, QRect(page.center() - QPoint(14, 14), QSize(28, 28)))
        painter.setOpacity(1.0)

        # name and folder (or "not found")
        text_rect = QRect(card.x() + 8, slot.bottom() + 8, card.width() - 16, 18)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(c.text_muted if missing else c.text))
        name = painter.fontMetrics().elidedText(
            str(index.data(Qt.ItemDataRole.DisplayRole)),
            Qt.TextElideMode.ElideMiddle,
            text_rect.width(),
        )
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, name
        )
        font.setWeight(QFont.Weight.Normal)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        painter.setPen(QColor(c.text_muted))
        path = Path(str(index.data(PATH_ROLE)))
        detail = "File not found" if missing else (path.parent.name or str(path.parent))
        detail = painter.fontMetrics().elidedText(
            detail, Qt.TextElideMode.ElideMiddle, text_rect.width()
        )
        painter.drawText(
            text_rect.translated(0, 18),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            detail,
        )

        if pinned:
            badge = QRect(card.right() - 28, card.y() + 6, 22, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c.surface))
            painter.drawEllipse(badge)
            icon("pin").paint(painter, badge.adjusted(4, 4, -4, -4))
        painter.restore()


class RecentFilesView(QListWidget):
    """Grid of recent files. Enter or a click opens, Delete removes, the menu key pins."""

    open_requested = Signal(str)
    remove_requested = Signal(str)
    pin_requested = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("RecentFiles")
        self.setAccessibleName("Recent files")
        self.setAccessibleDescription("Enter opens a file, Delete removes it, the menu key pins it")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setGridSize(CARD + QSize(8, 8))
        self.setSpacing(0)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setItemDelegate(RecentFileDelegate(self))
        self.clicked.connect(self._clicked)

    def current_path(self) -> str | None:
        item = self.currentItem()
        return str(item.data(PATH_ROLE)) if item is not None else None

    def item_for(self, path: str) -> QListWidgetItem | None:
        return next(
            (it for i in range(self.count()) if (it := self.item(i)).data(PATH_ROLE) == path),
            None,
        )

    def _clicked(self, index: QModelIndex) -> None:
        self.open_requested.emit(str(index.data(PATH_ROLE)))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        path = self.current_path()
        key = event.key()
        if path is not None and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_requested.emit(path)
        elif path is not None and key == Qt.Key.Key_Delete:
            self.remove_requested.emit(path)
        elif key == Qt.Key.Key_F10 and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._show_menu(None)
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        keyboard = event.reason() == QContextMenuEvent.Reason.Keyboard
        self._show_menu(None if keyboard else event.pos())

    def build_menu(self, item: QListWidgetItem) -> QMenu:
        path = str(item.data(PATH_ROLE))
        pinned = bool(item.data(PINNED_ROLE))
        missing = bool(item.data(MISSING_ROLE))
        menu = QMenu(self)
        menu.setAccessibleName(f"Actions for {item.text()}")
        menu.addAction(
            icon("folder-open"), "&Open", lambda: self.open_requested.emit(path)
        ).setEnabled(not missing)
        menu.addAction(
            icon("pin-off" if pinned else "pin"),
            "Un&pin" if pinned else "&Pin to Top",
            lambda: self.pin_requested.emit(path, not pinned),
        )
        folder = menu.addAction(
            "Show in &Folder",
            lambda: show_in_folder(Path(path)),
        )
        folder.setEnabled(not missing)
        menu.addSeparator()
        remove = menu.addAction(
            icon("trash-2"), "&Remove from List", lambda: self.remove_requested.emit(path)
        )
        remove.setShortcut(Qt.Key.Key_Delete)
        return menu

    def _show_menu(self, pos: QPoint | None) -> None:
        item = self.itemAt(pos) if pos is not None else self.currentItem()
        if item is None:
            return
        self.setCurrentItem(item)
        where = pos if pos is not None else self.visualItemRect(item).center()
        self.build_menu(item).exec(self.viewport().mapToGlobal(where))


class DropZone(QFrame):
    """Dashed target for dropped PDFs; clicking it (or Enter) opens the file dialog too."""

    files_dropped = Signal(list)  # list[Path]
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAccessibleName("Drop zone: drop PDF files here to open them")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("dragging", False)
        self.setMinimumHeight(88)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        glyph = QLabel(self)
        glyph.setPixmap(icon("upload").pixmap(QSize(28, 28), self.devicePixelRatioF()))
        glyph.setAccessibleName("Upload")
        text = QLabel("Drop PDF files here\nor click to browse", self)
        text.setProperty("role", "muted")
        layout.addStretch(1)
        layout.addWidget(glyph)
        layout.addSpacing(8)
        layout.addWidget(text)
        layout.addStretch(1)

    @staticmethod
    def _pdfs(event: QDragEnterEvent | QDropEvent) -> list[Path]:
        return [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if u.isLocalFile() and u.toLocalFile().lower().endswith(".pdf")
        ]

    def _set_dragging(self, on: bool) -> None:
        self.setProperty("dragging", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._pdfs(event):
            event.acceptProposedAction()
            self._set_dragging(True)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_dragging(False)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_dragging(False)
        paths = self._pdfs(event)
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()


class StartPage(QWidget):
    """``open_path_requested`` opens a recent or dropped file; ``open_dialog_requested`` asks
    for one."""

    open_path_requested = Signal(Path)
    open_dialog_requested = Signal()

    def __init__(self, recent: RecentFiles, engine: Engine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StartPage")
        self.setAccessibleName("Start page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.recent = recent
        self._engine = engine
        self._thumbs: dict[str, tuple[float, QPixmap | None]] = {}
        self._job: Job | None = None
        self._again = False  # the list changed while a job ran: render again after it
        self._closed = False
        self.quick_buttons: dict[str, QToolButton] = {}

        # A centered column up to MAX_WIDTH wide; the side stretches only take what's left.
        # It sits in a scroll area so the page never sets a minimum size for the window: a
        # small window scrolls the start page instead of growing (or squeezing the docks).
        m = METRICS
        scroll = QScrollArea(self)
        scroll.setObjectName("StartScroll")
        scroll.setAccessibleName("Start page content")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.viewport().setObjectName("StartScrollViewport")
        inner = QWidget(scroll)
        inner.setObjectName("StartInner")
        scroll.setWidget(inner)
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(scroll)
        outer = QHBoxLayout(inner)
        outer.setContentsMargins(m.space(8), m.space(8), m.space(8), m.space(4))
        column = QWidget(inner)
        column.setMaximumWidth(MAX_WIDTH)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(m.space(3))
        outer.addStretch(1)
        outer.addWidget(column, 100)
        outer.addStretch(1)

        title = QLabel("Welcome to pdfeditor", column)
        title.setObjectName("StartTitle")
        subtitle = QLabel("Open a PDF to start, or pick up where you left off.", column)
        subtitle.setProperty("role", "muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(m.space(2))

        top = QHBoxLayout()
        top.setSpacing(m.space(4))
        self.open_button = QPushButton("  Open File…", column)
        self.open_button.setObjectName("StartOpenButton")
        self.open_button.setAccessibleName("Open file")
        self.open_button.setToolTip("Open a PDF (Ctrl+O)")
        self.open_button.setIconSize(QSize(20, 20))
        self.open_button.setMinimumHeight(88)
        self.open_button.setMinimumWidth(200)
        self.open_button.clicked.connect(self.open_dialog_requested)
        self.drop_zone = DropZone(column)
        self.drop_zone.files_dropped.connect(self._dropped)
        self.drop_zone.clicked.connect(self.open_dialog_requested)
        top.addWidget(self.open_button)
        top.addWidget(self.drop_zone, 1)
        layout.addLayout(top)

        layout.addSpacing(m.space(2))
        layout.addWidget(self._section("Quick actions", column))
        self.quick_row = QHBoxLayout()
        self.quick_row.setSpacing(m.space(2))
        self.quick_row.addStretch(1)
        layout.addLayout(self.quick_row)

        layout.addSpacing(m.space(2))
        header = QHBoxLayout()
        header.addWidget(self._section("Recent files", column))
        header.addStretch(1)
        hint = QLabel("Right-click a file to pin or remove it", column)
        hint.setProperty("role", "muted")
        header.addWidget(hint)
        layout.addLayout(header)
        self.recent_view = RecentFilesView(column)
        self.recent_view.open_requested.connect(lambda p: self.open_path_requested.emit(Path(p)))
        self.recent_view.remove_requested.connect(self.recent.forget)
        self.recent_view.pin_requested.connect(self.recent.set_pinned)
        self.empty_label = QLabel("Files you open will appear here.", column)
        self.empty_label.setProperty("role", "muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.recent_view, 1)
        layout.addWidget(self.empty_label, 1)

        self.recent.changed.connect(self.refresh)
        theme_manager().changed.connect(self._theme_changed)
        self._theme_changed()
        self.refresh()

    def _theme_changed(self) -> None:
        # the Open button is accent-filled: its icon takes the on-accent color, not the text's
        self.open_button.setIcon(icon("folder-open", current_colors().on_accent))
        self.recent_view.viewport().update()

    @staticmethod
    def _section(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("StartSection")
        return label

    def set_quick_actions(self, actions: Sequence[tuple[str, str, QAction]]) -> None:
        """(label, icon, action) buttons; each triggers its action."""
        previous: QWidget = self.open_button  # the drop zone is for the mouse only
        for label, icon_name, action in actions:
            button = QToolButton(self)
            button.setObjectName("QuickAction")
            button.setText(label)
            button.setIcon(icon(icon_name))
            button.setIconSize(QSize(20, 20))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setAccessibleName(label)
            button.setToolTip(action.toolTip() or label)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.clicked.connect(lambda _=False, a=action: a.trigger())
            self.quick_row.insertWidget(self.quick_row.count() - 1, button)
            self.quick_buttons[label] = button
            QWidget.setTabOrder(previous, button)
            previous = button
        QWidget.setTabOrder(previous, self.recent_view)

    # -- recent files ---------------------------------------------------------------------
    def refresh(self) -> None:
        if self._closed:
            return
        view = self.recent_view
        current = view.current_path()
        view.clear()
        pending: list[tuple[str, float]] = []
        pinned = set(self.recent.pinned())
        for p in self.recent.files():
            path = Path(p)
            mtime = _mtime(path)
            item = QListWidgetItem(path.name)
            item.setData(PATH_ROLE, p)
            item.setData(PINNED_ROLE, p in pinned)
            item.setData(MISSING_ROLE, mtime is None)
            item.setToolTip(p if mtime is not None else f"{p}\n(file not found)")
            notes = [path.parent.name] + (["pinned"] if p in pinned else [])
            notes += ["file not found"] if mtime is None else []
            item.setData(Qt.ItemDataRole.AccessibleTextRole, ", ".join([path.name, *notes]))
            cached = self._thumbs.get(p)
            if cached is not None and cached[0] == mtime and cached[1] is not None:
                item.setData(Qt.ItemDataRole.DecorationRole, cached[1])
            elif mtime is not None and (cached is None or cached[0] != mtime):
                pending.append((p, mtime))
            view.addItem(item)
            if p == current:
                view.setCurrentItem(item)
        if view.currentItem() is None and view.count():
            view.setCurrentRow(0)
        has_files = view.count() > 0
        view.setVisible(has_files)
        self.empty_label.setVisible(not has_files)
        if pending:
            self._render(pending)

    def _render(self, pending: list[tuple[str, float]]) -> None:
        if self._job is not None:
            self._again = True
            return
        engine = self._engine
        ratio = self.devicePixelRatioF()
        width, height = round(THUMB.width() * ratio), round(THUMB.height() * ratio)

        def work(job: Job) -> None:
            for p, mtime in pending:
                job.token.check()
                with ENGINE_LOCK:
                    result = first_page_thumbnail(engine, Path(p), width, height)
                image = to_qimage(result) if result is not None else None
                job.report((p, mtime, image))

        job = Job(work)
        job.partial.connect(self._thumbnail_ready)
        job.finished.connect(self._job_done)
        job.failed.connect(self._job_done)
        job.cancelled.connect(self._job_done)
        self._job = job
        job.start()

    def _thumbnail_ready(self, value: tuple[str, float, QImage | None]) -> None:
        p, mtime, image = value
        pixmap: QPixmap | None = None
        if image is not None:
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(self.devicePixelRatioF())
        self._thumbs[p] = (mtime, pixmap)
        item = self.recent_view.item_for(p)
        if item is not None and pixmap is not None:
            item.setData(Qt.ItemDataRole.DecorationRole, pixmap)

    def _job_done(self, *_args: object) -> None:
        self._job = None
        if self._again and not self._closed:
            self._again = False
            self.refresh()

    def thumbnails_pending(self) -> bool:
        return self._job is not None

    def has_thumbnail(self, path: str) -> bool:
        cached = self._thumbs.get(path)
        return cached is not None and cached[1] is not None

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Stop the preview job and wait for it: its signals must not outlive the page."""
        self._closed = True
        job = self._job
        if job is None:
            return
        job.cancel()
        deadline = time.monotonic() + timeout_ms / 1000
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.005)
        for signal in (job.partial, job.finished, job.failed, job.cancelled):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect()
        self._job = None

    def _dropped(self, paths: list[Path]) -> None:
        for path in paths:
            self.open_path_requested.emit(path)
