"""A compact ribbon: a tab bar that switches between tool rows.

Later phases add their own tabs (Comment, Edit, Organize, Protect, Tools) with ``add_tab``.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QTabBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class RibbonTab(QToolBar):
    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(name, parent)
        self.setIconSize(QSize(22, 22))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setMovable(False)
        self.setFloatable(False)

    def add_group(self, *actions: QAction | QWidget | None) -> None:
        """Add actions/widgets as one group; ``None`` entries are skipped."""
        if self.actions():
            self.addSeparator()
        for entry in actions:
            if entry is None:
                continue
            if isinstance(entry, QAction):
                self.addAction(entry)
            else:
                self.addWidget(entry)


class Ribbon(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bar = QTabBar(self)
        self.bar.setAccessibleName("Ribbon tabs")
        self.bar.setDrawBase(False)
        self.bar.setExpanding(False)
        self.stack = QStackedWidget(self)
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 0)
        layout.setSpacing(0)
        self.quick = QToolBar(self)
        self.quick.setIconSize(QSize(16, 16))
        self.quick.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.quick.setMovable(False)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.quick)
        top.addWidget(self.bar, 1)
        layout.addLayout(top)
        layout.addWidget(self.stack)
        layout.addWidget(line)
        self.bar.currentChanged.connect(self.stack.setCurrentIndex)
        self._tabs: dict[str, RibbonTab] = {}

    def set_quick_actions(self, actions: list[QAction]) -> None:
        """Always-visible actions left of the tabs (Select, Hand, Undo, ...)."""
        self.quick.clear()
        for action in actions:
            self.quick.addAction(action)
            button = self.quick.widgetForAction(action)
            if isinstance(button, QToolButton) and action.icon().isNull():
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

    def add_tab(self, name: str) -> RibbonTab:
        tab = RibbonTab(name, self)
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
        bars: list[QToolBar] = [self.quick, *self._tabs.values()]
        return [
            a
            for bar in bars
            for a in bar.actions()
            if not a.isSeparator() and isinstance(bar.widgetForAction(a), QToolButton)
        ]
