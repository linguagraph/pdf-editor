"""Request Signatures: the name and email of the person who will sign the box just drawn.

Nothing is sent from here. The email address only makes the email draft's recipients.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialogButtonBox, QLabel, QLineEdit, QPushButton, QWidget

from pdfeditor.ui.dialogs.base import FormDialog, add_row, set_role

NAME_REQUIRED = "Enter the name of the person who will sign this box."
EMAIL_INVALID = "Enter an email address like name@example.com, or leave the email empty."


def valid_email(text: str) -> bool:
    """A simple check: one @, something on both sides, no spaces. Empty is allowed."""
    text = text.strip()
    if not text:
        return True
    local, sep, domain = text.partition("@")
    return bool(sep) and "@" not in domain and bool(local) and bool(domain) and " " not in text


class RequestSignerDialog(FormDialog):
    """One signature box: who signs it. Finish ends the request, Add Another Signer keeps going,
    and Cancel (or Esc) drops only this box."""

    def __init__(self, number: int, page: int, parent: QWidget | None = None) -> None:
        super().__init__(
            f"Signer {number}",
            f"A signature box on page {page + 1}. Its name appears in the box until the person "
            "signs it.",
            parent,
            primary="Finish",
        )
        self.add_another = False
        form = self.add_form()
        self.name = QLineEdit(self)
        self.name.setPlaceholderText("Full name, as it should appear in the signature")
        add_row(form, "&Name:", self.name)
        self.email = QLineEdit(self)
        self.email.setPlaceholderText("name@example.com (optional)")
        add_row(form, "&Email:", self.email, "Used only to address the email draft.")
        self.error = QLabel(self)
        self.error.setWordWrap(True)
        set_role(self.error, "error")
        self.error.setVisible(False)
        self.add_widget(self.error)

        self.add_button = QPushButton("Add Another Signer", self)
        self.button_box.addButton(self.add_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.add_button.clicked.connect(self._add_another)
        self.name.setFocus()

    def _checked(self) -> bool:
        if not self.name.text().strip():
            self.show_error(NAME_REQUIRED)
            return False
        if not valid_email(self.email.text()):
            self.show_error(EMAIL_INVALID)
            return False
        return True

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(True)

    def primary_clicked(self) -> None:
        self.add_another = False
        if self._checked():
            self.accept()

    def _add_another(self) -> None:
        self.add_another = True
        if self._checked():
            self.accept()
        else:
            self.add_another = False

    def signer_name(self) -> str:
        return self.name.text().strip()

    def signer_email(self) -> str:
        return self.email.text().strip()
