"""Side panels behind an icon rail (Pages, Bookmarks, Comments... on the left, Properties on
the right).

Each side is a dock holding a narrow rail of icon buttons and, next to it, the open panel.
Clicking an icon opens its panel; clicking the active icon again collapses the panel so only
the rail stays. The dock is then pinned to the rail's width, and its old width comes back when
a panel opens again. Which panel is open and how wide it is are kept in the settings.

Rail icons can carry a badge with a count the panel already knows (comments, search hits,
accessibility problems), see ``ViewPanel.badge_count``; screen readers hear it in the button's
name ("Comments, 6 items").

Keyboard: Up/Down move between rail icons, Space opens one, Escape anywhere in the side panels
returns to the page (``escape_pressed``), and F6 reaches them (see ``ui/focus_regions.py``).
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QCoreApplication, QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.focus_regions import first_focusable, focusable
from pdfeditor.ui.icons import icon
from pdfeditor.ui.panels.base import EmptyState, ViewPanel
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.theme import current_colors

Side = Literal["left", "right"]
QWIDGETSIZE_MAX = 16777215
RAIL_BUTTON = 36
RAIL_MARGIN = 4
MIN_PANEL_WIDTH = 160


class RailButton(QToolButton):
    """A rail icon: checked while its panel is open, with an optional count badge."""

    def __init__(self, panel: ViewPanel, icon_name: str, side: Side, parent: QWidget) -> None:
        super().__init__(parent)
        self.panel = panel
        self.side = side
        self.count = 0
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setIcon(icon(icon_name))
        self.setIconSize(QSize(20, 20))
        self.setFixedSize(RAIL_BUTTON, RAIL_BUTTON)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._update_label()

    def set_count(self, count: int) -> None:
        if count != self.count:
            self.count = count
            self._update_label()
            self.update()

    def _update_label(self) -> None:
        title = QCoreApplication.translate("ViewPanel", self.panel.title)
        tr = QCoreApplication.translate
        if self.count <= 0:
            name = tip = title
        elif self.count == 1:
            name = tr("RailButton", "{panel}, 1 item").format(panel=title)
            tip = tr("RailButton", "{panel} (1)").format(panel=title)
        else:
            name = tr("RailButton", "{panel}, {count} items").format(panel=title, count=self.count)
            tip = tr("RailButton", "{panel} ({count})").format(panel=title, count=self.count)
        self.setAccessibleName(name)
        self.setToolTip(tip)

    def badge_text(self) -> str:
        return "" if self.count <= 0 else "99+" if self.count > 99 else str(self.count)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.isChecked():  # accent bar on the outer edge, as in Windows 11 navigation
            bar_h = 16.0
            x = 1.0 if self.side == "left" else self.width() - 4.0
            painter.setBrush(QColor(colors.accent_text))
            painter.drawRoundedRect(QRectF(x, (self.height() - bar_h) / 2, 3, bar_h), 1.5, 1.5)
        text = self.badge_text()
        if text:
            font = QFont(self.font())
            font.setPointSizeF(max(6.5, font.pointSizeF() * 0.75))
            font.setBold(True)
            h = 15.0
            w = max(h, QFontMetrics(font).horizontalAdvance(text) + 8.0)
            rect = QRectF(self.width() - w - 2, 2, w, h)
            painter.setBrush(QColor(colors.accent))
            painter.drawRoundedRect(rect, h / 2, h / 2)
            painter.setPen(QColor(colors.on_accent))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(Qt.Key(event.key()))
        rail = self.parentWidget()
        if step is not None and isinstance(rail, PanelRail):
            buttons = [b for b in rail.buttons() if b.isVisible()]
            if self in buttons:
                buttons[(buttons.index(self) + step) % len(buttons)].setFocus(
                    Qt.FocusReason.TabFocusReason
                )
                return
        super().keyPressEvent(event)


class PanelRail(QWidget):
    def __init__(self, side: Side, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PanelRail")
        self.setProperty("side", side)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAccessibleName(
            self.tr("Left panels") if side == "left" else self.tr("Right panels")
        )
        self._layout = QVBoxLayout(self)
        m = RAIL_MARGIN
        self._layout.setContentsMargins(m, m, m, m)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)
        self.setFixedWidth(RAIL_BUTTON + 2 * m + 1)  # + the 1 px border towards the panel

    def buttons(self) -> list[RailButton]:
        items = (self._layout.itemAt(i) for i in range(self._layout.count()))
        widgets = (item.widget() for item in items if item is not None)
        return [w for w in widgets if isinstance(w, RailButton)]

    def insert(self, index: int, button: RailButton) -> None:
        count = len(self.buttons())
        self._layout.insertWidget(count if index < 0 or index > count else index, button)


class SidePanels(QWidget):
    """A rail plus the open panel (with a title header) for one side of the window."""

    current_changed = Signal(object)  # the open ViewPanel, or None when collapsed
    escape_pressed = Signal()  # Escape in the rail or a panel: back to the page

    def __init__(
        self,
        side: Side,
        prefs: AppSettings | None = None,
        default_open: str = "",
        default_width: int = 240,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.side: Side = side
        self.prefs = prefs
        self.default_open = default_open
        self.panel_width = default_width
        self.rail = PanelRail(side, self)
        self._buttons: dict[ViewPanel, RailButton] = {}
        self._keys: dict[ViewPanel, str] = {}
        self._current: ViewPanel | None = None

        self.content = QWidget(self)
        self.content.setObjectName("PanelContent")
        self.content.setMinimumWidth(MIN_PANEL_WIDTH)
        self.title = QLabel(self.content)
        self.title.setProperty("role", "panel-title")
        self.close_button = QToolButton(self.content)
        self.close_button.setIcon(icon("x"))
        self.close_button.setAutoRaise(True)
        self.close_button.setToolTip(self.tr("Close panel"))
        self.close_button.setAccessibleName(self.tr("Close panel"))
        self.close_button.clicked.connect(self.collapse)
        header = QHBoxLayout()
        header.setContentsMargins(8, 4, 4, 0)
        header.addWidget(self.title, 1)
        header.addWidget(self.close_button)
        self.stack = QStackedWidget(self.content)
        self.stack.setAccessibleName(self.tr("Panel"))
        self.no_document = EmptyState(
            "folder-open",
            self.tr("No document open"),
            self.tr("Open a PDF to see its pages, bookmarks, comments and more here."),
            self.content,
        )
        self.no_document.hide()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(2)
        content_layout.addLayout(header)
        content_layout.addWidget(self.stack, 1)
        content_layout.addWidget(self.no_document, 1)
        self.content.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        if side == "left":
            layout.addWidget(self.rail)
            layout.addWidget(self.content, 1)
        else:
            layout.addWidget(self.content, 1)
            layout.addWidget(self.rail)

    # -- panels ---------------------------------------------------------------------------
    def add_panel(self, panel: ViewPanel, key: str, icon_name: str, index: int = -1) -> None:
        button = RailButton(panel, icon_name, self.side, self.rail)
        button.clicked.connect(lambda _=False, p=panel: self.toggle(p))
        panel.badge_changed.connect(lambda b=button, p=panel: b.set_count(p.badge_count()))
        self.rail.insert(index, button)
        self.stack.addWidget(panel)
        self._buttons[panel] = button
        self._keys[panel] = key
        button.set_count(panel.badge_count())

    def panels(self) -> list[ViewPanel]:
        return [b.panel for b in self.rail.buttons()]

    def button(self, panel: ViewPanel) -> RailButton:
        return self._buttons[panel]

    def key(self, panel: ViewPanel) -> str:
        return self._keys[panel]

    def current(self) -> ViewPanel | None:
        """The open panel; None while collapsed to the rail."""
        return self._current

    def is_open(self) -> bool:
        return self._current is not None

    def open(self, panel: ViewPanel) -> None:
        if panel is self._current:
            return
        self._current = panel
        self.stack.setCurrentWidget(panel)
        self.title.setText(QCoreApplication.translate("ViewPanel", panel.title))
        self.stack.setAccessibleName(self.title.text())
        self._sync()

    def collapse(self) -> None:
        if self._current is None:
            return
        focus = QApplication.focusWidget()
        if focus is not None and self.content.isAncestorOf(focus):
            # Keep keyboard focus nearby rather than letting it jump to the next rail icon.
            self._buttons[self._current].setFocus(Qt.FocusReason.OtherFocusReason)
        self.remember_width()
        self._current = None
        self._sync()

    def toggle(self, panel: ViewPanel) -> None:
        if panel is self._current:
            self.collapse()
        else:
            self.open(panel)

    def focus_target(self) -> QWidget | None:
        """Where F6 puts the keyboard: into the open panel, else on the rail."""
        if self._current is not None and self.content.isVisible():
            root = self._current if self.stack.isVisible() else self.no_document
            target = first_focusable(root)
            if target is not None:
                return target
            if focusable(self.close_button):
                return self.close_button
        buttons = [b for b in self.rail.buttons() if focusable(b)]
        checked = [b for b in buttons if b.isChecked()]
        return next(iter(checked or buttons), None)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and not event.modifiers():
            self.escape_pressed.emit()
            return
        super().keyPressEvent(event)

    def set_has_document(self, has_document: bool) -> None:
        self.stack.setVisible(has_document)
        self.no_document.setVisible(not has_document)

    def _sync(self) -> None:
        for panel, button in self._buttons.items():
            button.setChecked(panel is self._current)
        self.content.setVisible(self._current is not None)
        self.save()
        self.current_changed.emit(self._current)

    # -- size and settings ----------------------------------------------------------------
    def remember_width(self) -> None:
        """Keep the panel's current width (call while it is shown at the user's size)."""
        if self.content.isVisible() and self.window().isVisible():
            self.panel_width = max(MIN_PANEL_WIDTH, self.content.width())

    def save(self) -> None:
        if self.prefs is None:
            return
        self.prefs.set_side_panel(self.side, self._keys[self._current] if self._current else "")
        self.prefs.set_side_panel_width(self.side, self.panel_width)

    def restore(self) -> None:
        if self.prefs is None:
            return
        self.panel_width = self.prefs.side_panel_width(self.side, self.panel_width)
        key = self.prefs.side_panel(self.side, self.default_open)
        panel = next((p for p, k in self._keys.items() if k == key), None)
        if panel is not None:
            self.open(panel)
        else:
            self.collapse()


class PanelDock(QDockWidget):
    """A title-less dock around ``SidePanels`` that shrinks to the rail when collapsed."""

    def __init__(self, title: str, name: str, panels: SidePanels, window: QMainWindow) -> None:
        super().__init__(title, window)
        self.setObjectName(name)
        self.panels = panels
        self.setWidget(panels)
        self.setTitleBarWidget(QWidget(self))  # the rail replaces the title bar and tabs
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self._sized_open = False  # the dock currently has the open panel's width
        panels.current_changed.connect(lambda _p: self.fit())
        self.fit()

    def fit(self, force: bool = False) -> None:
        """Size the dock to the rail alone, or to the rail plus the panel's remembered width.

        Only the change from collapsed to open (or ``force``) resizes an open dock, so
        switching panels keeps the width the user dragged it to."""
        window = self.parentWidget()
        if isinstance(window, QMainWindow):
            fit_docks(window, [self], force)

    def _limit(self) -> bool:
        """Pin a collapsed dock to the rail; True if the dock is open and needs sizing."""
        if not self.panels.is_open():
            self.panels.setMaximumWidth(self.panels.rail.width())
            self._sized_open = False
            return False
        self.panels.setMaximumWidth(QWIDGETSIZE_MAX)
        return True


def fit_docks(window: QMainWindow, docks: list[PanelDock], force: bool = False) -> None:
    """Fit several panel docks in one go (see ``PanelDock.fit``).

    One ``resizeDocks`` call for all of them: sizing them one after the other before Qt's next
    layout pass would squeeze the docks sized earlier to their minimum width."""
    grow = [d for d in docks if d._limit() and (force or not d._sized_open)]
    if not grow or not window.isVisible():
        return
    widths = [d.panels.rail.width() + d.panels.panel_width for d in grow]
    window.resizeDocks(list(grow), widths, Qt.Orientation.Horizontal)
    for d in grow:
        d._sized_open = True
    # Lay out now, so a change to another dock in the same step starts from these widths.
    QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
