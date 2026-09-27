"""Accessibility checker results and the Tags (structure tree) editor."""

from __future__ import annotations

import copy
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import (
    ChangeKind,
    ReorderStructCommand,
    SetAccessibilityCommand,
    SetMetadataCommand,
    SetStructElementCommand,
)
from pdfeditor.model.structure import STANDARD_TYPES, StructNode, walk_all
from pdfeditor.services.accessibility import Finding, Status, check
from pdfeditor.ui.panels.base import ViewPanel

COMMON_LANGUAGES = ("en-US", "en-GB", "bg-BG", "de-DE", "fr-FR", "es-ES", "it-IT", "ru-RU")
_STATUS_LABEL = {Status.FAILED: "✗", Status.WARNING: "!", Status.PASSED: "✓"}


class AccessibilityPanel(ViewPanel):
    title = "Accessibility"
    rebuild_on = frozenset({ChangeKind.STRUCTURE})
    tag_requested = Signal(int)  # ask the Tags panel to show a structure element

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.findings: list[Finding] = []
        self.summary = QLabel("Run the check to find accessibility problems.", self)
        self.summary.setWordWrap(True)
        self.run_button = QPushButton("Run Check", self)
        self.run_button.clicked.connect(lambda: self.run())
        self.fix_button = QPushButton("Fix…", self)
        self.fix_button.clicked.connect(lambda: self.fix_selected())
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self._jump(item))
        buttons = QHBoxLayout()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.fix_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addLayout(buttons)
        layout.addWidget(self.tree, 1)
        self._selection_changed()

    def rebuild(self) -> None:
        self.findings = []
        self.tree.clear()
        self.summary.setText(
            "Run the check to find accessibility problems." if self.view else "No document."
        )
        self.run_button.setEnabled(self.view is not None)
        self._selection_changed()

    def run(self) -> list[Finding]:
        if self.view is None:
            return []
        session = self.view.session
        with session.lock:
            self.findings = check(session.document)
        self._show()
        return self.findings

    def _show(self) -> None:
        self.tree.clear()
        groups: dict[Status, QTreeWidgetItem] = {}
        for status, label in (
            (Status.FAILED, "Failed"),
            (Status.WARNING, "Needs review"),
            (Status.PASSED, "Passed"),
        ):
            n = sum(1 for f in self.findings if f.status is status)
            if n:
                groups[status] = QTreeWidgetItem(self.tree, [f"{label} ({n})"])
        for i, f in enumerate(self.findings):
            where = f" — page {f.page_index + 1}" if f.page_index is not None else ""
            item = QTreeWidgetItem(
                groups[f.status], [f"{_STATUS_LABEL[f.status]} {f.title}{where}"]
            )
            item.setToolTip(0, f.detail)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
        for status in (Status.FAILED, Status.WARNING):
            if status in groups:
                groups[status].setExpanded(True)
        failed = sum(1 for f in self.findings if f.status is Status.FAILED)
        review = sum(1 for f in self.findings if f.status is Status.WARNING)
        self.summary.setText(
            "No problems found."
            if not failed and not review
            else f"{failed} failed, {review} to review. Double-click to go there; "
            "select a fixable item and press Fix."
        )
        self._selection_changed()

    def selected(self) -> Finding | None:
        items = self.tree.selectedItems()
        if not items or items[0].data(0, Qt.ItemDataRole.UserRole) is None:
            return None
        return self.findings[int(items[0].data(0, Qt.ItemDataRole.UserRole))]

    def _selection_changed(self) -> None:
        f = self.selected()
        self.fix_button.setEnabled(f is not None and f.fixable and f.status is not Status.PASSED)

    def _jump(self, item: QTreeWidgetItem) -> None:
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if index is None or self.view is None:
            return
        f = self.findings[int(index)]
        if f.page_index is not None:
            self.view.go_to_page(f.page_index)
        if f.ref is not None:
            self.tag_requested.emit(f.ref)

    def fix_selected(self, value: str | None = None) -> bool:
        """Fix the selected finding; ``value`` answers the question it would ask (tests)."""
        f = self.selected()
        if f is None or self.view is None or not f.fixable:
            return False
        session = self.view.session
        with session.lock:
            settings = session.document.accessibility_settings()
            meta = session.document.metadata()
        if f.rule == "language":
            if value is None:
                value, ok = QInputDialog.getItem(
                    self, "Document Language", "Language:", COMMON_LANGUAGES, 0, True
                )
                if not ok:
                    return False
            session.execute(SetAccessibilityCommand(replace(settings, language=value.strip())))
        elif f.rule == "display-title":
            session.execute(SetAccessibilityCommand(replace(settings, display_doc_title=True)))
        elif f.rule == "tab-order":
            session.execute(SetAccessibilityCommand(replace(settings, tab_order_structure=True)))
        elif f.rule == "title":
            if value is None:
                value, ok = QInputDialog.getText(self, "Document Title", "Title:")
                if not ok or not value.strip():
                    return False
            new = copy.copy(meta)
            new.title = value.strip()
            session.execute(SetMetadataCommand(new))
        elif f.rule == "alt-text" and f.ref is not None:
            if value is None:
                value, ok = QInputDialog.getMultiLineText(
                    self, "Alternate Text", "Describe the figure for people who can't see it:"
                )
                if not ok or not value.strip():
                    return False
            with session.lock:
                node = next(
                    n for n in walk_all(session.document.structure_tree()) if n.ref == f.ref
                )
            session.execute(
                SetStructElementCommand(f.ref, node.type, node.alt, None, value.strip())
            )
        else:
            return False
        self.run()
        return True


