"""Bookmarks (outline): navigate, and edit (add, rename, delete, drag to reorder or nest,
set destination). Every edit is one undoable step."""

from __future__ import annotations

import copy
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import ChangeKind, SetOutlineCommand
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.outline import Destination, Link, LinkKind, OutlineItem
from pdfeditor.ui.icons import icon
from pdfeditor.ui.panels.base import ViewPanel

ITEM_ROLE = Qt.ItemDataRole.UserRole


class _Tree(QTreeWidget):
    """Tree whose drops (reorder / nest) are reported so the outline can be rewritten."""

    def __init__(self, panel: BookmarksPanel) -> None:
        super().__init__(panel)
        self._panel = panel

    def dropEvent(self, event: QDropEvent) -> None:
        super().dropEvent(event)
        self._panel.commit("Move Bookmark")


class BookmarksPanel(ViewPanel):
    title = "Bookmarks"
    rebuild_on = frozenset({ChangeKind.STRUCTURE, ChangeKind.OUTLINE})

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = _Tree(self)
        self.tree.setHeaderHidden(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.tree.itemActivated.connect(self._activate)
        self.tree.itemClicked.connect(self._activate)
        self.tree.itemChanged.connect(self._on_renamed)
        self.empty = QLabel("This document has no bookmarks.", self)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.add_button = self._tool(
            "bookmark-plus", "Add a bookmark for the current view", self.add_bookmark
        )
        self.delete_button = self._tool(
            "trash-2", "Delete the selected bookmark", self.delete_bookmark
        )
        self.dest_button = self._tool(
            "map-pin",
            "Point the selected bookmark at the current view",
            self.set_destination,
        )
        bar = QHBoxLayout()
        bar.setContentsMargins(2, 2, 2, 2)
        for b in (self.add_button, self.delete_button, self.dest_button):
            bar.addWidget(b)
        bar.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.tree)
        layout.addWidget(self.empty)
        self._building = False

    def _tool(self, icon_name: str, tip: str, slot: Callable[[], None]) -> QToolButton:
        b = QToolButton(self)
        b.setIcon(icon(icon_name))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(lambda _=False: slot())
        return b

    # -- display --------------------------------------------------------------------------
    def rebuild(self) -> None:
        self._building = True
        try:
            self.tree.clear()
            items: list[OutlineItem] = []
            if self.view is not None:
                with self.view.session.lock:
                    items = self.view.session.document.outline()
            for item in items:
                self.tree.addTopLevelItem(self._make(item))
            self.tree.setVisible(bool(items))
            self.empty.setVisible(not items)
            editable = self.view is not None and self.view.session.engine.capabilities.outline_write
            for b in (self.add_button, self.delete_button, self.dest_button):
                b.setEnabled(editable)
        finally:
            self._building = False

    def _make(self, item: OutlineItem) -> QTreeWidgetItem:
        node = QTreeWidgetItem([item.title])
        node.setData(0, ITEM_ROLE, item)
        node.setToolTip(0, item.title)
        node.setFlags(
            node.flags()
            | Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
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

    # -- editing --------------------------------------------------------------------------
    def outline_from_tree(self) -> list[OutlineItem]:
        def build(node: QTreeWidgetItem) -> OutlineItem:
            item = copy.copy(node.data(0, ITEM_ROLE))
            item.title = node.text(0)
            item.is_open = node.isExpanded()
            item.children = [build(c) for i in range(node.childCount()) if (c := node.child(i))]
            return item

        count = self.tree.topLevelItemCount()
        return [build(n) for i in range(count) if (n := self.tree.topLevelItem(i))]

    def commit(self, label: str) -> None:
        if self.view is None or self._building:
            return
        self.view.session.execute(SetOutlineCommand(self.outline_from_tree(), label))

    def _current_destination(self) -> Destination | None:
        view = self.view
        if view is None:
            return None
        page = view.current_page
        loc = view.location()
        rect = view.page_rect(page)
        return Destination(page, Point(0, max(0.0, loc.y_fraction * rect.height)))

    def add_bookmark(self, title: str | None = None) -> None:
        """New bookmark after the selected one: named ``title``, else the selected text, else
        the page label."""
        view = self.view
        if view is None:
            return
        if not title:
            selected = view.selected_text().strip()
            title = selected.splitlines()[0][:120] if selected else ""
        if not title:
            title = f"Page {view.page_label(view.current_page)}"
        node = self._make(OutlineItem(title, dest=self._current_destination()))
        current = self.tree.currentItem()
        self._building = True
        try:
            parent = current.parent() if current is not None else None
            if current is not None and parent is not None:
                parent.insertChild(parent.indexOfChild(current) + 1, node)
            elif current is not None:
                self.tree.insertTopLevelItem(self.tree.indexOfTopLevelItem(current) + 1, node)
            else:
                self.tree.addTopLevelItem(node)
        finally:
            self._building = False
        self.commit("Add Bookmark")

    def delete_bookmark(self) -> None:
        node = self.tree.currentItem()
        if node is None:
            return
        self._building = True
        try:
            parent = node.parent()
            if parent is not None:
                parent.removeChild(node)
            else:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(node))
        finally:
            self._building = False
        self.commit("Delete Bookmark")

    def set_destination(self) -> None:
        node = self.tree.currentItem()
        dest = self._current_destination()
        if node is None or dest is None:
            return
        item = copy.copy(node.data(0, ITEM_ROLE))
        item.dest, item.uri = dest, None
        self._building = True
        node.setData(0, ITEM_ROLE, item)
        self._building = False
        self.commit("Set Bookmark Destination")

    def _on_renamed(self, node: QTreeWidgetItem, _column: int) -> None:
        if self._building:
            return
        original: OutlineItem | None = node.data(0, ITEM_ROLE)
        if original is not None and node.text(0) != original.title:
            self.commit("Rename Bookmark")
