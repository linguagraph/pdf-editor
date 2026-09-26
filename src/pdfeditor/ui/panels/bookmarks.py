"""Bookmarks (outline) tree; click to navigate."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from pdfeditor.core.commands import ChangeKind
from pdfeditor.model.geometry import Rect
from pdfeditor.model.outline import Link, LinkKind, OutlineItem
from pdfeditor.ui.panels.base import ViewPanel

ITEM_ROLE = Qt.ItemDataRole.UserRole


class BookmarksPanel(ViewPanel):
    title = "Bookmarks"
    rebuild_on = frozenset({ChangeKind.STRUCTURE, ChangeKind.OUTLINE})

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.itemActivated.connect(self._activate)
        self.tree.itemClicked.connect(self._activate)
        self.empty = QLabel("This document has no bookmarks.", self)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)
        layout.addWidget(self.empty)

    def rebuild(self) -> None:
        self.tree.clear()
        items: list[OutlineItem] = []
        if self.view is not None:
            with self.view.session.lock:
                items = self.view.session.document.outline()
        for item in items:
            self.tree.addTopLevelItem(self._make(item))
        self.tree.setVisible(bool(items))
        self.empty.setVisible(not items)

    def _make(self, item: OutlineItem) -> QTreeWidgetItem:
        node = QTreeWidgetItem([item.title])
        node.setData(0, ITEM_ROLE, item)
        node.setToolTip(0, item.title)
        if item.bold or item.italic:
            font = QFont()
            font.setBold(item.bold)
            font.setItalic(item.italic)
            node.setFont(0, font)
        if item.color is not None and item.color.rgb() != (0.0, 0.0, 0.0):
            node.setForeground(0, QBrush(QColor.fromRgbF(*item.color.rgb())))
        for child in item.children:
            node.addChild(self._make(child))
        node.setExpanded(item.is_open)
        return node

    def _activate(self, node: QTreeWidgetItem, _column: int = 0) -> None:
        item: OutlineItem | None = node.data(0, ITEM_ROLE)
        if item is None or self.view is None:
            return
        if item.dest is not None:
            self.view.go_to_page(item.dest.page_index, item.dest.point)
        elif item.uri:
            self.view.link_activated.emit(Link(Rect(0, 0, 0, 0), LinkKind.URI, uri=item.uri))
