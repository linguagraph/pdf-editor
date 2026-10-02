"""Document tab strip: rounded tabs, a hover close button, an unsaved-changes dot, a "+" button
and a menu of all open documents when the tabs don't fit.

The close button is our own widget rather than Qt's, which Fusion draws as a red-boxed cross on
every tab. Ours is the tinted ``x`` icon, shown on the active and the hovered tab. A tab with
unsaved changes shows a dot in the same spot (the cross while hovered), so the tab text keeps
its width, unlike with a trailing "*".
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QHoverEvent, QIcon, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QMenu, QTabBar, QTabWidget, QToolButton, QWidget

from pdfeditor.ui.icons import icon
from pdfeditor.ui.theme import current_colors

CLOSE_SIDE = QTabBar.ButtonPosition.RightSide


class TabCloseButton(QToolButton):
    """Close button of one tab. ``mode`` is what it shows: the cross, the dirty dot, or nothing."""

    CLOSE, DIRTY, HIDDEN = "close", "dirty", "hidden"

    def __init__(self, bar: DocumentTabBar) -> None:
        super().__init__(bar)
        self._bar = bar
        self.mode = self.HIDDEN
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the keyboard closes tabs with Ctrl+W
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setIconSize(QSize(12, 12))
        self.setFixedSize(QSize(20, 20))
        self.clicked.connect(self._close)

    def _close(self) -> None:
        index = self._bar.button_index(self)
        if index >= 0:
            self._bar.tabCloseRequested.emit(index)

    def set_mode(self, mode: str, name: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.setIcon(icon("x") if mode == self.CLOSE else QIcon())
            self.update()
        dirty = self._bar.is_dirty(self._bar.button_index(self))
        tip = self.tr("Close {name} (unsaved changes)") if dirty else self.tr("Close {name}")
        self.setToolTip(tip.format(name=name))
        self.setAccessibleName(self.tr("Close {name}").format(name=name))

    def enterEvent(self, event: QEvent) -> None:
        super().enterEvent(event)  # type: ignore[arg-type]
        self._bar.set_hover_index(self._bar.button_index(self))

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.mode != self.DIRTY:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(current_colors().text_muted))
        painter.drawEllipse(QRectF(self.rect()).center(), 4.0, 4.0)
        painter.end()


class DocumentTabBar(QTabBar):
    overflow_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hover = -1
        self._middle_pressed = -1
        self._overflowing = False
        self.setObjectName("DocumentTabBar")
        self.setAccessibleName(self.tr("Document tabs"))
        self.setTabsClosable(False)  # our own buttons, see TabCloseButton
        self.setMovable(True)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.setDrawBase(False)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.currentChanged.connect(lambda _i: self.refresh_buttons())
        self.tabMoved.connect(lambda _a, _b: self.refresh_buttons())

    # -- buttons --------------------------------------------------------------------------
    def close_button(self, index: int) -> TabCloseButton | None:
        button = self.tabButton(index, CLOSE_SIDE)
        return button if isinstance(button, TabCloseButton) else None

    def button_index(self, button: QWidget) -> int:
        return next((i for i in range(self.count()) if self.tabButton(i, CLOSE_SIDE) is button), -1)

    def set_hover_index(self, index: int) -> None:
        if index != self._hover:
            self._hover = index
            self.refresh_buttons()

    def set_dirty(self, index: int, dirty: bool) -> None:
        self.setTabData(index, bool(dirty))
        name = self.tabText(index)
        unsaved = QCoreApplication.translate("DocumentTabBar", "{name}, unsaved changes")
        self.setAccessibleTabName(index, unsaved.format(name=name) if dirty else name)
        self.refresh_buttons()

    def is_dirty(self, index: int) -> bool:
        return 0 <= index < self.count() and bool(self.tabData(index))

    def refresh_buttons(self) -> None:
        current = self.currentIndex()
        for i in range(self.count()):
            button = self.close_button(i)
            if button is None:
                continue
            if i == self._hover:
                mode = TabCloseButton.CLOSE
            elif self.is_dirty(i):
                mode = TabCloseButton.DIRTY
            elif i == current:
                mode = TabCloseButton.CLOSE
            else:
                mode = TabCloseButton.HIDDEN
            button.set_mode(mode, self.tabText(i))

    def tabInserted(self, index: int) -> None:
        super().tabInserted(index)
        self.setTabButton(index, CLOSE_SIDE, TabCloseButton(self))
        self.refresh_buttons()

    def tabRemoved(self, index: int) -> None:
        super().tabRemoved(index)
        self._hover = -1
        self.refresh_buttons()

    # -- hover and middle click -----------------------------------------------------------
    def event(self, event: QEvent) -> bool:
        if isinstance(event, QHoverEvent):
            if event.type() == QEvent.Type.HoverLeave:
                self.set_hover_index(-1)
            else:
                self.set_hover_index(self.tabAt(event.position().toPoint()))
        return super().event(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._middle_pressed = self.tabAt(event.position().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            pressed, self._middle_pressed = self._middle_pressed, -1
            if index >= 0 and index == pressed:
                self.tabCloseRequested.emit(index)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- overflow -------------------------------------------------------------------------
    def is_overflowing(self) -> bool:
        if self.count() < 2:
            return False
        needed = sum(self.tabSizeHint(i).width() for i in range(self.count()))
        return needed > self.width()

    def check_overflow(self) -> None:
        overflowing = self.is_overflowing()
        if overflowing != self._overflowing:
            self._overflowing = overflowing
            self.overflow_changed.emit(overflowing)

    def tabLayoutChange(self) -> None:
        super().tabLayoutChange()
        self.check_overflow()

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        self.check_overflow()


class DocumentTabWidget(QTabWidget):
    """The documents area's tab widget. ``open_requested`` fires from the "+" button,
    ``close_requested(index)`` from a close button or a middle click."""

    open_requested = Signal()
    # QTabWidget doesn't relay tabCloseRequested from a bar that isn't "closable", so ours is
    # a signal of its own.
    close_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bar = DocumentTabBar(self)
        self.setTabBar(self.bar)
        self.bar.tabCloseRequested.connect(self.close_requested)
        self.setAccessibleName(self.tr("Open documents"))
        self.setDocumentMode(True)
        self.setMovable(True)

        corner = QWidget(self)
        corner.setObjectName("DocumentTabsCorner")
        corner.setAccessibleName(self.tr("Document tab actions"))
        layout = QHBoxLayout(corner)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        self.overflow_button = QToolButton(corner)
        self.overflow_button.setIcon(icon("chevron-down"))
        self.overflow_button.setToolTip(self.tr("All open documents"))
        self.overflow_button.setAccessibleName(self.tr("All open documents"))
        self.overflow_button.setAutoRaise(True)
        self.overflow_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.documents_menu = QMenu(self.overflow_button)
        self.documents_menu.setAccessibleName(self.tr("Open documents"))
        self.documents_menu.aboutToShow.connect(self.fill_documents_menu)
        self.overflow_button.setMenu(self.documents_menu)
        self.overflow_button.setObjectName("DocumentTabsMenu")
        self.overflow_button.hide()
        self.plus_button = QToolButton(corner)
        self.plus_button.setIcon(icon("plus"))
        self.plus_button.setToolTip(self.tr("Open a file (Ctrl+O)"))
        self.plus_button.setAccessibleName(self.tr("Open a file"))
        self.plus_button.setAutoRaise(True)
        self.plus_button.clicked.connect(self.open_requested)
        layout.addWidget(self.overflow_button)
        layout.addWidget(self.plus_button)
        self.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.bar.overflow_changed.connect(self.overflow_button.setVisible)

    def set_dirty(self, index: int, dirty: bool) -> None:
        self.bar.set_dirty(index, dirty)

    def is_dirty(self, index: int) -> bool:
        return self.bar.is_dirty(index)

    def fill_documents_menu(self) -> None:
        menu = self.documents_menu
        menu.clear()
        for i in range(self.count()):
            text = self.tabText(i).replace("&", "&&")
            action = menu.addAction(f"{text}  •" if self.is_dirty(i) else text)
            action.setCheckable(True)
            action.setChecked(i == self.currentIndex())
            action.setToolTip(self.tabToolTip(i))
            action.triggered.connect(lambda _=False, index=i: self._activate(index))

    def _activate(self, index: int) -> None:
        if 0 <= index < self.count():
            self.setCurrentIndex(index)
            widget = self.currentWidget()
            if widget is not None:
                widget.setFocus()

    def tabInserted(self, index: int) -> None:
        super().tabInserted(index)
        self.bar.check_overflow()

    def tabRemoved(self, index: int) -> None:
        super().tabRemoved(index)
        self.bar.check_overflow()

    def tab_center(self, index: int) -> QPoint:
        """Where to click to hit tab ``index`` (tests)."""
        return self.bar.tabRect(index).center()
