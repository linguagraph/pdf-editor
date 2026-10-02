"""The ribbon: a tab bar that switches between rows of labelled button groups.

Each tab is a toolbar whose items are whole groups (a row of buttons with a caption under it),
so when the window is too narrow Qt's toolbar overflow ("»") moves complete groups out of
sight rather than single buttons. Controllers add their own tabs and groups with ``add_tab`` and
``add_group``.

The ribbon can be collapsed to just its tab row (double-click a tab, or the chevron), and can
show icons only ("compact"), for small screens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QElapsedTimer, QSize, Qt, Signal
from PySide6.QtGui import QAction, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.icons import icon

if TYPE_CHECKING:
    from pdfeditor.ui.command_search import CommandSearch

ICON_SIZE = QSize(22, 22)
COMPACT_ICON_SIZE = QSize(18, 18)
# Below this ribbon width the "Search tools" box becomes a button, so the tabs keep their room.
SEARCH_BOX_MIN_WIDTH = 1000


class RibbonGroup(QWidget):
    """A row of buttons with a caption under it."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("RibbonGroup")
        # Fixed: the tab's toolbar then moves whole groups to its "»" overflow menu instead of
        # squeezing each group's own toolbar.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.bar = QToolBar(title, self)
        self.bar.setIconSize(ICON_SIZE)
        self.bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.label = QLabel(title, self)
        self.label.setProperty("role", "caption")
        self.label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.label.setVisible(bool(title))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 2)
        layout.setSpacing(0)
        layout.addWidget(self.bar)
        layout.addWidget(self.label)

    def set_compact(self, compact: bool) -> None:
        self.bar.setIconSize(COMPACT_ICON_SIZE if compact else ICON_SIZE)
        self.bar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonIconOnly
            if compact
            else Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        )
        self.label.setVisible(bool(self.label.text()) and not compact)
        self.bar.adjustSize()
        self.adjustSize()  # the fixed size policy pins the old size until recomputed
        self.updateGeometry()


class RibbonTab(QToolBar):
    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(name, parent)
        self.setMovable(False)
        self.setFloatable(False)
        self.groups: list[RibbonGroup] = []
        self._compact = False
        # Qt's overflow button is a tiny arrow; make it a findable "more" button.
        more = self.findChild(QToolButton, "qt_toolbar_ext_button")
        if more is not None:
            more.setIcon(icon("chevrons-down"))
            more.setToolTip("More commands")
            more.setAccessibleName("More commands")
            more.setMinimumWidth(24)

    def add_group(self, *actions: QAction | QWidget | None, title: str = "") -> RibbonGroup:
        """Add actions/widgets as one captioned group; ``None`` entries are skipped."""
        if self.groups:
            self.addSeparator()
        group = RibbonGroup(title, self)
        for entry in actions:
            if entry is None:
                continue
            if isinstance(entry, QAction):
                group.bar.addAction(entry)
            else:
                group.bar.addWidget(entry)
        group.set_compact(self._compact)
        self.groups.append(group)
        self.addWidget(group)
        return group

    def button_actions(self) -> list[QAction]:
        """The actions shown as buttons in this tab's groups."""
        return [
            a
            for g in self.groups
            for a in g.bar.actions()
            if not a.isSeparator() and isinstance(g.bar.widgetForAction(a), QToolButton)
        ]

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        for group in self.groups:
            group.set_compact(compact)
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
        self.adjustSize()


