"""Toasts: short, non-blocking notifications at the bottom right of the main window.

They replace transient status-bar messages ("Saved report.pdf", "3 pages deleted"). A toast
can offer one action ("Undo", "Show in folder"), dismisses itself after a few seconds (longer
for errors, never while the mouse is over it or it has keyboard focus), and at most
:data:`MAX_TOASTS` are shown, newest at the bottom. Screen readers hear each one through an
accessibility announcement.

Toasts are children of the main window (above the docks), placed over the bottom right of the
central area, just above the status bar. When the window is so narrow that they would cover
the floating page/zoom pill, they move up above it. F6 reaches them from the keyboard (the
last region of ``ui/focus_regions.py``); Escape closes the focused one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import (
    QAbstractAnimation,
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAccessible, QColor, QEnterEvent, QKeyEvent, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.i18n import QT_TRANSLATE_NOOP
from pdfeditor.ui.icons import icon
from pdfeditor.ui.reveal import show_in_folder
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors, theme_manager
from pdfeditor.ui.view import motion

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession

log = logging.getLogger(__name__)

Kind = Literal["info", "success", "error"]
KINDS: tuple[Kind, ...] = ("info", "success", "error")
MAX_TOASTS = 3
WIDTH = 360  # px, narrower when the window is
MARGIN = 16  # px from the central area's edges
GAP = 8  # px between stacked toasts
SHADOW = 12  # px around each toast reserved for its drop shadow
# How long each kind stays (ms); a toast with an action stays longer, so it can be reached.
TIMEOUTS: dict[Kind, int] = {"info": 5000, "success": 5000, "error": 10000}
ACTION_EXTRA_MS = 3000
RESUME_MIN_MS = 1500  # after hover/focus leaves, at least this long before it goes
_ICONS: dict[Kind, str] = {"info": "info", "success": "circle-check", "error": "circle-alert"}
# What screen readers say before the text, and the kind's name (translated when used).
_SPOKEN: dict[Kind, str] = {
    "info": QT_TRANSLATE_NOOP("Toast", "Notification"),
    "success": QT_TRANSLATE_NOOP("Toast", "Done"),
    "error": QT_TRANSLATE_NOOP("Toast", "Error"),
}
_KIND_NAMES: dict[Kind, str] = {
    "info": QT_TRANSLATE_NOOP("Toast", "Information"),
    "success": QT_TRANSLATE_NOOP("Toast", "Success"),
    "error": QT_TRANSLATE_NOOP("Toast", "Error"),
}


@dataclass
class ToastAction:
    """The one button a toast can carry. ``enabled`` is asked again whenever the window's
    state changes (and right before running), so an offer that went stale (Undo after
    another edit) is disabled instead of doing something unexpected."""

    label: str
    run: Callable[[], object]
    enabled: Callable[[], bool] | None = None

    def available(self) -> bool:
        try:
            return self.enabled() if self.enabled is not None else True
        except Exception:  # a stale offer (closed document...) is just unavailable
            log.debug("toast action check failed", exc_info=True)
            return False


def undo_action(session: DocumentSession, label: str | None = None) -> ToastAction:
    """Undo the command just executed on ``session``, as long as it's still the latest one."""
    stack = session.undo_stack
    command, version = stack.top, stack.version

    def latest() -> bool:
        return (
            not session.closed
            and not session.busy
            and command is not None
            and stack.top is command
            and stack.version == version
        )

    def run() -> None:
        if latest():  # verified again: the button may be pressed long after it was offered
            session.undo()

    return ToastAction(label or QCoreApplication.translate("Toast", "Undo"), run, latest)


def folder_action(path: Path, label: str | None = None) -> ToastAction:
    label = label or QCoreApplication.translate("Toast", "Show in folder")
    return ToastAction(label, lambda: show_in_folder(path), lambda: Path(path).exists())


