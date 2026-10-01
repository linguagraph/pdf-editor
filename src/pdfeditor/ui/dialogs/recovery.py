"""Offer documents recovered from a previous session that ended unexpectedly."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QWidget,
)

from pdfeditor.core.autosave import RecoveryEntry
from pdfeditor.ui.dialogs.base import FormDialog


class RecoveryDialog(FormDialog):
    """Result: :attr:`selected` entries to reopen; :attr:`discard_rest` says what to do with
    the others (True = delete them, False = ask again next time)."""

    def __init__(self, entries: list[RecoveryEntry], parent: QWidget | None = None) -> None:
        super().__init__(
            "Recover Unsaved Documents",
            "pdfeditor closed unexpectedly. These documents had unsaved changes. "
            "Select the ones to recover:",
            parent,
            primary="Recover",
            cancel="Decide Later",
        )
        self.entries = entries
        self.selected: list[RecoveryEntry] = []
        self.discard_rest = False

        self.list = QListWidget(self)
        self.list.setAccessibleName("Recovered documents")
        for entry in entries:
            when = datetime.fromtimestamp(entry.saved_at).strftime("%Y-%m-%d %H:%M")
            where = f" ({entry.original_path})" if entry.original_path else ""
            item = QListWidgetItem(f"{entry.display_name} — saved {when}{where}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)

        discard = self.button_box.addButton(
            "Discard All", QDialogButtonBox.ButtonRole.DestructiveRole
        )
        discard.clicked.connect(self._discard)
        self.add_widget(self.list, 1)
        self.resize(580, 360)

    def primary_clicked(self) -> None:
        self._recover()

    def _recover(self) -> None:
        self.selected = [
            e
            for i, e in enumerate(self.entries)
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]
        self.discard_rest = True  # unchecked entries were deliberately left out
        self.accept()

    def _discard(self) -> None:
        self.selected = []
        self.discard_rest = True
        self.accept()