class TagsPanel(ViewPanel):
    title = "Tags"
    rebuild_on = frozenset({ChangeKind.STRUCTURE, ChangeKind.METADATA})

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Tag", "Alternate text"])
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self._jump(item))
        self.empty = QLabel("This document has no tags.", self)
        self.rename_button = QPushButton("Change Tag…", self)
        self.rename_button.clicked.connect(lambda: self.change_type())
        self.alt_button = QPushButton("Alternate Text…", self)
        self.alt_button.clicked.connect(lambda: self.change_alt())
        self.up_button = QPushButton("Move Up", self)
        self.up_button.clicked.connect(lambda: self.move_tag(-1))
        self.down_button = QPushButton("Move Down", self)
        self.down_button.clicked.connect(lambda: self.move_tag(1))
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        row1.addWidget(self.rename_button)
        row1.addWidget(self.alt_button)
        row2.addWidget(self.up_button)
        row2.addWidget(self.down_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addWidget(self.tree, 1)
        layout.addLayout(row1)
        layout.addLayout(row2)
        self.nodes: dict[int, StructNode] = {}
        self.parents: dict[int, int | None] = {}

    def rebuild(self) -> None:
        selected = self.selected_ref()
        self.tree.clear()
        self.nodes, self.parents = {}, {}
        roots: list[StructNode] = []
        if self.view is not None and self.view.session.engine.capabilities.structure:
            with self.view.session.lock:
                roots = self.view.session.document.structure_tree()

        def add(node: StructNode, parent_item: QTreeWidgetItem | None, parent: int | None) -> None:
            label = node.type + (f" ({node.role})" if node.role and node.role != node.type else "")
            text = node.alt or node.title or node.actual_text
            item = QTreeWidgetItem([label, text])
            item.setData(0, Qt.ItemDataRole.UserRole, node.ref)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            self.nodes[node.ref] = node
            self.parents[node.ref] = parent
            for child in node.children:
                add(child, item, node.ref)

        for root in roots:
            add(root, None, None)
        self.tree.expandToDepth(1)
        self.empty.setVisible(not roots)
        if selected is not None:
            self.show_tag(selected)
        self._update_buttons()

    def selected_ref(self) -> int | None:
        items = self.tree.selectedItems()
        return int(items[0].data(0, Qt.ItemDataRole.UserRole)) if items else None

    def show_tag(self, ref: int) -> None:
        for item in self._items():
            if item.data(0, Qt.ItemDataRole.UserRole) == ref:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return

    def _items(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            out.append(item)
            stack.extend(item.child(i) for i in range(item.childCount()))
        return out

    def _siblings(self, ref: int) -> list[int]:
        parent = self.parents.get(ref)
        if parent is None:
            return [r for r, p in self.parents.items() if p is None]
        return [c.ref for c in self.nodes[parent].children]

    def _update_buttons(self) -> None:
        ref = self.selected_ref()
        for b in (self.rename_button, self.alt_button, self.up_button, self.down_button):
            b.setEnabled(ref is not None)
        if ref is not None:
            siblings = self._siblings(ref)
            self.up_button.setEnabled(siblings.index(ref) > 0)
            self.down_button.setEnabled(siblings.index(ref) < len(siblings) - 1)

    def _jump(self, item: QTreeWidgetItem) -> None:
        node = self.nodes.get(int(item.data(0, Qt.ItemDataRole.UserRole)))
        if node is not None and node.page_index is not None and self.view is not None:
            self.view.go_to_page(node.page_index)

    def change_type(self, value: str | None = None) -> bool:
        ref = self.selected_ref()
        if ref is None or self.view is None:
            return False
        node = self.nodes[ref]
        if value is None:
            types = list(STANDARD_TYPES)
            current = types.index(node.type) if node.type in types else 0
            value, ok = QInputDialog.getItem(self, "Change Tag", "Tag type:", types, current, True)
            if not ok:
                return False
        if not value.strip() or value.strip() == node.type:
            return False
        self.view.session.execute(
            SetStructElementCommand(ref, node.type, node.alt, value.strip(), None)
        )
        return True

    def change_alt(self, value: str | None = None) -> bool:
        ref = self.selected_ref()
        if ref is None or self.view is None:
            return False
        node = self.nodes[ref]
        if value is None:
            value, ok = QInputDialog.getMultiLineText(
                self, "Alternate Text", f"Alternate text for this {node.type}:", node.alt
            )
            if not ok:
                return False
        if value == node.alt:
            return False
        self.view.session.execute(SetStructElementCommand(ref, node.type, node.alt, None, value))
        return True

    def move_tag(self, step: int) -> bool:
        ref = self.selected_ref()
        if ref is None or self.view is None:
            return False
        siblings = self._siblings(ref)
        i = siblings.index(ref)
        j = i + step
        if not 0 <= j < len(siblings):
            return False
        new = list(siblings)
        new[i], new[j] = new[j], new[i]
        self.view.session.execute(ReorderStructCommand(self.parents.get(ref), siblings, new))
        return True