class Toast(QWidget):
    """One notification. ``dismissed`` fires once, when it starts going away."""

    dismissed = Signal()

    def __init__(
        self,
        text: str,
        kind: Kind = "info",
        action: ToastAction | None = None,
        timeout_ms: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if kind not in KINDS:
            kind = "info"
        self.kind: Kind = kind
        self.text = text
        self.action = action
        self._closing = False
        self._hovered = False
        self._focused = False
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAccessibleName(
            self.tr("{kind}: {text}").format(kind=self.tr(_SPOKEN[kind]), text=text)
        )
        kind_name = self.tr(_KIND_NAMES[kind])
        self.setAccessibleDescription(
            self.tr("{kind} notification with “{action}” button").format(
                kind=kind_name, action=action.label
            )
            if action
            else self.tr("{kind} notification").format(kind=kind_name)
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW // 2, SHADOW, SHADOW + SHADOW // 2)
        self.frame = QFrame(self)
        self.frame.setObjectName("ToastFrame")
        self.frame.setProperty("kind", kind)
        if theme_manager().high_contrast_applied:  # no style sheet: an opaque, framed box
            self.frame.setFrameShape(QFrame.Shape.Box)
            self.frame.setLineWidth(2)
            self.frame.setBackgroundRole(QPalette.ColorRole.Base)
            self.frame.setAutoFillBackground(True)
        outer.addWidget(self.frame)
        shadow = QGraphicsDropShadowEffect(self.frame)
        shadow.setBlurRadius(SHADOW * 2)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(0, 0, 0, 70 if current_colors().scheme == "light" else 150))
        self.frame.setGraphicsEffect(shadow)

        m = METRICS
        row = QHBoxLayout(self.frame)
        row.setContentsMargins(m.space(3), m.space(2), m.space(1), m.space(2))
        row.setSpacing(m.space(2))
        colors = current_colors()
        ink = {"info": colors.accent_text, "success": colors.success, "error": colors.danger}[kind]
        self.icon_label = QLabel(self.frame)
        self.icon_label.setPixmap(icon(_ICONS[kind], ink).pixmap(18, 18))
        self.icon_label.setAccessibleName(kind_name)
        row.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel(text, self.frame)
        self.label.setObjectName("ToastText")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.label, 1)
        self.action_button: QPushButton | None = None
        if action is not None:
            self.action_button = QPushButton(action.label, self.frame)
            self.action_button.setObjectName("ToastAction")
            self.action_button.setAccessibleName(action.label)
            self.action_button.setAutoDefault(False)
            self.action_button.clicked.connect(self._run_action)
            row.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.close_button = QToolButton(self.frame)
        self.close_button.setObjectName("ToastClose")
        self.close_button.setIcon(icon("x"))
        self.close_button.setAutoRaise(True)
        self.close_button.setToolTip(self.tr("Dismiss (Esc)"))
        self.close_button.setAccessibleName(self.tr("Dismiss notification"))
        self.close_button.clicked.connect(self.dismiss)
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        for child in (self.action_button, self.close_button):
            if child is not None:
                child.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                child.installEventFilter(self)

        if timeout_ms is None:
            timeout_ms = TIMEOUTS[kind] + (ACTION_EXTRA_MS if action is not None else 0)
        self.timeout_ms = timeout_ms
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._remaining = timeout_ms
        self._fade: QPropertyAnimation | None = None
        self.refresh()

    # -- life cycle -----------------------------------------------------------------------
    def present(self) -> None:
        """Show it, start the clock and announce it to screen readers."""
        self.show()
        self.raise_()
        if motion.animations_enabled():
            self._animate(0.0, 1.0)
        if self.timeout_ms > 0:
            self._timer.start(self.timeout_ms)
        announce(self, self.accessibleName(), assertive=self.kind == "error")

    def dismiss(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self.dismissed.emit()
        if motion.animations_enabled() and self.isVisible():
            anim = self._animate(1.0, 0.0)
            anim.finished.connect(self.close)
        else:
            self.close()

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def paused(self) -> bool:
        return not self._timer.isActive() and not self._closing and self.timeout_ms > 0

    def refresh(self) -> None:
        if self.action is not None and self.action_button is not None:
            self.action_button.setEnabled(self.action.available())

    def _run_action(self) -> None:
        action = self.action
        if action is None:
            return
        if not action.available():
            self.refresh()
            return
        try:
            action.run()
        finally:
            self.dismiss()

    def _animate(self, start: float, end: float) -> QPropertyAnimation:
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(start)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(motion.FADE_MS)
        anim.setStartValue(start)
        anim.setEndValue(end)
        if end >= 1.0:  # the effect costs a repaint per frame; drop it once fully shown
            anim.finished.connect(lambda: self.setGraphicsEffect(None))  # type: ignore[arg-type]
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade = anim
        return anim

    # -- pause while the user is with it --------------------------------------------------
    def _pause(self) -> None:
        if self._timer.isActive():
            self._remaining = max(self._timer.remainingTime(), 0)
            self._timer.stop()

    def _resume(self) -> None:
        if self._closing or self._hovered or self._focused or self.timeout_ms <= 0:
            return
        if not self._timer.isActive():
            self._timer.start(max(self._remaining, RESUME_MIN_MS))

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hovered = True
        self._pause()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        self._resume()
        super().leaveEvent(event)

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        if focused:
            self._pause()
        else:
            self._resume()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        ):
            self.dismiss()
            return True
        return False


