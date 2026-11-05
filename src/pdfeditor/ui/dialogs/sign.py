"""Sign Document dialog: choose a key (Windows store, PKCS#11 token or PFX file) and details.

The PIN and passwords live only in masked fields while the dialog is open, are never saved or
logged, and are cleared as soon as the signature is on its way. A PIN leaves the field only for
the token selected in the list, and only from Log In and List or Sign, never from listing tokens
or from choosing a module.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.signing import (
    CertificateInfo,
    EmptySignatureField,
    KeyRef,
    PfxKey,
    PinState,
    Pkcs11Key,
    SignatureOptions,
    SignaturePlacement,
    TokenCertificate,
    TokenInfo,
    WindowsStoreKey,
)
from pdfeditor.services.signing import SigningError, WrongPin, windows_store
from pdfeditor.services.signing.tokens import list_tokens, login_and_list
from pdfeditor.ui.dialogs.base import FormDialog, add_row, caption, form_layout
from pdfeditor.ui.jobs import run_modal

SOURCE_STORE, SOURCE_TOKEN, SOURCE_FILE = "store", "pkcs11", "pfx"
NO_CERTIFICATES = (
    "No signing certificates were found in the Windows certificate store. If you use a USB "
    "token or smart card, plug it in, check that its vendor's software (middleware) is "
    "installed, and press Refresh. Otherwise use the token's PKCS#11 module or a PFX file: "
    "pick one under Key above."
)
PIN_WARNING = {
    PinState.COUNT_LOW: "This token has recorded a wrong PIN. It locks after a few failures.",
    PinState.FINAL_TRY: "Careful: one more wrong PIN locks this token for good.",
    PinState.LOCKED: "This token's PIN is locked. Unlock it with the vendor's tools first.",
}
REJECTED_WARNING = (
    "The token rejected the last PIN. Check it before trying again: tokens lock after a few "
    "failures, and this app never retries on its own."
)


def _kind(info: CertificateInfo) -> str:
    if info.hardware is True:
        return "USB token or smart card"
    if info.hardware is False:
        return "software key"
    return info.provider or "unknown key type"


def _certificate_text(info: CertificateInfo) -> str:
    return (
        f"{info.subject}\nIssued by {info.issuer} · valid until {info.not_after:%Y-%m-%d} · "
        f"{_kind(info)}"
    )


def _password_field() -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setInputMethodHints(
        Qt.InputMethodHint.ImhHiddenText | Qt.InputMethodHint.ImhNoPredictiveText
    )
    return edit


def _token_name(token: TokenInfo) -> str:
    return token.label or token.serial or f"slot {token.slot_id}"


def _disable_item(item: QListWidgetItem) -> None:
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)


class SignDialog(FormDialog):
    def __init__(
        self,
        placement: SignaturePlacement | None,
        parent: QWidget | None = None,
        *,
        rejected_tokens: set[tuple[str, int]] | None = None,
        empty_fields: tuple[EmptySignatureField, ...] = (),
    ) -> None:
        # Only an invisible signature can go into an existing field: a drawn box makes a new one.
        offer_fields = placement is None and bool(empty_fields)
        if placement is not None:
            subtitle = f"A visible signature on page {placement.page + 1}. "
        elif offer_fields:
            subtitle = "Choose the signature field to sign, or sign invisibly. "
        else:
            subtitle = (
                "An invisible signature: nothing is drawn on the pages. "
                "Use Place Signature to show one. "
            )
        super().__init__(
            "Sign Document",
            subtitle + "The signed copy is saved as a new file; the original isn't changed.",
            parent,
            primary="Sign…",
        )
        self.placement = placement
        self.settings = QSettings()
        # Shared with the controller: (serial, slot) of tokens that rejected their last PIN.
        self.rejected_tokens = rejected_tokens if rejected_tokens is not None else set()
        self._token_rows: list[tuple[TokenInfo, TokenCertificate | None]] = []
        self._busy = False  # a token call is running behind a progress dialog
        self._final_try_confirmed = False

        form = self.add_form()
        self.source = QComboBox(self)
        if windows_store.available():
            self.source.addItem(
                "Windows certificate store, USB tokens and smart cards", SOURCE_STORE
            )
        self.source.addItem("USB token through its PKCS#11 module", SOURCE_TOKEN)
        self.source.addItem("Certificate file (PFX or P12)", SOURCE_FILE)
        add_row(form, "&Key:", self.source)

        self.pages = QStackedWidget(self)
        self.pages.addWidget(self._store_page())
        self.pages.addWidget(self._token_page())
        self.pages.addWidget(self._file_page())
        self.add_widget(self.pages, 1)

        details = self.add_form()
        self.field_choice: QComboBox | None = None
        if offer_fields:
            self.field_choice = QComboBox(self)
            self.field_choice.addItem("New invisible signature", None)
            for field in empty_fields:
                index = self.field_choice.count()
                self.field_choice.addItem(f"{field.label} (page {field.page + 1})", field.name)
                self.field_choice.setItemData(index, field.name, Qt.ItemDataRole.ToolTipRole)
            self.field_choice.setCurrentIndex(1)  # the first empty field, as the default
            add_row(details, "Signature &field:", self.field_choice)
        self.reason = QLineEdit(self)
        self.location = QLineEdit(self)
        self.location.setText(str(self.settings.value("signing/location", "")))
        add_row(details, "&Reason:", self.reason)
        add_row(details, "&Location:", self.location)
        self.use_timestamp = QCheckBox("Add a trusted &timestamp", self)
        self.tsa_url = QLineEdit(self)
        self.tsa_url.setPlaceholderText("https://… address of an RFC 3161 timestamp server")
        self.tsa_url.setText(str(self.settings.value("signing/tsa_url", "")))
        self.use_timestamp.setChecked(self.settings.value("signing/use_tsa", "false") == "true")
        details.addRow(self.use_timestamp)
        add_row(
            details,
            "&Server:",
            self.tsa_url,
            "Proves the time of signing. Only a hash of the signature is sent to this server.",
        )
        self.use_timestamp.toggled.connect(self.tsa_url.setEnabled)
        self.tsa_url.setEnabled(self.use_timestamp.isChecked())

        self.error = QLabel(self)
        self.error.setWordWrap(True)
        self.error.setProperty("role", "error")
        self.error.setVisible(False)
        self.add_widget(self.error)

        self.source.currentIndexChanged.connect(self._source_changed)
        last = self.source.findData(self.settings.value("signing/source", SOURCE_STORE))
        self.source.setCurrentIndex(max(last, 0))
        self._source_changed()

    # -- pages ----------------------------------------------------------------------------
    def _store_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.store_list = QListWidget(page)
        self.store_list.setAccessibleName("Certificates in the Windows store")
        self.store_list.setWordWrap(True)
        self.store_empty = caption(NO_CERTIFICATES, page)
        refresh = QPushButton("&Refresh", page)
        refresh.setAutoDefault(False)
        refresh.clicked.connect(lambda _=False: self.refresh_store())
        self.refresh_button = refresh
        row = QHBoxLayout()
        row.addWidget(caption("Windows or your token's software asks for any PIN.", page), 1)
        row.addWidget(refresh)
        layout.addWidget(self.store_list, 1)
        layout.addWidget(self.store_empty)
        layout.addLayout(row)
        return page

    def _token_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        form = form_layout()
        self.module = QLineEdit(page)
        self.module.setText(str(self.settings.value("signing/pkcs11_module", "")))
        self.module.textChanged.connect(lambda _text: self._module_changed())
        self.browse_button = QPushButton("&Browse…", page)
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(lambda _=False: self._browse_module())
        self.list_button = QPushButton("L&ist", page)
        self.list_button.setAutoDefault(False)
        self.list_button.setToolTip("Reads the tokens without sending any PIN.")
        self.list_button.clicked.connect(lambda _=False: self.list_token_certificates())
        row = QHBoxLayout()
        row.addWidget(self.module, 1)
        row.addWidget(self.browse_button)
        row.addWidget(self.list_button)
        add_row(
            form, "&Module:", row, "The vendor's PKCS#11 DLL (64-bit), usually in Program Files."
        )
        layout.addLayout(form)
        self.token_list = QListWidget(page)
        self.token_list.setAccessibleName("Tokens and certificates")
        self.token_list.setWordWrap(True)
        self.token_list.currentRowChanged.connect(lambda _row: self._token_selected())
        layout.addWidget(self.token_list, 1)
        self.token_status = caption("", page)
        layout.addWidget(self.token_status)
        pin_form = form_layout()
        self.pin = _password_field()
        self.pin.setParent(page)
        self.login_button = QPushButton("Log In and List", page)
        self.login_button.setAutoDefault(False)
        self.login_button.setToolTip("Sends the PIN once, to the selected token only.")
        self.login_button.clicked.connect(lambda _=False: self.log_in_to_selected())
        pin_row = QHBoxLayout()
        pin_row.addWidget(self.pin, 1)
        pin_row.addWidget(self.login_button)
        self.pin_label = QLabel("&PIN:", page)
        self.pin_label.setBuddy(self.pin)
        pin_form.addRow(self.pin_label, pin_row)
        self.pin.setAccessibleName("Token PIN")
        layout.addLayout(pin_form)
        self.pin_warning = QLabel(page)
        self.pin_warning.setWordWrap(True)
        self.pin_warning.setProperty("role", "error")
        self.pin_warning.setVisible(False)
        layout.addWidget(self.pin_warning)
        self.login_button.setVisible(False)
        return page

    def _file_page(self) -> QWidget:
        page = QWidget(self)
        form = form_layout(page)
        self.pfx_path = QLineEdit(page)
        self.pfx_path.setText(str(self.settings.value("signing/pfx_path", "")))
        browse = QPushButton("B&rowse…", page)
        browse.setAutoDefault(False)
        browse.clicked.connect(lambda _=False: self._browse_pfx())
        row = QHBoxLayout()
        row.addWidget(self.pfx_path, 1)
        row.addWidget(browse)
        add_row(form, "&File:", row)
        self.pfx_password = _password_field()
        self.pfx_password.setParent(page)
        add_row(form, "&Password:", self.pfx_password)
        self.pfx_password.setAccessibleName("Password of the certificate file")
        return page

    def current_source(self) -> str:
        return str(self.source.currentData())

    def _source_changed(self) -> None:
        source = self.current_source()
        self.pages.setCurrentIndex({SOURCE_STORE: 0, SOURCE_TOKEN: 1, SOURCE_FILE: 2}[source])
        self.show_error("")
        if source == SOURCE_STORE and not self.store_list.count():
            self.refresh_store()

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(bool(message))

    def _set_busy(self, busy: bool) -> None:
        """While a token call runs: its progress dialog only turns modal after 300 ms, so a quick
        second click could otherwise send a PIN again."""
        self._busy = busy
        for widget in (self.list_button, self.browse_button, self.login_button, self.module):
            widget.setEnabled(not busy)
        if self.primary_button is not None:
            self.primary_button.setEnabled(not busy)
        if not busy:
            self._token_selected()

    # -- Windows store --------------------------------------------------------------------
    def refresh_store(self) -> None:
        """Only reads certificates: no key is touched, so no PIN prompt can come up."""
        self.store_list.clear()
        try:
            certificates = windows_store.list_certificates()
        except SigningError as exc:
            self.show_error(str(exc))
            certificates = []
        selected = False
        for info in certificates:
            problem = info.problem()
            text = _certificate_text(info)
            if problem:
                text += f"\n{problem}"
            item = QListWidgetItem(text, self.store_list)
            item.setData(Qt.ItemDataRole.UserRole, info)
            if problem:
                _disable_item(item)
            elif not selected:
                self.store_list.setCurrentItem(item)
                selected = True
        self.store_empty.setVisible(not certificates)
        self.store_list.setVisible(bool(certificates))

    # -- PKCS#11 --------------------------------------------------------------------------
    def _module_changed(self) -> None:
        """What's listed, and any PIN typed for it, belongs to the previous module."""
        self.pin.clear()
        self.show_error("")
        self.token_status.setText("")
        self.set_tokens([], listed=False)

    def _browse_module(self) -> None:
        if self._busy:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self, "PKCS#11 Module", self.module.text(), "PKCS#11 modules (*.dll *.so *.dylib)"
        )
        if chosen:
            self.module.setText(chosen)  # clears the PIN
            self.list_token_certificates()

    def list_token_certificates(self, select: tuple[str, int] | None = None) -> None:
        """Read the tokens. Never sends a PIN, whatever is typed in the field."""
        if self._busy:
            return
        path = self.module.text().strip()
        self.show_error("")
        if not path:
            self.show_error("Choose the token's PKCS#11 module first.")
            return
        self._set_busy(True)
        try:
            tokens = run_modal(self, "Reading the tokens…", lambda _job: list_tokens(path))
        except RuntimeError as exc:
            self.show_error(str(exc))
            return
        finally:
            self._set_busy(False)
        if tokens is None:  # cancelled
            return
        assert isinstance(tokens, list)
        self.settings.setValue("signing/pkcs11_module", path)
        self.set_tokens(tokens, select=select)

    def log_in_to_selected(self) -> None:
        """For a token hiding its certificates: send the PIN once, to this token only."""
        if self._busy:
            return
        selected = self._selected_token()
        if selected is None or selected[1] is not None:
            return
        token = selected[0]
        if token.pin_state is PinState.LOCKED:
            self.show_error(PIN_WARNING[PinState.LOCKED])
            return
        pin = None if token.protected_auth else self.pin.text()
        if pin == "":
            self.show_error("Enter the token's PIN.")
            return
        if token.pin_state is PinState.FINAL_TRY and not self.confirm_final_try():
            return
        path = self.module.text().strip()
        self.show_error("")
        self._set_busy(True)

        def work(_job: object) -> object:
            try:
                return login_and_list(
                    path,
                    token.slot_id,
                    token.serial,
                    pin,
                    protected_auth=token.protected_auth,
                    allow_final_try=token.pin_state is PinState.FINAL_TRY,
                )
            except SigningError as exc:
                return exc

        try:
            outcome = run_modal(self, "Logging in to the token…", work)
        except RuntimeError as exc:
            outcome = SigningError(str(exc))
        finally:
            self._set_busy(False)
        if isinstance(outcome, SigningError):
            self.pin.clear()  # nothing can send a rejected PIN again
            if isinstance(outcome, WrongPin):
                self.rejected_tokens.add((token.serial, token.slot_id))
            # Re-read the counters, publicly. Stays on this token: no other may get the next PIN.
            self.list_token_certificates(select=(token.serial, token.slot_id))
            self.show_error(str(outcome))
            return
        if not isinstance(outcome, TokenInfo):  # cancelled
            return
        self.rejected_tokens.discard((token.serial, token.slot_id))
        tokens = {id(t): t for t, _cert in self._token_rows}
        updated = [outcome if t is token else t for t in tokens.values()]
        self.set_tokens(updated, logged_in=outcome)

    def confirm_final_try(self) -> bool:
        """One more wrong PIN locks the token: only with an explicit yes."""
        answer = QMessageBox.warning(
            self,
            "Last PIN Attempt",
            "One more wrong PIN locks this token for good. Only continue if you're sure of the "
            "PIN.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        self._final_try_confirmed = answer == QMessageBox.StandardButton.Ok
        return self._final_try_confirmed

    def set_tokens(
        self,
        tokens: list[TokenInfo],
        *,
        listed: bool = True,
        logged_in: TokenInfo | None = None,
        select: tuple[str, int] | None = None,
    ) -> None:
        """One row per certificate, and one per token still hiding its certificates, so every
        token's PIN state shows and the one to log in to can be picked."""
        self.token_list.clear()
        self._token_rows = []
        for token in tokens:
            state = PIN_WARNING.get(token.pin_state, "")
            suffix = f"\n{state}" if state else ""
            if not token.certificates:
                text = (
                    f"{_token_name(token)}: no certificates visible"
                    + (
                        ""
                        if token is logged_in
                        else ". They may appear after login: select this and log in."
                    )
                    + suffix
                )
                QListWidgetItem(text, self.token_list)
                self._token_rows.append((token, None))
            for cert in token.certificates:
                problem = cert.info.problem()
                text = f"{_certificate_text(cert.info)}\nOn {_token_name(token)}" + suffix
                if problem:
                    text += f"\n{problem}"
                item = QListWidgetItem(text, self.token_list)
                self._token_rows.append((token, cert))
                if problem:
                    _disable_item(item)
        if listed and not tokens:
            self.token_status.setText(
                "No token found. Plug it in, wait until its software sees it, and press List."
            )
        elif listed:
            self.token_status.setText("")
        if logged_in is not None:
            select = (logged_in.serial, logged_in.slot_id)
        self._select_first_row(select)
        self._token_selected()

    def _select_first_row(self, prefer: tuple[str, int] | None) -> None:
        """On the preferred token if any, and only on it. Otherwise a usable certificate, else
        the first token to log in to."""
        rows = list(enumerate(self._token_rows))
        if prefer is not None:
            preferred = [r for r in rows if (r[1][0].serial, r[1][0].slot_id) == prefer]
            rows = preferred or rows
        for row, (_token, cert) in rows:
            if cert is not None and not cert.info.problem():
                self.token_list.setCurrentRow(row)
                return
        for row, (_token, cert) in rows:
            if cert is None:
                self.token_list.setCurrentRow(row)
                return

    def _selected_token(self) -> tuple[TokenInfo, TokenCertificate | None] | None:
        row = self.token_list.currentRow()
        return self._token_rows[row] if 0 <= row < len(self._token_rows) else None

    def _token_selected(self) -> None:
        selected = self._selected_token()
        token = selected[0] if selected is not None else None
        protected = token is not None and token.protected_auth
        locked = token is not None and token.pin_state is PinState.LOCKED
        self.pin.setVisible(not protected)
        self.pin_label.setVisible(not protected)
        self.pin.setEnabled(not locked)
        if locked:
            self.pin.clear()
        hidden = selected is not None and selected[1] is None
        self.login_button.setVisible(hidden and not locked)
        self.login_button.setEnabled(hidden and not locked and not self._busy)
        warning = ""
        if protected:
            warning = "Enter the PIN on the device when it asks."
        if token is not None:
            warning = PIN_WARNING.get(token.pin_state, warning)
            rejected = (token.serial, token.slot_id) in self.rejected_tokens
            if rejected and token.pin_state is not PinState.LOCKED:
                warning = REJECTED_WARNING
        self.pin_warning.setText(warning)
        self.pin_warning.setVisible(bool(warning))

    # -- PFX ------------------------------------------------------------------------------
    def _browse_pfx(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Certificate File", self.pfx_path.text(), "Certificate files (*.pfx *.p12)"
        )
        if chosen:
            self.pfx_path.setText(chosen)

    # -- result ---------------------------------------------------------------------------
    def problem(self) -> str:
        """Why the form can't be used yet ("" if it can)."""
        source = self.current_source()
        if source == SOURCE_STORE and self._selected_store() is None:
            return "Select a certificate."
        if source == SOURCE_TOKEN:
            selected = self._selected_token()
            if selected is None:
                return "Select a certificate on the token. Press List to read the tokens."
            token, cert = selected
            if token.pin_state is PinState.LOCKED:
                return PIN_WARNING[PinState.LOCKED]
            if cert is None:
                return "This token shows no certificates yet: log in to it, or select another."
            if cert.info.problem():
                return "Select a certificate on the token. Press List to read the tokens."
            if not token.protected_auth and not self.pin.text():
                return "Enter the token's PIN."
        if source == SOURCE_FILE and not Path(self.pfx_path.text().strip() or "?").is_file():
            return "Choose the certificate file."
        if self.use_timestamp.isChecked() and not self.tsa_url.text().strip():
            return "Enter the address of the timestamp server, or turn the timestamp off."
        return ""

    def primary_clicked(self) -> None:
        if self._busy:
            return
        problem = self.problem()
        if not problem and self.current_source() == SOURCE_FILE:
            problem = self._check_pfx()
        self.show_error(problem)
        if problem:
            return
        if self.current_source() == SOURCE_TOKEN:
            selected = self._selected_token()
            final_try = selected is not None and selected[0].pin_state is PinState.FINAL_TRY
            if final_try and not self.confirm_final_try():
                return
        self._remember()
        self.accept()

    def _check_pfx(self) -> str:
        from pdfeditor.services.signing import pfx_certificate

        key = PfxKey(Path(self.pfx_path.text().strip()), self.pfx_password.text())
        self._set_busy(True)
        try:
            info = run_modal(self, "Opening the certificate…", lambda _job: pfx_certificate(key))
        except RuntimeError as exc:
            return str(exc)
        finally:
            self._set_busy(False)
        return info.problem() if isinstance(info, CertificateInfo) else "Cancelled."

    def _selected_store(self) -> CertificateInfo | None:
        item = self.store_list.currentItem()
        info = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return info if isinstance(info, CertificateInfo) and not info.problem() else None

    def _remember(self) -> None:
        """Settings the next signature starts from. Never a PIN or password."""
        s = self.settings
        s.setValue("signing/source", self.current_source())
        s.setValue("signing/location", self.location.text().strip())
        s.setValue("signing/tsa_url", self.tsa_url.text().strip())
        s.setValue("signing/use_tsa", "true" if self.use_timestamp.isChecked() else "false")
        if self.current_source() == SOURCE_FILE:
            s.setValue("signing/pfx_path", self.pfx_path.text().strip())

    def key(self, parent_window: int = 0) -> KeyRef:
        source = self.current_source()
        if source == SOURCE_STORE:
            info = self._selected_store()
            assert info is not None
            return WindowsStoreKey(info.thumbprint, parent_window)
        if source == SOURCE_TOKEN:
            selected = self._selected_token()
            assert selected is not None
            token, cert = selected
            assert cert is not None
            return Pkcs11Key(
                module=self.module.text().strip(),
                slot_id=token.slot_id,
                token_serial=token.serial,
                cert_id=cert.cert_id,
                certificate=cert.info.der,
                cert_label=cert.label,
                pin=None if token.protected_auth else self.pin.text(),
                protected_auth=token.protected_auth,
                # The yes was for a token shown on its final try, not for whichever comes next.
                allow_final_try=(
                    self._final_try_confirmed and token.pin_state is PinState.FINAL_TRY
                ),
            )
        return PfxKey(Path(self.pfx_path.text().strip()), self.pfx_password.text())

    def options(self) -> SignatureOptions:
        field_name = self.field_choice.currentData() if self.field_choice is not None else None
        return SignatureOptions(
            placement=self.placement,
            reason=self.reason.text().strip(),
            location=self.location.text().strip(),
            tsa_url=self.tsa_url.text().strip() if self.use_timestamp.isChecked() else "",
            field_name=field_name if isinstance(field_name, str) else None,
        )

    def clear_secrets(self) -> None:
        self.pin.clear()
        self.pfx_password.clear()
