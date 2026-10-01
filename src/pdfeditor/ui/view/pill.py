"""The floating page/zoom pill at the bottom centre of the document view.

One pill per window. It moves into whichever view is current and fades out when the mouse and
keyboard leave it alone, coming back on mouse move, scroll or focus.

It is a child of the view's viewport, not a sibling: a sibling overlapping the viewport stops
Qt from scrolling it by blitting (every scroll step would repaint the whole page area). As a
child it is blitted along with the pages, and put back in place right after.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise

from PySide6.QtCore import (
    QAbstractAnimation,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from pdfeditor.ui.icons import icon
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors, theme_manager
from pdfeditor.ui.view import motion
from pdfeditor.ui.view.document_view import DocumentView

IDLE_MS = 3000  # fade out after this long without mouse, scroll or focus
SHADOW = 4  # px around the pill reserved for its drop shadow
BOTTOM_MARGIN = 16  # px between the pill and the bottom of the page area


def _button(parent: QWidget, name: str, tip: str, slot: Callable[[], None]) -> QToolButton:
    b = QToolButton(parent)
    b.setIcon(icon(name))
    b.setToolTip(tip)
    b.setAccessibleName(tip)
    b.setAutoRaise(True)
    b.clicked.connect(slot)
    return b


class PageNavigator(QWidget):
    """Previous, [page] / N, next; accepts page labels or numbers.

    First/last page buttons exist for the Go menu's actions and scripts but aren't shown: the
    pill stays small, and Home/End do the same from the keyboard.
    """

    def __init__(self, pill: CanvasPill) -> None:
        super().__init__(pill)
        self._pill = pill
        self.first = _button(self, "chevrons-up", "First page", lambda: self._go("first_page"))
        self.prev = _button(
            self, "chevron-left", "Previous page", lambda: self._go("previous_page")
        )
        self.edit = QLineEdit(self)
        self.edit.setAccessibleName("Page number")
        self.edit.setToolTip("Page number: type a page and press Enter")
        self.edit.setFixedWidth(44)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit.returnPressed.connect(self._jump)
        self.total = QLabel(self)
        self.total.setAccessibleName("Page count")
        self.next = _button(self, "chevron-right", "Next page", lambda: self._go("next_page"))
        self.last = _button(self, "chevrons-down", "Last page", lambda: self._go("last_page"))
        self.first.hide()
        self.last.hide()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.space(1))
        for w in (self.first, self.prev, self.edit, self.total, self.next, self.last):
            layout.addWidget(w)

    def _go(self, method: str) -> None:
        view = self._pill.view
        if view is not None:
            getattr(view, method)()

    def update_state(self, view: DocumentView | None) -> None:
        enabled = view is not None and view.page_count > 0
        for w in (self.first, self.prev, self.edit, self.next, self.last):
            w.setEnabled(enabled)
        if view is None or not enabled:
            self.edit.clear()
            self.total.clear()
            return
        page = view.current_page
        label = view.page_label(page)
        if not self.edit.hasFocus():  # don't overwrite what the user is typing
            self.edit.setText(label)
        numeric = label == str(page + 1)
        self.total.setText(
            f"/ {view.page_count}" if numeric else f"({page + 1} / {view.page_count})"
        )
        self.first.setEnabled(page > 0)
        self.prev.setEnabled(page > 0)
        self.next.setEnabled(page < view.page_count - 1)
        self.last.setEnabled(page < view.page_count - 1)

    def _jump(self) -> None:
        view = self._pill.view
        if view is None:
            return
        index = view.page_index_for_label(self.edit.text())
        if index is None:
            QApplication.beep()
            self.edit.clearFocus()
            self.update_state(view)
        else:
            view.setFocus()
            view.go_to_page(index, animated=True)
            self.update_state(view)


class ZoomBox(QComboBox):
    PRESETS = ("Fit Width", "Fit Page", "50%", "75%", "100%", "125%", "150%", "200%", "400%")

    def __init__(self, pill: CanvasPill) -> None:
        super().__init__(pill)
        self._pill = pill
        self.setEditable(True)
        self.setAccessibleName("Zoom")
        self.setToolTip("Zoom: pick a preset or type a percentage")
        edit = self.lineEdit()
        if edit is not None:
            edit.setAccessibleName("Zoom")
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems(self.PRESETS)
        self.setMinimumContentsLength(5)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.textActivated.connect(self._apply)

    def show_zoom(self, zoom: float) -> None:
        self.setEditText(f"{zoom * 100:.0f}%")

    def _apply(self, text: str) -> None:
        view = self._pill.view
        if view is None:
            return
        if text == "Fit Width":
            view.fit_width()
        elif text == "Fit Page":
            view.fit_page()
        else:
            try:
                view.set_zoom(float(text.strip().rstrip("%")) / 100)
            except ValueError:
                self.show_zoom(view.zoom)
        view.setFocus()


class CanvasPill(QFrame):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("canvasPill")
        self.setAccessibleName("Page and zoom controls")
        # Faded out, the pill stays shown (transparent, and to the mouse too) so it remains in
        # the focus chain: Tab from the page reaches it and focusing it fades it back in.
        self.setAutoFillBackground(False)
        self.view: DocumentView | None = None
        self._home = parent  # where the pill waits while no document is open
        self.navigator = PageNavigator(self)
        self.zoom_out_button = _button(self, "minus", "Zoom out", lambda: self._zoom("zoom_out"))
        self.zoom_box = ZoomBox(self)
        self.zoom_in_button = _button(self, "plus", "Zoom in", lambda: self._zoom("zoom_in"))
        self.fit_width_button = _button(
            self, "move-horizontal", "Fit width", lambda: self._zoom("fit_width")
        )
        self.fit_page_button = _button(self, "expand", "Fit page", lambda: self._zoom("fit_page"))
        self._separator = QWidget(self)  # painted by paintEvent, in the border color
        self._separator.setFixedWidth(1)
        layout = QHBoxLayout(self)
        pad = METRICS.space(2)
        layout.setContentsMargins(
            SHADOW + pad, SHADOW + METRICS.space(1), SHADOW + pad, SHADOW + METRICS.space(1)
        )
        layout.setSpacing(METRICS.space(1))
        layout.addWidget(self.navigator)
        layout.addWidget(self._separator)
        for w in (
            self.zoom_out_button,
            self.zoom_box,
            self.zoom_in_button,
            self.fit_width_button,
            self.fit_page_button,
        ):
            layout.addWidget(w)

        # The effect is switched on only while faded or fading: drawn through it, the pill
        # would be rendered offscreen whenever the page under it repaints (every scroll step).
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self._effect.setEnabled(False)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(motion.FADE_MS)
        self._fade.finished.connect(self._on_fade_done)
        self.idle_ms = IDLE_MS
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._on_idle)
        # Focusing a control (Tab from the page) brings the pill back.
        for child in self.findChildren(QWidget):
            if child.focusPolicy() != Qt.FocusPolicy.NoFocus:
                child.installEventFilter(self)
        theme_manager().changed.connect(self.update)
        self.hide()

    # -- binding --------------------------------------------------------------------------
    def attach(self, view: DocumentView | None) -> None:
        """Move into ``view`` (the current document), or out of sight when there's none."""
        if view is self.view:
            self.update_state()
            return
        old = self.view
        if old is not None:
            try:
                old.user_activity.disconnect(self.poke)
                old.geometry_changed.disconnect(self._place)
                old.current_page_changed.disconnect(self._on_page_changed)
                old.zoom_changed.disconnect(self._on_zoom_changed)
                old.layout_changed.disconnect(self.update_state)
                old.verticalScrollBar().valueChanged.disconnect(self._on_scrolled)
                old.horizontalScrollBar().valueChanged.disconnect(self._on_scrolled)
            except (RuntimeError, TypeError):  # the view is already being destroyed
                pass
        self.view = view
        self.setParent(view.viewport() if view is not None else self._home)
        if view is None:
            self.hide()
            self.update_state()
            return
        # Signals rather than event filters on the view: a Python filter can be called while
        # the view is torn down at interpreter exit, which crashes.
        view.user_activity.connect(self.poke)
        view.geometry_changed.connect(self._place)
        view.current_page_changed.connect(self._on_page_changed)
        view.zoom_changed.connect(self._on_zoom_changed)
        view.layout_changed.connect(self.update_state)
        # connected after QAbstractScrollArea's own handler, so this runs after the blit
        view.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        view.horizontalScrollBar().valueChanged.connect(self._on_scrolled)
        # Tab from the page walks through the pill's controls, left to right.
        nav = self.navigator
        chain = [
            view,
            nav.prev,
            nav.edit,
            nav.next,
            self.zoom_out_button,
            self.zoom_box,
            self.zoom_in_button,
            self.fit_width_button,
            self.fit_page_button,
        ]
        for first, second in pairwise(chain):
            QWidget.setTabOrder(first, second)
        self.update_state()
        self.poke()

    def update_state(self) -> None:
        view = self.view
        self.navigator.update_state(view)
        enabled = view is not None and view.page_count > 0
        for w in (
            self.zoom_out_button,
            self.zoom_box,
            self.zoom_in_button,
            self.fit_width_button,
            self.fit_page_button,
        ):
            w.setEnabled(enabled)
        if view is not None and not self.zoom_box.hasFocus():
            self.zoom_box.show_zoom(view.zoom)
        elif view is None:
            self.zoom_box.setEditText("")
        self._place()

    def _on_page_changed(self, _page: int) -> None:
        self.navigator.update_state(self.view)
        self.poke()

    def _on_zoom_changed(self, zoom: float) -> None:
        if not self.zoom_box.hasFocus():
            self.zoom_box.show_zoom(zoom)
        self.zoom_out_button.setEnabled(zoom > 0.051)
        self.zoom_in_button.setEnabled(zoom < 63.99)
        self.poke()

    def _zoom(self, method: str) -> None:
        if self.view is not None:
            getattr(self.view, method)()

    def _on_scrolled(self, _value: int) -> None:
        self._place()  # the viewport scroll moved us along with the pages
        self.poke()

    def _place(self) -> None:
        """Bottom centre of the page area; hidden when there's no room or nothing to show."""
        view = self.view
        if view is None:
            return
        area = view.viewport().rect()
        size = self.sizeHint()
        if view.page_count == 0 or area.width() < size.width() + 2 * SHADOW:
            self.hide()
            return
        x = area.left() + (area.width() - size.width()) // 2
        y = area.bottom() + 1 - size.height() - BOTTOM_MARGIN + SHADOW
        if self.geometry() != QRect(x, y, size.width(), size.height()):
            self.setGeometry(x, y, size.width(), size.height())
        if self.isHidden():
            self.raise_()
            self.show()

    # Clicks and drags on the pill's own background stay here instead of reaching the page.
    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()

    # -- fading ---------------------------------------------------------------------------
    @property
    def opacity(self) -> float:
        return self._effect.opacity()

    @property
    def faded(self) -> bool:
        """Faded out (or fading): clicks go through to the page."""
        return self.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def poke(self) -> None:
        """Activity: show the pill and restart the idle countdown."""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._fade_to(1.0)
        self._idle.start(self.idle_ms)

    def _busy(self) -> bool:
        focus = QApplication.focusWidget()
        held = focus is not None and self.isAncestorOf(focus)
        return held or self.underMouse() or self.zoom_box.view().isVisible()

    def _on_idle(self) -> None:
        if self._busy():
            self._idle.start(self.idle_ms)
            return
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._fade_to(0.0)

    def _fade_to(self, value: float) -> None:
        running = self._fade.state() is QAbstractAnimation.State.Running
        if running and self._fade.endValue() == value:
            return
        if not running and self._effect.opacity() == value:
            return  # mouse moves and scroll steps poke a visible pill all the time
        self._fade.stop()
        self._effect.setEnabled(True)
        if not motion.animations_enabled() or not self.isVisible():
            self._effect.setOpacity(value)
            self._on_fade_done()
            return
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(value)
        self._fade.start()

    def _on_fade_done(self) -> None:
        if self._effect.opacity() >= 1.0:
            self._effect.setEnabled(False)

    # -- events ---------------------------------------------------------------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FocusIn:  # one of our controls
            self.poke()
        return False

    def enterEvent(self, event: QEvent) -> None:
        self.poke()
        super().enterEvent(event)  # type: ignore[arg-type]

    def paintEvent(self, event: QPaintEvent) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QRectF(self.rect()).adjusted(SHADOW, SHADOW - 1, -SHADOW, -SHADOW - 1)
        radius = body.height() / 2
        shadow = QColor(colors.shadow)
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, alpha in ((1, 34), (2, 20), (3, 10), (4, 5)):
            shadow.setAlpha(alpha)
            painter.setBrush(shadow)
            r = body.adjusted(-spread, -spread + 1, spread, spread + 1)
            painter.drawRoundedRect(r, radius + spread, radius + spread)
        painter.setBrush(QColor(colors.surface))
        pen = QPen(QColor(colors.border_strong))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.fillRect(self._separator.geometry().adjusted(0, 4, 0, -4), QColor(colors.border))
        painter.end()
