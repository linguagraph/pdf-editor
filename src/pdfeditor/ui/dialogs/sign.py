"""Sign Document dialog: choose a key (Windows store, PKCS#11 token or PFX file) and details.

The PIN and passwords live only in masked fields while the dialog is open, are never saved or
logged, and are cleared as soon as the signature is on its way.
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
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.signing import (
    CertificateInfo,
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
from pdfeditor.services.signing import SigningError, windows_store
from pdfeditor.services.signing.tokens import list_tokens
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


class SignDialog(FormDialog):
    def __init__(
        self,
        placement: SignaturePlacement | None,
        parent: QWidget | None = None,
        *,
        rejected_tokens: set[str] | None = None,
    ) -> None:
        subtitle = (
            f"A visible signature on page {placement.page + 1}. "
            if placement is not None
            else "An invisible signature: nothing is drawn on the pages. "
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
        self.rejected_tokens = rejected_tokens or set()
        self._token_rows: list[tuple[TokenInfo, TokenCertificate]] = []
        self._list_with_pin = False

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
        browse = QPushButton("&Browse…", page)
        browse.setAutoDefault(False)
        browse.clicked.connect(lambda _=False: self._browse_module())
        self.list_button = QPushButton("L&ist", page)
        self.list_button.setAutoDefault(False)
        self.list_button.clicked.connect(lambda _=False: self.list_token_certificates())
        row = QHBoxLayout()
        row.addWidget(self.module, 1)
        row.addWidget(browse)
        row.addWidget(self.list_button)
        add_row(
            form, "&Module:", row, "The vendor's PKCS#11 DLL (64-bit), usually in Program Files."
        )
        layout.addLayout(form)
        self.token_list = QListWidget(page)
        self.token_list.setAccessibleName("Certificates on the token")
        self.token_list.setWordWrap(True)
        self.token_list.currentRowChanged.connect(lambda _row: self._token_selected())
        layout.addWidget(self.token_list, 1)
        self.token_status = caption("", page)
        layout.addWidget(self.token_status)
        pin_form = form_layout()
        self.pin = _password_field()
        self.pin.setParent(page)
        self.pin_label = QLabel("&PIN:", page)
        self.pin_label.setBuddy(self.pin)
        pin_form.addRow(self.pin_label, self.pin)
        self.pin.setAccessibleName("Token PIN")
        layout.addLayout(pin_form)
        self.pin_warning = QLabel(page)
        self.pin_warning.setWordWrap(True)
        self.pin_warning.setProperty("role", "error")
        self.pin_warning.setVisible(False)
        layout.addWidget(self.pin_warning)
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
                item.setFlags(
                    item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable
                )
            elif not selected:
                self.store_list.setCurrentItem(item)
                selected = True
        self.store_empty.setVisible(not certificates)
        self.store_list.setVisible(bool(certificates))

    # -- PKCS#11 --------------------------------------------------------------------------
    def _browse_module(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "PKCS#11 Module", self.module.text(), "PKCS#11 modules (*.dll *.so *.dylib)"
        )
        if chosen:
            self.module.setText(chosen)
            self.list_token_certificates()

    def list_token_certificates(self) -> None:
        """List the certificates on the tokens. Sends the PIN only when the user pressed List
        again after typing it, because the first listing came back empty."""
        path = self.module.text().strip()
        self.show_error("")
        if not path:
            self.show_error("Choose the token's PKCS#11 module first.")
            return
        pin = self.pin.text() if self._list_with_pin and self.pin.text() else None
        try:
            tokens = run_modal(self, "Reading the token…", lambda _job: list_tokens(path, pin))
        except RuntimeError as exc:
            self.show_error(str(exc))
            return
        if tokens is None:  # cancelled
            return
        assert isinstance(tokens, list)
        self.settings.setValue("signing/pkcs11_module", path)
        self.set_tokens(tokens, logged_in=pin is not None)

    def set_tokens(self, tokens: list[TokenInfo], logged_in: bool = False) -> None:
        self.token_list.clear()
        self._token_rows = []
        for token in tokens:
            for cert in token.certificates:
                problem = cert.info.problem()
                text = f"{_certificate_text(cert.info)}\nOn {token.label or token.serial}"
                if problem:
                    text += f"\n{problem}"
                item = QListWidgetItem(text, self.token_list)
                self._token_rows.append((token, cert))
                if problem:
                    item.setFlags(
                        item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable
                    )
        self._list_with_pin = False
        if not tokens:
            self.token_status.setText(
                "No token found. Plug it in, wait until its software sees it, and press List."
            )
        elif not self._token_rows and not logged_in:
            self._list_with_pin = True
            self.token_status.setText(
                "This token shows its certificates only after login. Enter the PIN and press "
                "List: that sends the PIN to the token once."
            )
        elif not self._token_rows:
            self.token_status.setText("No certificate for signing was found on this token.")
        else:
            self.token_status.setText("")
            for row, (_token, cert) in enumerate(self._token_rows):
                if not cert.info.problem():
                    self.token_list.setCurrentRow(row)
                    break
        self._token_selected()

    def _selected_token(self) -> tuple[TokenInfo, TokenCertificate] | None:
        row = self.token_list.currentRow()
        return self._token_rows[row] if 0 <= row < len(self._token_rows) else None

    def _token_selected(self) -> None:
        selected = self._selected_token()
        protected = selected is not None and selected[0].protected_auth
        self.pin.setVisible(not protected)
        self.pin_label.setVisible(not protected)
        warning = ""
        if protected:
            warning = "Enter the PIN on the device when it asks."
        if selected is not None:
            token = selected[0]
            warning = PIN_WARNING.get(token.pin_state, warning)
            if token.serial in self.rejected_tokens and token.pin_state is not PinState.LOCKED:
                warning = (
                    "The token rejected the last PIN. Check it before signing again: tokens lock "
                    "after a few failures, and this app never retries on its own."
                )
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
            if selected is None or selected[1].info.problem():
                return "Select a certificate on the token. Press List to read the token."
            token = selected[0]
            if token.pin_state is PinState.LOCKED:
                return PIN_WARNING[PinState.LOCKED]
            if not token.protected_auth and not self.pin.text():
                return "Enter the token's PIN."
        if source == SOURCE_FILE and not Path(self.pfx_path.text().strip() or "?").is_file():
            return "Choose the certificate file."
        if self.use_timestamp.isChecked() and not self.tsa_url.text().strip():
            return "Enter the address of the timestamp server, or turn the timestamp off."
        return ""

    def primary_clicked(self) -> None:
        problem = self.problem()
        if not problem and self.current_source() == SOURCE_FILE:
            problem = self._check_pfx()
        self.show_error(problem)
        if problem:
            return
        self._remember()
        self.accept()

    def _check_pfx(self) -> str:
        from pdfeditor.services.signing import pfx_certificate

        key = PfxKey(Path(self.pfx_path.text().strip()), self.pfx_password.text())
        try:
            info = run_modal(self, "Opening the certificate…", lambda _job: pfx_certificate(key))
        except RuntimeError as exc:
            return str(exc)
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
            return Pkcs11Key(
                module=self.module.text().strip(),
                slot_id=token.slot_id,
                token_serial=token.serial,
                cert_id=cert.cert_id,
                cert_label=cert.label,
                pin=None if token.protected_auth else self.pin.text(),
                protected_auth=token.protected_auth,
            )
        return PfxKey(Path(self.pfx_path.text().strip()), self.pfx_password.text())

    def options(self) -> SignatureOptions:
        return SignatureOptions(
            placement=self.placement,
            reason=self.reason.text().strip(),
            location=self.location.text().strip(),
            tsa_url=self.tsa_url.text().strip() if self.use_timestamp.isChecked() else "",
        )

    def clear_secrets(self) -> None:
        self.pin.clear()
        self.pfx_password.clear()
