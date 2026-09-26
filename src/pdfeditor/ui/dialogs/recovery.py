"""Offer documents recovered from a previous session that ended unexpectedly."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.autosave import RecoveryEntry


class RecoveryDialog(QDialog):
    """Result: :attr:`selected` entries to reopen; :attr:`discard_rest` says what to do with
    the others (True = delete them, False = ask again next time)."""

    def __init__(self, entries: list[RecoveryEntry], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recover Unsaved Documents")
        self.entries = entries
        self.selected: list[RecoveryEntry] = []
        self.discard_rest = False

        intro = QLabel(
            "pdfeditor closed unexpectedly. These documents had unsaved changes. "
            "Select the ones to recover:",
            self,
        )
        intro.setWordWrap(True)
        self.list = QListWidget(self)
        for entry in entries:
            when = datetime.fromtimestamp(entry.saved_at).strftime("%Y-%m-%d %H:%M")
            where = f" ({entry.original_path})" if entry.original_path else ""
            item = QListWidgetItem(f"{entry.display_name} — saved {when}{where}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)

        buttons = QDialogButtonBox(self)
        recover = buttons.addButton("Recover", QDialogButtonBox.ButtonRole.AcceptRole)
        discard = buttons.addButton("Discard All", QDialogButtonBox.ButtonRole.DestructiveRole)
        later = buttons.addButton("Decide Later", QDialogButtonBox.ButtonRole.RejectRole)
        recover.clicked.connect(self._recover)
        discard.clicked.connect(self._discard)
        later.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.list)
        layout.addWidget(buttons)
        self.resize(560, 320)

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