class Ribbon(QWidget):
    collapsed_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.collapsed = False
        self.compact = False
        self.setObjectName("Ribbon")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.bar = QTabBar(self)
        self.bar.setAccessibleName("Ribbon tabs")
        self.bar.setDrawBase(False)
        self.bar.setExpanding(False)
        self.stack = QStackedWidget(self)
        self.stack.setObjectName("RibbonPanel")
        self.menu_button = QToolButton(self)
        self.menu_button.setIcon(icon("menu"))
        self.menu_button.setToolTip("Menu")
        self.menu_button.setAccessibleName("Menu")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setAutoRaise(True)
        self.quick = QToolBar(self)
        self.quick.setIconSize(QSize(16, 16))
        self.quick.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.quick.setMovable(False)
        self.collapse_button = QToolButton(self)
        self.collapse_button.setAutoRaise(True)
        self.collapse_button.setAccessibleName("Collapse ribbon")
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(not self.collapsed))
        self.search: CommandSearch | None = None
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        top.addWidget(self.menu_button)
        top.addWidget(self.quick)
        top.addWidget(self.bar, 1)
        top.addWidget(self.collapse_button)
        self._top = top
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(self.stack)
        self.bar.currentChanged.connect(self.stack.setCurrentIndex)
        self.bar.tabBarClicked.connect(self._tab_clicked)
        self.bar.tabBarDoubleClicked.connect(self._tab_double_clicked)
        self._expanded_by_click = QElapsedTimer()  # see _tab_double_clicked
        self._tabs: dict[str, RibbonTab] = {}
        self._update_collapse_button()

    def set_quick_actions(self, actions: list[QAction]) -> None:
        """Always-visible actions left of the tabs (Select, Hand, Undo, ...)."""
        self.quick.clear()
        for action in actions:
            self.quick.addAction(action)
            button = self.quick.widgetForAction(action)
            if isinstance(button, QToolButton) and action.icon().isNull():
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

    def set_search(self, search: CommandSearch) -> None:
        """Put the "Search tools" box at the right of the tab row, before the chevron."""
        self.search = search
        search.setParent(self)
        self._top.insertWidget(self._top.indexOf(self.collapse_button), search)
        self._update_search()

    def _update_search(self) -> None:
        if self.search is not None:
            self.search.set_collapsed(self.compact or self.width() < SEARCH_BOX_MIN_WIDTH)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_search()

    def add_tab(self, name: str) -> RibbonTab:
        tab = RibbonTab(name, self)
        tab.set_compact(self.compact)
        self._tabs[name] = tab
        self.stack.addWidget(tab)
        self.bar.addTab(name)
        return tab

    def tab(self, name: str) -> RibbonTab:
        return self._tabs[name]

    def tab_names(self) -> list[str]:
        return list(self._tabs)

    def button_actions(self) -> list[QAction]:
        """Every action shown as a button: the quick bar and all tabs (no separators/widgets)."""
        quick = [
            a
            for a in self.quick.actions()
            if not a.isSeparator() and isinstance(self.quick.widgetForAction(a), QToolButton)
        ]
        return quick + [a for tab in self._tabs.values() for a in tab.button_actions()]

    # -- size modes ---------------------------------------------------------------------------
    def set_collapsed(self, collapsed: bool) -> None:
        """Show only the tab row; clicking a tab brings the buttons back."""
        if collapsed == self.collapsed:
            return
        self.collapsed = collapsed
        self.stack.setVisible(not collapsed)
        self._update_collapse_button()
        self.collapsed_changed.emit(collapsed)

    def set_compact(self, compact: bool) -> None:
        """Icons only, without captions: a much shorter ribbon for small screens."""
        self.compact = compact
        for tab in self._tabs.values():
            tab.set_compact(compact)
        self._update_search()
        # The stacked pages' hints change, but layouts above them keep cached sizes.
        for widget in (self.stack, self):
            layout = widget.layout()
            if layout is not None:
                layout.invalidate()
            widget.updateGeometry()

    def _tab_clicked(self, _index: int) -> None:
        if self.collapsed:
            self.set_collapsed(False)
            self._expanded_by_click.start()

    def _tab_double_clicked(self, _index: int) -> None:
        # The double-click's first click already expanded a collapsed ribbon: keep it open.
        timer = self._expanded_by_click
        if timer.isValid() and timer.elapsed() <= QApplication.doubleClickInterval():
            timer.invalidate()
            return
        self.set_collapsed(not self.collapsed)

    def _update_collapse_button(self) -> None:
        if self.collapsed:
            self.collapse_button.setIcon(icon("chevron-down"))
            self.collapse_button.setToolTip("Show the ribbon (Ctrl+F1)")
        else:
            self.collapse_button.setIcon(icon("chevron-up"))
            self.collapse_button.setToolTip("Collapse the ribbon to its tabs (Ctrl+F1)")
