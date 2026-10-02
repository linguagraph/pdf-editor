"""The "Search tools…" box in the ribbon's tab row: the command palette, inline.

Typing filters every command (the same list and order as :class:`CommandPalette`) in a popup
list under the box; Up/Down pick, Enter runs, Esc closes and returns to the page. Ctrl+Shift+P
(the palette's shortcut) focuses it.

The popup is a plain child of the main window rather than a ``Qt.Popup`` window, so the box
keeps the keyboard focus while the list follows what is typed. In a narrow window or with the
compact ribbon the box shrinks to a search button; clicking it (or the shortcut) opens the box
until it loses focus.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QWidget,
)

from pdfeditor.ui.icons import icon
from pdfeditor.ui.shortcuts import ShortcutManager, current_command, fill_command_list

BOX_WIDTH = 200  # px of the search box in the tab row
POPUP_MIN_WIDTH = 340
POPUP_ROWS = 10  # rows shown before the list scrolls
PALETTE_ID = "command-palette"


class _SearchEdit(QLineEdit):
    """The text box: list navigation and Enter/Esc go to the search widget."""

    def __init__(self, owner: CommandSearch) -> None:
        super().__init__(owner)
        self._owner = owner

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._owner.handle_key(event):
            super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self._owner.focus_left()


class CommandSearch(QWidget):
    def __init__(
        self,
        manager: ShortcutManager,
        parent: QWidget | None = None,
        focus_back: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("CommandSearch")
        self.manager = manager
        self.focus_back = focus_back  # where the keyboard goes after Esc or a command
        self.collapsed = False  # shown as a button (narrow window, compact ribbon)
        self._opened = False  # the box is open although collapsed (button clicked)
        self.button = QToolButton(self)
        self.button.setIcon(icon("search"))
        self.button.setAutoRaise(True)
        self.button.setAccessibleName("Search tools")
        self.button.clicked.connect(self.activate)
        self.button.hide()
        self.edit = _SearchEdit(self)
        self.edit.setPlaceholderText("Search tools…")
        self.edit.setAccessibleName("Search tools")
        self.edit.setClearButtonEnabled(True)
        self.edit.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.edit.setFixedWidth(BOX_WIDTH)
        self.edit.textEdited.connect(self._on_text)
        self.popup = QListWidget(self)  # moves to the window when shown (see _show_popup)
        self.popup.setObjectName("CommandSearchPopup")
        self.popup.setAccessibleName("Matching commands")
        self.popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the box keeps the keyboard
        self.popup.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.popup.setUniformItemSizes(True)
        self.popup.itemClicked.connect(self._clicked)
        self.popup.hide()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.button)
        layout.addWidget(self.edit)
        manager.changed.append(self._update_tooltip)
        self._update_tooltip()

    # -- size modes -----------------------------------------------------------------------
    def set_collapsed(self, collapsed: bool) -> None:
        """Show just a search button (the box opens when it's clicked)."""
        if collapsed == self.collapsed:
            return
        self.collapsed = collapsed
        self._opened = False
        self._show_box(not collapsed or self.edit.hasFocus())

    def _show_box(self, box: bool) -> None:
        self.edit.setVisible(box)
        self.button.setVisible(not box)

    def _update_tooltip(self) -> None:
        keys = self.manager.current(PALETTE_ID) if PALETTE_ID in self.manager.actions else ()
        tip = "Search tools and commands" + (f" ({keys[0]})" if keys else "")
        self.edit.setToolTip(tip)
        self.button.setToolTip(tip)

    # -- use ------------------------------------------------------------------------------
    def activate(self) -> None:
        """Focus the box (opening it if it's collapsed to a button), text selected."""
        if self.collapsed:
            self._opened = True
            self._show_box(True)
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.edit.selectAll()
        if self.edit.text():
            self._show_popup()

    def _on_text(self, text: str) -> None:
        if text.strip():
            self._show_popup()
        else:
            self.close_popup()

    def _show_popup(self) -> None:
        fill_command_list(self.popup, self.manager, self.edit.text())
        window = self.window()
        if self.popup.parentWidget() is not window:
            self.popup.setParent(window)
        rows = min(max(self.popup.count(), 1), POPUP_ROWS)
        row_height = self.popup.sizeHintForRow(0) if self.popup.count() else 22
        frame = 2 * self.popup.frameWidth()
        size = QSize(max(POPUP_MIN_WIDTH, self.edit.width()), rows * row_height + frame + 4)
        below = self.edit.mapTo(window, QPoint(0, self.edit.height() + 2))
        x = below.x() + self.edit.width() - size.width()  # right-aligned under the box
        x = max(4, min(x, window.width() - size.width() - 4))
        self.popup.setGeometry(x, below.y(), size.width(), size.height())
        if self.popup.count() == 0:
            self.popup.addItem(QListWidgetItem("No matching commands"))
            self.popup.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self.popup.show()
        self.popup.raise_()

    def close_popup(self) -> None:
        self.popup.hide()

    @property
    def popup_open(self) -> bool:
        return self.popup.isVisible()

    def handle_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
            if not self.popup_open:
                self._show_popup()  # Down on an empty box lists every command
                return True
            step = {
                Qt.Key.Key_Down: 1,
                Qt.Key.Key_Up: -1,
                Qt.Key.Key_PageDown: POPUP_ROWS,
                Qt.Key.Key_PageUp: -POPUP_ROWS,
            }[Qt.Key(key)]
            row = max(0, min(self.popup.count() - 1, self.popup.currentRow() + step))
            self.popup.setCurrentRow(row)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.run_current()
            return True
        if key == Qt.Key.Key_Escape:
            self.finish()
            return True
        return False

    def run_current(self) -> bool:
        """Run the highlighted command (Enter)."""
        action = current_command(self.popup, self.manager) if self.popup_open else None
        if action is None and self.edit.text().strip():
            fill_command_list(self.popup, self.manager, self.edit.text())
            action = current_command(self.popup, self.manager)
        if action is None:
            QApplication.beep()
            return False
        self.finish()
        action.trigger()
        return True

    def _clicked(self, item: QListWidgetItem) -> None:
        if item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.popup.setCurrentItem(item)
            self.run_current()

    def finish(self) -> None:
        """Close the list, clear the box and give the keyboard back to the page."""
        self.close_popup()
        self.edit.clear()
        if self.focus_back is not None:
            self.focus_back()
        if self.edit.hasFocus():
            self.edit.clearFocus()
        self.focus_left()

    def focus_left(self) -> None:
        if self.edit.hasFocus():
            return
        self.close_popup()
        if self.collapsed and self._opened:
            self._opened = False
            self._show_box(False)
