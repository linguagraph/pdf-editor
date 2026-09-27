"""Keyboard shortcuts (Acrobat conventions, user-customisable) and the command palette."""

from __future__ import annotations

import re
from collections.abc import Iterable

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

SETTINGS_GROUP = "shortcuts"
# Acrobat shortcuts the actions don't get from Qt's platform defaults.
ACROBAT_DEFAULTS: dict[str, tuple[str, ...]] = {
    "close": ("Ctrl+W", "Ctrl+F4"),
    "exit": ("Ctrl+Q",),
    "sticky-note": ("Ctrl+6",),
    "command-palette": ("Ctrl+Shift+P",),
    "keyboard-shortcuts": (),
}


def action_id(action: QAction) -> str:
    """A stable id from the action's text: "Save &As…" -> "save-as"."""
    if action.objectName():
        return action.objectName()
    text = action.text().replace("&", "").replace("…", "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _sequences(action: QAction) -> tuple[str, ...]:
    return tuple(s.toString() for s in action.shortcuts() if not s.isEmpty())


def command_actions(root: QWidget) -> list[QAction]:
    """The application's commands: actions marked with the ``needs_doc`` property."""
    seen: set[int] = set()
    out = []
    for a in root.findChildren(QAction):
        if a.property("needs_doc") is None or not a.text() or id(a) in seen:
            continue
        seen.add(id(a))
        out.append(a)
    return out


class ShortcutManager:
    """Remembers each command's default shortcuts, applies the user's overrides (QSettings)
    and changes them."""

    def __init__(self, root: QWidget, settings: QSettings | None = None) -> None:
        self.settings = settings or QSettings()
        self.actions: dict[str, QAction] = {}
        self.defaults: dict[str, tuple[str, ...]] = {}
        self.register(command_actions(root))

    def register(self, actions: Iterable[QAction]) -> None:
        for a in actions:
            key = action_id(a)
            if key in self.actions:
                continue
            self.actions[key] = a
            default = _sequences(a)
            if key in ACROBAT_DEFAULTS:
                extra = ACROBAT_DEFAULTS[key]
                default = tuple(dict.fromkeys((*extra, *default)))
            self.defaults[key] = default
            self._apply(key, self.user_value(key))

    def user_value(self, key: str) -> tuple[str, ...] | None:
        """The user's override (() = no shortcut), or None when using the default."""
        value = self.settings.value(f"{SETTINGS_GROUP}/{key}")
        if value is None:
            return None
        text = str(value)
        return tuple(s for s in text.split("|") if s) if text else ()

    def current(self, key: str) -> tuple[str, ...]:
        user = self.user_value(key)
        return self.defaults.get(key, ()) if user is None else user

    def _apply(self, key: str, seqs: tuple[str, ...] | None) -> None:
        action = self.actions[key]
        use = self.defaults[key] if seqs is None else seqs
        action.setShortcuts([QKeySequence(s) for s in use])

    def set(self, key: str, seqs: tuple[str, ...]) -> None:
        """Assign shortcuts (an empty tuple removes them); persisted."""
        if seqs == self.defaults[key]:
            self.reset(key)
            return
        self.settings.setValue(f"{SETTINGS_GROUP}/{key}", "|".join(seqs))
        self._apply(key, seqs)

    def reset(self, key: str | None = None) -> None:
        keys = [key] if key is not None else list(self.actions)
        for k in keys:
            self.settings.remove(f"{SETTINGS_GROUP}/{k}")
            self._apply(k, None)

    def conflicts(self, key: str, seq: str) -> list[str]:
        """Other commands already using ``seq``."""
        wanted = QKeySequence(seq).toString()
        return [k for k in self.actions if k != key and wanted in self.current(k)]

    def label(self, key: str) -> str:
        return self.actions[key].text().replace("&", "").replace("…", "").strip()


class ShortcutsDialog(QDialog):
    """Edit shortcuts: filter, select a command, press the new keys."""

    def __init__(self, manager: ShortcutManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(620, 560)
        self.manager = manager
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText("Filter commands…")
        self.filter.setAccessibleName("Filter commands")
        self.filter.textChanged.connect(lambda _t: self._fill())
        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Command", "Shortcut"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selected)
        self.editor = QKeySequenceEdit(self)
        self.editor.setAccessibleName("New shortcut")
        self.assign = QPushButton("Assign", self)
        self.assign.clicked.connect(lambda: self.apply_keys(self.editor.keySequence().toString()))
        self.clear = QPushButton("Remove", self)
        self.clear.clicked.connect(lambda: self.apply_keys(""))
        self.reset_one = QPushButton("Default", self)
        self.reset_one.clicked.connect(self._reset_one)
        self.reset_all = QPushButton("Reset All", self)
        self.reset_all.clicked.connect(self._reset_all)
        self.message = QLabel(self)
        self.message.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(QLabel("Keys:", self))
        row.addWidget(self.editor, 1)
        for b in (self.assign, self.clear, self.reset_one):
            row.addWidget(b)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.addButton(self.reset_all, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.filter)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(self.message)
        layout.addWidget(buttons)
        self._fill()
        self._selected()

    def _fill(self) -> None:
        words = self.filter.text().lower().split()
        keys = sorted(self.manager.actions, key=lambda k: self.manager.label(k).lower())
        rows = [k for k in keys if all(w in self.manager.label(k).lower() for w in words)]
        self.table.setRowCount(len(rows))
        for r, key in enumerate(rows):
            name = QTableWidgetItem(self.manager.label(key))
            name.setData(Qt.ItemDataRole.UserRole, key)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(", ".join(self.manager.current(key))))
        self.table.resizeColumnToContents(0)

    def selected_key(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def select(self, key: str) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                self.table.selectRow(r)
                return

    def _selected(self) -> None:
        key = self.selected_key()
        for w in (self.editor, self.assign, self.clear, self.reset_one):
            w.setEnabled(key is not None)
        if key is not None:
            current = self.manager.current(key)
            self.editor.setKeySequence(QKeySequence(current[0] if current else ""))

    def apply_keys(self, seq: str) -> bool:
        key = self.selected_key()
        if key is None:
            return False
        if seq:
            clash = self.manager.conflicts(key, seq)
            if clash:
                names = ", ".join(self.manager.label(k) for k in clash)
                self.message.setText(f"{seq} is already used by: {names}. Remove it there first.")
                return False
        self.manager.set(key, (seq,) if seq else ())
        self.message.setText("")
        self._refresh(key)
        return True

    def _reset_one(self) -> None:
        key = self.selected_key()
        if key is not None:
            self.manager.reset(key)
            self._refresh(key)

    def _reset_all(self) -> None:
        key = self.selected_key()
        self.manager.reset()
        self._refresh(key)

    def _refresh(self, key: str | None) -> None:
        self._fill()
        if key is not None:
            self.select(key)


class CommandPalette(QDialog):
    """Type to find any command and run it (Ctrl+Shift+P)."""

    def __init__(self, manager: ShortcutManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Command Palette")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
        self.resize(520, 420)
        self.manager = manager
        self.query = QLineEdit(self)
        self.query.setPlaceholderText("Type a command…")
        self.query.setAccessibleName("Command")
        self.query.textChanged.connect(lambda _t: self._fill())
        self.query.returnPressed.connect(self.run_current)
        self.list = QListWidget(self)
        self.list.setAccessibleName("Matching commands")
        self.list.itemActivated.connect(lambda _i: self.run_current())
        layout = QVBoxLayout(self)
        layout.addWidget(self.query)
        layout.addWidget(self.list, 1)
        self._fill()
        self.query.setFocus()

    def _fill(self) -> None:
        words = self.query.text().lower().split()
        self.list.clear()
        entries = []
        for key, action in self.manager.actions.items():
            if not action.isEnabled() or not action.isVisible():
                continue
            label = self.manager.label(key)
            if all(w in label.lower() for w in words):
                entries.append((label, key))
        for label, key in sorted(entries, key=lambda e: (not e[0].lower().startswith(
                self.query.text().lower()), e[0].lower())):  # fmt: skip
            shortcut = ", ".join(self.manager.current(key))
            item = QListWidgetItem(f"{label}    {shortcut}" if shortcut else label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.list.count():
            row = self.list.currentRow() + (1 if event.key() == Qt.Key.Key_Down else -1)
            self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
            return
        super().keyPressEvent(event)

    def run_current(self) -> bool:
        item = self.list.currentItem()
        if item is None:
            return False
        action = self.manager.actions[str(item.data(Qt.ItemDataRole.UserRole))]
        self.accept()
        action.trigger()
        return True
