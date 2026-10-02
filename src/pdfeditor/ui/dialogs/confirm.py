"""Confirmations for destructive commands: a red primary button, and what exactly is lost.

The primary button names the action ("Flatten 4 Comments", not "Yes") and is painted in the
danger color; Cancel is the default button, so Enter and Escape both back out, and a click is
needed to destroy something.

Callers use the module function (``confirm.confirm_destructive(...)``), so tests can answer
it with ``monkeypatch.setattr(confirm, "confirm_destructive", ...)``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QPushButton, QWidget

from pdfeditor.ui.dialogs.base import FormDialog, set_role


class ConfirmDialog(FormDialog):
    """``title`` as the question, ``text`` saying what will be lost, ``action_label`` on the
    red button. ``extra`` adds a second, non-destructive choice (e.g. "Save")."""

    EXTRA = 2  # result code when the extra button was chosen

    def __init__(
        self,
        title: str,
        text: str,
        action_label: str,
        parent: QWidget | None = None,
        *,
        window_title: str | None = None,
        extra: str | None = None,
    ) -> None:
        super().__init__(
            title, "", parent, window_title=window_title, primary=action_label, danger=True
        )
        self.message = QLabel(text, self)
        self.message.setObjectName("ConfirmText")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.add_widget(self.message)
        self.setAccessibleDescription(text)
        self.action_button = self.primary_button
        assert self.action_button is not None and self.cancel_button is not None
        self.action_button.setAutoDefault(False)
        self.action_button.setDefault(False)
        self.extra_button: QPushButton | None = None
        if extra is not None:  # an action role: it doesn't accept or reject by itself
            self.extra_button = self.button_box.addButton(
                extra, QDialogButtonBox.ButtonRole.ActionRole
            )
            self.extra_button.setAutoDefault(False)
            self.extra_button.clicked.connect(lambda: self.done(self.EXTRA))
        # Cancel is the default (Enter backs out), but looks like a normal button: the red
        # one is still the main choice on screen
        self.cancel_button.setDefault(True)
        set_role(self.cancel_button, "secondary")

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        assert self.cancel_button is not None
        self.cancel_button.setFocus()


def confirm_destructive(parent: QWidget | None, title: str, text: str, action_label: str) -> bool:
    """Ask before a destructive command; True only if the user chose ``action_label``."""
    dialog = ConfirmDialog(title, text, action_label, parent)
    try:
        return dialog.exec() == ConfirmDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
