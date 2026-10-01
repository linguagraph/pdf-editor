"""Password security dialog: open password, permissions password and permissions."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.metadata import EncryptionMethod, Permissions, SecuritySettings
from pdfeditor.ui.dialogs.base import FormDialog, add_row, form_layout


def _password() -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    return edit


class SecurityDialog(FormDialog):
    def __init__(self, current: Permissions | None = None, parent: QWidget | None = None) -> None:
        super().__init__(
            "Encrypt with Password",
            "The security is applied when you save the document.",
            parent,
        )
        self.method = QComboBox(self)
        self.method.addItem("AES 256-bit (Acrobat X and later)", EncryptionMethod.AES_256)
        self.method.addItem("AES 128-bit (Acrobat 7 and later)", EncryptionMethod.AES_128)

        self.require_open = QCheckBox("Require a password to open the document", self)
        self.user_pw, self.user_pw2 = _password(), _password()
        open_box = QGroupBox("Document open", self)
        form = form_layout()
        open_box.setLayout(form)
        form.addRow(self.require_open)
        add_row(form, "Password:", self.user_pw)
        add_row(form, "Confirm:", self.user_pw2)
        self.user_pw2.setAccessibleName("Confirm open password")

        self.restrict = QCheckBox("Restrict printing, editing and copying", self)
        self.owner_pw, self.owner_pw2 = _password(), _password()
        p = current or Permissions()
        self.allow_print = QCheckBox("Printing", self)
        self.allow_print.setChecked(p.print)
        self.allow_modify = QCheckBox("Changing the document", self)
        self.allow_modify.setChecked(p.modify)
        self.allow_copy = QCheckBox("Copying text and images", self)
        self.allow_copy.setChecked(p.copy)
        self.allow_annotate = QCheckBox("Commenting", self)
        self.allow_annotate.setChecked(p.annotate)
        self.allow_forms = QCheckBox("Filling in forms", self)
        self.allow_forms.setChecked(p.fill_forms)
        self.allow_assemble = QCheckBox("Inserting, deleting and rotating pages", self)
        self.allow_assemble.setChecked(p.assemble)
        perm_box = QGroupBox("Permissions", self)
        form = form_layout()
        perm_box.setLayout(form)
        form.addRow(self.restrict)
        add_row(form, "Permissions password:", self.owner_pw)
        add_row(form, "Confirm:", self.owner_pw2)
        self.owner_pw2.setAccessibleName("Confirm permissions password")
        allowed = QVBoxLayout()
        allowed.setSpacing(2)
        for box in (self.allow_print, self.allow_modify, self.allow_copy, self.allow_annotate,
                    self.allow_forms, self.allow_assemble):  # fmt: skip
            allowed.addWidget(box)
        form.addRow("Allowed:", allowed)

        self.error = QLabel(self)
        self.error.setProperty("role", "error")  # colored by the app style sheet
        self.error.setWordWrap(True)
        method_row = self.add_form()
        add_row(method_row, "Encryption:", self.method)
        self.add_widget(open_box)
        self.add_widget(perm_box)
        self.add_widget(self.error)

        self.require_open.toggled.connect(lambda _on: self._sync())
        self.restrict.toggled.connect(lambda _on: self._sync())
        self._sync()

    def _sync(self) -> None:
        opening: tuple[QWidget, ...] = (self.user_pw, self.user_pw2)
        for w in opening:
            w.setEnabled(self.require_open.isChecked())
        restricting: tuple[QWidget, ...] = (
            self.owner_pw, self.owner_pw2, self.allow_print, self.allow_modify, self.allow_copy,
            self.allow_annotate, self.allow_forms, self.allow_assemble,
        )  # fmt: skip
        for w in restricting:
            w.setEnabled(self.restrict.isChecked())

    def problem(self) -> str:
        """Why the input can't be accepted ("" when it can)."""
        if not self.require_open.isChecked() and not self.restrict.isChecked():
            return "Choose an open password, permissions, or both."
        if self.require_open.isChecked():
            if not self.user_pw.text():
                return "Enter the password to open the document."
            if self.user_pw.text() != self.user_pw2.text():
                return "The open passwords don't match."
        if self.restrict.isChecked():
            if not self.owner_pw.text():
                return "Enter a permissions password: it's needed to lift the restrictions."
            if self.owner_pw.text() != self.owner_pw2.text():
                return "The permissions passwords don't match."
            if self.require_open.isChecked() and self.owner_pw.text() == self.user_pw.text():
                return "The permissions password must differ from the open password."
        return ""

    def primary_clicked(self) -> None:
        self._try_accept()

    def _try_accept(self) -> None:
        message = self.problem()
        self.error.setText(message)
        if not message:
            self.accept()

    def settings(self) -> SecuritySettings:
        method = self.method.currentData()
        assert isinstance(method, EncryptionMethod)
        user = self.user_pw.text() if self.require_open.isChecked() else ""
        if self.restrict.isChecked():
            owner = self.owner_pw.text()
            perms = Permissions(
                print=self.allow_print.isChecked(),
                modify=self.allow_modify.isChecked(),
                copy=self.allow_copy.isChecked(),
                annotate=self.allow_annotate.isChecked(),
                fill_forms=self.allow_forms.isChecked(),
                assemble=self.allow_assemble.isChecked(),
                print_high_quality=self.allow_print.isChecked(),
            )
        else:
            # only an open password: whoever can open it may do everything
            owner, perms = user, Permissions()
        return SecuritySettings(method, user, owner, perms)
