"""Password prompt used as the engine's password callback."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QInputDialog, QLineEdit, QWidget


def password_prompt(parent: QWidget | None, file_name: str) -> Callable[[int], str | None]:
    def ask(attempt: int) -> str | None:
        message = f"“{file_name}” is protected. Enter the password to open it:"
        if attempt > 1:
            message = "Incorrect password. Try again:\n\n" + message
        text, ok = QInputDialog.getText(
            parent, "Password Required", message, QLineEdit.EchoMode.Password
        )
        return text if ok else None

    return ask