def announce(widget: QWidget, text: str, assertive: bool = False) -> None:
    """Ask screen readers to read ``text`` now (Qt ≥ 6.8 announcement events)."""
    try:
        from PySide6.QtGui import QAccessibleAnnouncementEvent
    except ImportError:  # older Qt: the accessible name still describes the toast
        return
    if not QAccessible.isActive():
        return
    event = QAccessibleAnnouncementEvent(widget, text)
    politeness = QAccessible.AnnouncementPoliteness
    event.setPoliteness(politeness.Assertive if assertive else politeness.Polite)
    QAccessible.updateAccessibility(event)


class ToastHost(QObject):
    """Shows, stacks and places the toasts of one main window."""

    changed = Signal()

    def __init__(self, window: QMainWindow, avoid: Callable[[], QWidget | None] | None = None):
        super().__init__(window)
        self.main_window = window
        self._avoid = avoid  # the widget toasts mustn't cover (the page/zoom pill)
        self._toasts: list[Toast] = []
        self._last: tuple[str, Kind] = ("", "info")
        self._watched: list[QWidget] = [window]
        central = window.centralWidget()
        if central is not None:  # docks resizing move its edges without resizing the window
            self._watched.append(central)
        for widget in self._watched:
            widget.installEventFilter(self)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.focusChanged.connect(self._focus_changed)

    # -- API ------------------------------------------------------------------------------
    def show(
        self,
        text: str,
        kind: Kind = "info",
        action: ToastAction | None = None,
        timeout_ms: int | None = None,
    ) -> Toast:
        log.info("toast (%s): %s", kind, text)
        toast = Toast(text, kind, action, timeout_ms, self.main_window)
        toast.dismissed.connect(lambda: self._forget(toast))
        toast.destroyed.connect(lambda _=None: self._forget(toast))
        self._toasts.append(toast)
        self._last = (text, kind)
        while len(self._toasts) > MAX_TOASTS:
            oldest = self._toasts.pop(0)
            oldest.dismiss()
        self.place()
        toast.present()
        self.place()
        self.changed.emit()
        return toast

    def toasts(self) -> list[Toast]:
        """Visible toasts, oldest first (ones already fading out are left out)."""
        return list(self._toasts)

    def last_text(self) -> str:
        """The most recent toast's text, even after it went away (tests, the self-test)."""
        return self._last[0]

    def last_kind(self) -> Kind:
        return self._last[1]

    def clear(self) -> None:
        for toast in list(self._toasts):
            toast.dismiss()

    def refresh(self) -> None:
        """Re-check every action's availability (call when the window's state changed)."""
        for toast in self._toasts:
            toast.refresh()

    # -- placement ------------------------------------------------------------------------
    def area(self) -> QRect:
        """The window rect toasts stack in: the central area, minus margins, ending above
        the status bar (the central widget ends there) and above the pill if it'd overlap."""
        central = self.main_window.centralWidget()
        rect = central.geometry() if central is not None else self.main_window.rect()
        return rect.adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)

    def place(self) -> None:
        area = self.area()
        width = min(WIDTH + 2 * SHADOW, max(area.width() + 2 * SHADOW, 160))
        right = area.right() + SHADOW + 1
        bottom = area.bottom() + SHADOW + SHADOW // 2 + 1
        avoid = self._avoid() if self._avoid is not None else None
        if avoid is not None and avoid.isVisible():
            top_left = avoid.mapTo(self.main_window, QPoint(0, 0))
            pill = QRect(top_left, avoid.size())
            column = QRect(right - width, area.top(), width, area.height())
            if column.adjusted(SHADOW, 0, -SHADOW, 0).intersects(pill):
                bottom = min(bottom, pill.top() - GAP + SHADOW + SHADOW // 2)
        for toast in reversed(self._toasts):  # newest at the bottom
            height = toast.heightForWidth(width)
            if height <= 0:
                height = toast.sizeHint().height()
            toast.setGeometry(right - width, bottom - height, width, height)
            toast.raise_()
            bottom -= height - 2 * SHADOW + GAP

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            self._toasts
            and watched in self._watched
            and event.type() in (QEvent.Type.Resize, QEvent.Type.Move)
        ):
            QTimer.singleShot(0, self.place)
        return False

    # -- bookkeeping ----------------------------------------------------------------------
    def _forget(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
            self.place()
            self.changed.emit()

    def _focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        for toast in self._toasts:
            toast.set_focused(new is not None and toast.isAncestorOf(new))
