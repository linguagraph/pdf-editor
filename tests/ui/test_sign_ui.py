"""The Sign flow in the window. Keys are throwaway PFX files or fakes; the real Windows store is
only read if at all, and never signed with (see tests/services/test_signing_windows.py)."""

from __future__ import annotations

import datetime as dt
import shutil
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from pdfeditor.core.commands import AddAnnotationCommand
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.signing import (
    CertificateInfo,
    PfxKey,
    PinState,
    Pkcs11Key,
    SignaturePlacement,
    SignResult,
    TokenCertificate,
    TokenInfo,
    WindowsStoreKey,
)
from pdfeditor.services.signing import WrongPin, windows_store
from pdfeditor.ui import sign_controller
from pdfeditor.ui.dialogs.sign import (
    NO_CERTIFICATES,
    SOURCE_FILE,
    SOURCE_STORE,
    SOURCE_TOKEN,
    SignDialog,
)
from pdfeditor.ui.jobs import wait_for
from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui

FUTURE = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
PAST = dt.datetime(2001, 1, 1, tzinfo=dt.UTC)


def certificate(
    name: str = "Good Signer", until: dt.datetime = FUTURE, **kwargs
) -> CertificateInfo:
    return CertificateInfo(
        name, "Some CA", PAST, until, name.encode().hex().upper().ljust(40, "0"), True, **kwargs
    )


def token_info(state: PinState = PinState.OK, protected: bool = False, until=FUTURE) -> TokenInfo:
    cert = TokenCertificate(certificate("Token Signer", until, hardware=True), b"\x01", "sig")
    return TokenInfo(7, "My Token", "Vendor", "SER1", protected, state, (cert,))


def hidden_token(
    serial: str = "SER1", slot: int = 7, state: PinState = PinState.OK, protected: bool = False
) -> TokenInfo:
    return TokenInfo(slot, f"Token {serial}", "Vendor", serial, protected, state)


def token_dialog(window: MainWindow, *tokens: TokenInfo) -> SignDialog:
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_TOKEN))
    dialog.module.setText("C:/vendor/pkcs11.dll")
    dialog.set_tokens(list(tokens))
    return dialog


@pytest.fixture
def window(qtbot, monkeypatch):
    monkeypatch.setattr(windows_store, "list_certificates", list)  # never the real store here
    w = MainWindow()
    w.resize(1200, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def view(window: MainWindow, fixture_pdf, tmp_path: Path):
    path = tmp_path / "contract.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), path)
    v = window.open_path(path)
    v.set_zoom(1.0)
    v.go_to_page(0, record=False)
    return v


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        sign_controller.QMessageBox,
        "warning",
        lambda _p, _t, text: seen.append(text),
    )
    return seen


def pfx_dialog(window: MainWindow, pfx: Path, password: str = "test", placement=None) -> SignDialog:
    dialog = SignDialog(placement, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_FILE))
    dialog.pfx_path.setText(str(pfx))
    dialog.pfx_password.setText(password)
    dialog.use_timestamp.setChecked(False)
    return dialog


def test_ribbon_and_palette_entries(window: MainWindow) -> None:
    from pdfeditor.ui.action_help import HELP
    from pdfeditor.ui.shortcuts import action_id

    buttons = window.ribbon.button_actions()
    assert window.signing.act_place in buttons and window.signing.act_sign in buttons
    for action in (window.signing.act_place, window.signing.act_sign):
        assert not action.icon().isNull()
        assert action.property("needs_doc")
        assert action_id(action) in HELP


def test_signs_with_a_pfx_and_opens_the_result(
    window: MainWindow, view, throwaway_pfx, signature_status, tmp_path: Path, qtbot
) -> None:
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.reason.setText("Agreed")
    dialog.primary_clicked()
    assert dialog.result()
    source_bytes = view.session.path.read_bytes()
    target = tmp_path / "contract-signed.pdf"
    outcome = wait_for(window.signing.sign(dialog=dialog, target=target))
    assert isinstance(outcome, SignResult)
    assert dialog.pfx_password.text() == "", "secrets don't outlive the dialog"
    (status,) = signature_status(target)
    assert status.intact and status.valid
    assert target.read_bytes().startswith(source_bytes)
    assert window.current_view().session.path == target.resolve()  # the result is opened
    with window.current_view().session.lock:
        assert window.current_view().session.document.info().has_signatures
    assert "Editing the content" in window.signing.last_message
    assert view.session.path.read_bytes() == source_bytes, "the original isn't touched"


def test_default_target_name_and_visible_placement_flow(
    window: MainWindow, view, throwaway_pfx, signature_status, tmp_path: Path, qtbot, monkeypatch
) -> None:
    placements: list[SignaturePlacement | None] = []
    monkeypatch.setattr(window.signing, "sign", lambda p=None: placements.append(p))
    window.signing.act_place.trigger()
    assert window.current_tool == "place_signature"
    a = view.mapFromScene(view.page_point_to_scene(0, Point(70, 250)))
    b = view.mapFromScene(view.page_point_to_scene(0, Point(300, 320)))
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=a)
    qtbot.mouseMove(view.viewport(), pos=b)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=b)
    qtbot.waitUntil(lambda: bool(placements))
    (placement,) = placements
    assert placement.page == 0
    assert placement.rect.x0 == pytest.approx(70, abs=2) and placement.rect.y1 == pytest.approx(
        320, abs=2
    )
    assert window.current_tool == "select"
    # too small a box only says so
    placements.clear()
    window.signing.placed(0, Rect(10, 10, 20, 20))
    qtbot.wait(5)
    assert placements == []
    # and the dialog proposes <name>-signed.pdf
    chosen: list[str] = []

    def file_dialog(_parent, _title, start, _filter):
        chosen.append(start)
        return "", ""

    monkeypatch.setattr(sign_controller.QFileDialog, "getSaveFileName", file_dialog)
    assert window.signing.choose_target(view.session.path) is None
    assert chosen == [str(view.session.path.with_name("contract-signed.pdf"))]


def test_visible_signature_through_the_controller(
    window: MainWindow, view, throwaway_pfx, signature_status, tmp_path: Path
) -> None:
    placement = SignaturePlacement(1, Rect(60, 60, 320, 130))
    dialog = pfx_dialog(window, throwaway_pfx(), placement=placement)
    assert "page 2" in dialog.header_subtitle.text()
    dialog.primary_clicked()
    target = tmp_path / "v.pdf"
    outcome = wait_for(window.signing.sign(placement, dialog=dialog, target=target))
    assert isinstance(outcome, SignResult)
    assert signature_status(target)[0].valid


def test_unsaved_changes_must_be_saved_first(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    note = AnnotationModel(AnnotationType.SQUARE, 0, Rect(10, 10, 60, 60))
    view.session.execute(AddAnnotationCommand(note))
    assert view.session.is_dirty
    asked: list[str] = []

    def question(_p, _t, text, *_a, **_k):
        asked.append(text)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(sign_controller.QMessageBox, "question", question)
    assert window.signing.sign(dialog=pfx_dialog(window, throwaway_pfx())) is None
    assert "save it first" in asked[0]
    # agreeing saves, then signs what's now on disk
    monkeypatch.setattr(
        sign_controller.QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Save,
    )
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.primary_clicked()
    target = tmp_path / "saved.pdf"
    outcome = wait_for(window.signing.sign(dialog=dialog, target=target))
    assert isinstance(outcome, SignResult)
    assert not view.session.is_dirty


def test_wrong_pfx_password_stays_in_the_dialog(window: MainWindow, view, throwaway_pfx) -> None:
    dialog = pfx_dialog(window, throwaway_pfx(), password="wrong")
    dialog.primary_clicked()
    assert not dialog.result()
    assert "password is wrong" in dialog.error.text()


def test_expired_pfx_is_refused_in_the_dialog(window: MainWindow, view, throwaway_pfx) -> None:
    dialog = pfx_dialog(window, throwaway_pfx(days=-2))
    dialog.primary_clicked()
    assert not dialog.result()
    assert "expired" in dialog.error.text()


def test_store_list_marks_expired_and_returns_the_chosen_key(
    window: MainWindow, view, monkeypatch
) -> None:
    good = certificate(hardware=True)
    old = certificate("Old Signer", PAST + dt.timedelta(days=1))
    monkeypatch.setattr(windows_store, "available", lambda: True)
    monkeypatch.setattr(windows_store, "list_certificates", lambda: [old, good])
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_STORE))
    dialog.refresh_store()
    assert dialog.store_list.count() == 2
    first, second = dialog.store_list.item(0), dialog.store_list.item(1)
    assert "expired" in first.text() and not (first.flags() & Qt.ItemFlag.ItemIsEnabled)
    assert "USB token or smart card" in second.text() and "Good Signer" in second.text()
    assert dialog.store_list.currentItem() is second, "the first usable one is preselected"
    assert dialog.problem() == ""
    key = dialog.key(4242)
    assert isinstance(key, WindowsStoreKey) and key.thumbprint == good.thumbprint
    assert key.parent_window == 4242


def test_no_certificates_points_to_the_other_routes(window: MainWindow, view, monkeypatch) -> None:
    monkeypatch.setattr(windows_store, "available", lambda: True)
    monkeypatch.setattr(windows_store, "list_certificates", list)
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_STORE))
    dialog.refresh_store()
    assert dialog.store_empty.text() == NO_CERTIFICATES
    assert not dialog.store_empty.isHidden() and dialog.store_list.isHidden()
    assert (
        "PKCS#11" in NO_CERTIFICATES
        and "PFX" in NO_CERTIFICATES
        and "middleware" in NO_CERTIFICATES
    )
    assert dialog.problem() == "Select a certificate."


def test_store_route_is_hidden_off_windows(window: MainWindow, view, monkeypatch) -> None:
    monkeypatch.setattr(windows_store, "available", lambda: False)
    dialog = SignDialog(None, window)
    assert dialog.source.findData(SOURCE_STORE) == -1
    assert dialog.source.findData(SOURCE_TOKEN) >= 0


def test_token_route_needs_pin_and_warns_about_the_counter(window: MainWindow, view) -> None:
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_TOKEN))
    assert "Select a certificate" in dialog.problem()
    dialog.module.setText("vendor.dll")
    dialog.set_tokens([token_info()])
    assert dialog.problem() == "Enter the token's PIN."
    assert dialog.pin_warning.isHidden()
    dialog.pin.setText("1234")
    assert dialog.problem() == ""
    key = dialog.key()
    assert isinstance(key, Pkcs11Key)
    assert (key.module, key.slot_id, key.token_serial, key.cert_id, key.pin) == (
        "vendor.dll",
        7,
        "SER1",
        b"\x01",
        "1234",
    )
    assert "1234" not in repr(key)
    assert dialog.pin.echoMode() == dialog.pin.EchoMode.Password
    dialog.clear_secrets()
    assert dialog.pin.text() == ""

    dialog.set_tokens([token_info(PinState.FINAL_TRY)])
    assert "one more wrong PIN" in dialog.pin_warning.text()
    assert "one more wrong PIN" in dialog.token_list.item(0).text()
    dialog.set_tokens([token_info(PinState.LOCKED)])
    assert "locked" in dialog.pin_warning.text()
    assert not dialog.pin.isEnabled(), "a locked token isn't asked for a PIN"
    assert "locked" in dialog.problem()


@pytest.mark.parametrize(
    ("state", "text"), [(PinState.FINAL_TRY, "one more wrong PIN"), (PinState.LOCKED, "locked")]
)
def test_pin_state_shows_even_while_certificates_are_hidden(
    window: MainWindow, view, state: PinState, text: str
) -> None:
    dialog = token_dialog(window, hidden_token(state=state))
    assert text in dialog.token_list.item(0).text()
    assert text in dialog.pin_warning.text()
    if state is PinState.LOCKED:
        assert dialog.login_button.isHidden() and not dialog.pin.isEnabled()
        assert "locked" in dialog.problem()


def test_pin_state_shows_per_token(window: MainWindow, view) -> None:
    dialog = token_dialog(
        window,
        hidden_token("A", 0),
        hidden_token("B", 1, PinState.FINAL_TRY),
        hidden_token("C", 2, PinState.LOCKED),
    )
    assert dialog.token_list.count() == 3
    assert "one more wrong" not in dialog.token_list.item(0).text()
    assert "one more wrong" in dialog.token_list.item(1).text()
    assert "locked" in dialog.token_list.item(2).text()


def test_final_try_token_needs_confirmation_at_sign(window: MainWindow, view, monkeypatch) -> None:
    dialog = token_dialog(window, token_info(PinState.FINAL_TRY))
    dialog.pin.setText("1")
    answers: list[bool] = []

    def confirm() -> bool:
        answers.append(False)
        return False

    monkeypatch.setattr(dialog, "confirm_final_try", confirm)
    dialog.primary_clicked()
    assert answers == [False] and not dialog.result()
    dialog.pin.clear()
    plain = token_dialog(window, token_info())
    plain.pin.setText("1")
    plain.primary_clicked()
    assert plain.result() and plain.key().allow_final_try is False


def test_token_with_pin_pad_asks_for_no_pin(window: MainWindow, view) -> None:
    dialog = token_dialog(window, token_info(protected=True))
    assert dialog.pin.isHidden()
    assert "on the device" in dialog.pin_warning.text()
    assert dialog.problem() == ""
    key = dialog.key()
    assert isinstance(key, Pkcs11Key) and key.protected_auth and key.pin is None


def test_expired_certificate_on_a_token_cant_be_chosen(window: MainWindow, view) -> None:
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_TOKEN))
    dialog.set_tokens([token_info(until=PAST + dt.timedelta(days=1))])
    assert "Select a certificate" in dialog.problem()


def test_listing_never_sends_a_pin(window: MainWindow, view, monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_list(*args, **kwargs) -> list[TokenInfo]:
        calls.append(args + tuple(kwargs.values()))
        return [hidden_token()]

    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.list_tokens", fake_list)
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_TOKEN))
    dialog.module.setText("C:/vendor/pkcs11.dll")
    dialog.pin.setText("9999")
    dialog.list_token_certificates()
    dialog.list_token_certificates()
    assert calls == [("C:/vendor/pkcs11.dll",)] * 2, "listing takes no PIN at all"
    assert "after login" in dialog.token_list.item(0).text()


def test_login_goes_to_the_selected_token_once(window: MainWindow, view, monkeypatch) -> None:
    sent: list[tuple] = []

    def fake_login(module, slot, serial, pin, **kwargs):
        sent.append((slot, serial, pin))
        cert = TokenCertificate(certificate("Hidden Signer", hardware=True), b"\x01", "sig")
        return TokenInfo(slot, f"Token {serial}", "Vendor", serial, False, PinState.OK, (cert,))

    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.login_and_list", fake_login)
    dialog = token_dialog(window, hidden_token("A", 0), hidden_token("B", 1))
    assert dialog.problem().startswith("This token shows no certificates")
    dialog.token_list.setCurrentRow(1)
    dialog.pin.setText("2222")
    dialog.log_in_to_selected()
    assert sent == [(1, "B", "2222")], "only the selected token gets the PIN"
    assert dialog.token_list.count() == 2
    selected = dialog._selected_token()
    assert selected[0].serial == "B" and selected[1] is not None
    assert dialog.problem() == "", "the PIN stays for the signature"


def test_wrong_pin_at_login_clears_the_field_and_stays_on_that_token(
    window: MainWindow, view, monkeypatch
) -> None:
    sent: list[str] = []

    def fake_login(_module, _slot, _serial, pin, **_kwargs):
        sent.append(pin)
        raise WrongPin("The token rejected the PIN.")

    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.login_and_list", fake_login)
    monkeypatch.setattr(
        "pdfeditor.ui.dialogs.sign.list_tokens",
        lambda _module: [hidden_token("A", 0), hidden_token("B", 1, PinState.COUNT_LOW)],
    )
    dialog = token_dialog(window, hidden_token("A", 0), hidden_token("B", 1))
    dialog.token_list.setCurrentRow(1)
    dialog.pin.setText("0000")
    dialog.log_in_to_selected()
    assert sent == ["0000"]
    assert dialog.pin.text() == "", "another press can't resend a rejected PIN"
    assert "rejected" in dialog.error.text()
    assert dialog._selected_token()[0].serial == "B", "the next PIN mustn't land on another token"
    assert ("B", 1) in dialog.rejected_tokens
    assert "rejected the last PIN" in dialog.pin_warning.text()
    dialog.log_in_to_selected()  # pressed again, with the field empty
    assert sent == ["0000"]
    assert "Enter the token's PIN" in dialog.error.text()


def test_login_at_final_try_needs_a_yes(window: MainWindow, view, monkeypatch) -> None:
    sent: list[tuple] = []

    def fake_login(_module, slot, _serial, pin, **kwargs):
        sent.append((pin, kwargs["allow_final_try"]))
        return hidden_token("A", slot, PinState.FINAL_TRY)

    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.login_and_list", fake_login)
    dialog = token_dialog(window, hidden_token("A", 0, PinState.FINAL_TRY))
    dialog.pin.setText("1111")
    monkeypatch.setattr(dialog, "confirm_final_try", lambda: False)
    dialog.log_in_to_selected()
    assert sent == []
    monkeypatch.setattr(dialog, "confirm_final_try", lambda: True)
    dialog.log_in_to_selected()
    assert sent == [("1111", True)]


def test_pin_pad_token_logs_in_on_the_device(window: MainWindow, view, monkeypatch) -> None:
    sent: list[tuple] = []

    def fake_login(_module, slot, serial, pin, **kwargs):
        sent.append((pin, kwargs["protected_auth"]))
        return token_info(protected=True)

    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.login_and_list", fake_login)
    dialog = token_dialog(window, hidden_token(protected=True))
    assert not dialog.login_button.isHidden() and dialog.pin.isHidden()
    dialog.log_in_to_selected()
    assert sent == [(None, True)]
    assert dialog.problem() == ""


def test_double_click_on_login_sends_the_pin_once(
    window: MainWindow, view, monkeypatch, qtbot
) -> None:
    """The progress dialog only turns modal after 300 ms: a real double-click gets through."""
    import time

    sent: list[str] = []

    def slow_login(_module, _slot, _serial, pin, **_kwargs):
        sent.append(pin)
        time.sleep(0.25)
        raise WrongPin("rejected")

    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.login_and_list", slow_login)
    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.list_tokens", lambda _m: [hidden_token()])
    dialog = token_dialog(window, hidden_token())
    dialog.pin.setText("9999")
    dialog.show()
    qtbot.waitExposed(dialog)

    def click() -> None:
        qtbot.mouseClick(dialog.login_button, Qt.MouseButton.LeftButton)

    QTimer.singleShot(0, click)
    QTimer.singleShot(120, click)
    qtbot.wait(800)
    dialog.close()
    assert sent == ["9999"]


def test_buttons_are_disabled_while_the_token_is_busy(
    window: MainWindow, view, monkeypatch
) -> None:
    seen: list[tuple[bool, bool, bool]] = []
    dialog = token_dialog(window, hidden_token())

    def observe(_module):
        seen.append(
            (dialog.list_button.isEnabled(), dialog.browse_button.isEnabled(), dialog._busy)
        )
        return []

    logins: list[str] = []
    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.list_tokens", observe)
    monkeypatch.setattr(
        "pdfeditor.ui.dialogs.sign.login_and_list", lambda *a, **_k: logins.append(a[3])
    )
    dialog.pin.setText("1234")
    dialog.list_token_certificates()
    dialog.set_tokens([hidden_token()])
    assert seen == [(False, False, True)]
    assert dialog.list_button.isEnabled() and not dialog._busy
    # re-entry is refused while busy, from any of the paths that reach the token
    dialog._busy = True
    dialog.list_token_certificates()
    dialog.log_in_to_selected()
    dialog.primary_clicked()
    assert len(seen) == 1 and logins == [] and not dialog.result()
    dialog._busy = False


def test_browse_never_sends_the_typed_pin(window: MainWindow, view, monkeypatch) -> None:
    listed: list[tuple] = []
    logins: list[str] = []

    def fake_list(*args):
        listed.append(args)
        return [hidden_token()]

    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.list_tokens", fake_list)
    monkeypatch.setattr(
        "pdfeditor.ui.dialogs.sign.login_and_list", lambda *a, **k: logins.append(a[3])
    )
    monkeypatch.setattr(
        "pdfeditor.ui.dialogs.sign.QFileDialog.getOpenFileName",
        lambda *_a, **_k: ("C:/other/vendor.dll", ""),
    )
    dialog = token_dialog(window, hidden_token())
    dialog.pin.setText("1234")
    dialog._browse_module()
    assert listed == [("C:/other/vendor.dll",)], "a public listing, and no more"
    assert logins == []
    assert dialog.pin.text() == "", "a PIN typed for the old module goes away with it"


def test_editing_the_module_drops_what_was_listed(window: MainWindow, view) -> None:
    dialog = token_dialog(window, token_info())
    dialog.pin.setText("1")
    assert dialog.problem() == ""
    dialog.module.setText("C:/vendorB/b.dll")
    assert dialog.token_list.count() == 0 and dialog._token_rows == []
    assert dialog.pin.text() == ""
    assert "Select a certificate" in dialog.problem(), "rows from the old module can't be used"


def test_module_path_is_remembered_but_never_the_pin(window: MainWindow, view, monkeypatch) -> None:
    monkeypatch.setattr("pdfeditor.ui.dialogs.sign.list_tokens", lambda *_a: [token_info()])
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_TOKEN))
    dialog.module.setText("C:/vendor/pkcs11.dll")
    dialog.list_token_certificates()
    dialog.pin.setText("SECRET-PIN")
    dialog.primary_clicked()
    assert dialog.result()
    again = SignDialog(None, window)
    assert again.module.text() == "C:/vendor/pkcs11.dll"
    settings = dialog.settings
    assert not [k for k in settings.allKeys() if "SECRET-PIN" in str(settings.value(k))]


def test_wrong_pin_warns_on_retry_and_never_retries(
    window: MainWindow, view, tmp_path: Path, monkeypatch, warnings
) -> None:
    attempts: list[Pkcs11Key] = []

    def refuse(_source, _target, key, *_a, **_k):
        attempts.append(key)
        raise WrongPin("The token rejected the PIN.")

    monkeypatch.setattr(sign_controller, "sign_file", refuse)
    dialog = token_dialog(window, token_info())
    dialog.pin.setText("0000")
    dialog.accept()
    outcome = wait_for(window.signing.sign(dialog=dialog, target=tmp_path / "x.pdf"))
    assert isinstance(outcome, WrongPin)
    assert len(attempts) == 1, "no automatic second try: tokens lock"
    assert warnings == ["The token rejected the PIN."]
    assert window.signing.rejected_tokens == {("SER1", 7)}
    retry = SignDialog(None, window, rejected_tokens=window.signing.rejected_tokens)
    retry.source.setCurrentIndex(retry.source.findData(SOURCE_TOKEN))
    retry.set_tokens([token_info()])
    assert "rejected the last PIN" in retry.pin_warning.text()
    assert not (tmp_path / "x.pdf").exists()


class BlockingStoreKey:
    """A store key whose signature waits, as a PIN prompt does. Signs with a throwaway key."""

    def __init__(self, pfx: Path) -> None:
        from cryptography.hazmat.primitives.serialization import Encoding, pkcs12

        self.key, cert, _o = pkcs12.load_key_and_certificates(pfx.read_bytes(), b"test")
        self.der = cert.public_bytes(Encoding.DER)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock_owned_in_key_call: bool | None = None

    def __enter__(self) -> BlockingStoreKey:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def chain(self) -> list[bytes]:
        return []

    def sign_digest(self, digest: bytes, _name: str) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

        from pdfeditor.core.engine_lock import ENGINE_LOCK

        self.lock_owned_in_key_call = ENGINE_LOCK._is_owned()
        self.entered.set()
        assert self.release.wait(30)
        return self.key.sign(digest, padding.PKCS1v15(), Prehashed(hashes.SHA256()))


def store_dialog(
    window: MainWindow, monkeypatch, info: CertificateInfo | None = None
) -> SignDialog:
    monkeypatch.setattr(windows_store, "available", lambda: True)
    monkeypatch.setattr(windows_store, "list_certificates", lambda: [info or certificate()])
    dialog = SignDialog(None, window)
    dialog.source.setCurrentIndex(dialog.source.findData(SOURCE_STORE))
    dialog.refresh_store()
    dialog.use_timestamp.setChecked(False)
    dialog.accept()
    return dialog


def test_engine_lock_is_free_while_the_key_waits(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, monkeypatch, qtbot
) -> None:
    """While Windows shows a PIN prompt the GUI needs the engine (to paint, to scroll)."""
    key = BlockingStoreKey(throwaway_pfx())
    monkeypatch.setattr(windows_store, "open_certificate", lambda _t, _w=0: key)
    target = tmp_path / "x.pdf"
    session = view.session
    job = window.signing.sign(dialog=store_dialog(window, monkeypatch), target=target)
    try:
        qtbot.waitUntil(key.entered.is_set, timeout=20_000)
        assert key.lock_owned_in_key_call is False, "the job's thread holds the engine lock"
        # the GUI thread, a different thread here, gets the lock and uses the document
        assert session.lock.acquire(timeout=5)
        try:
            assert session.document.page(0).rect.width > 0
        finally:
            session.lock.release()
        assert session.busy, "and the document stays read-only"
    finally:
        key.release.set()
    assert isinstance(wait_for(job), SignResult)
    assert not session.busy


def test_cancel_during_the_key_wait_writes_nothing(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, monkeypatch, qtbot
) -> None:
    """The real sign_file: Cancel can't interrupt the token, but its answer is thrown away."""
    key = BlockingStoreKey(throwaway_pfx())
    monkeypatch.setattr(windows_store, "open_certificate", lambda _t, _w=0: key)
    target = tmp_path / "x.pdf"
    job = window.signing.sign(dialog=store_dialog(window, monkeypatch), target=target)
    qtbot.waitUntil(key.entered.is_set, timeout=20_000)
    window.jobs.cancel(job)
    key.release.set()
    wait_for(job)
    assert "No file was written" in window.signing.last_message
    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_cancel_during_verification_writes_nothing(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    from pdfeditor.services.signing import _pdf

    real = _pdf.verify_output

    def cancel_then_verify(*args, **kwargs):
        for job in window.jobs.running():
            job.cancel()
        real(*args, **kwargs)

    monkeypatch.setattr(_pdf, "verify_output", cancel_then_verify)
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.primary_clicked()
    target = tmp_path / "x.pdf"
    wait_for(window.signing.sign(dialog=dialog, target=target))
    assert "No file was written" in window.signing.last_message
    assert not target.exists()


def test_result_wins_over_a_cancel_after_the_swap(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, monkeypatch, signature_status
) -> None:
    """The file is there: saying "cancelled, nothing written" would be false."""
    import os

    real = os.replace
    target = tmp_path / "x.pdf"

    def replace_then_cancel(a, b):
        real(a, b)
        if Path(b) == target.resolve():
            for job in window.jobs.running():
                job.cancel()

    monkeypatch.setattr("pdfeditor.core.safe_output.os.replace", replace_then_cancel)
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.primary_clicked()
    outcome = wait_for(window.signing.sign(dialog=dialog, target=target))
    assert isinstance(outcome, SignResult)
    assert signature_status(target)[0].valid
    assert "Signed by" in window.signing.last_message


def test_closing_the_tab_during_a_pin_prompt_doesnt_freeze(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, monkeypatch, qtbot
) -> None:
    key = BlockingStoreKey(throwaway_pfx())
    monkeypatch.setattr(windows_store, "open_certificate", lambda _t, _w=0: key)
    target = tmp_path / "x.pdf"
    job = window.signing.sign(dialog=store_dialog(window, monkeypatch), target=target)
    qtbot.waitUntil(key.entered.is_set, timeout=20_000)
    session = view.session
    started = time.monotonic()
    try:
        assert window.close_tab(window._tab_index(view), ask=False)
        assert time.monotonic() - started < 5, "the GUI thread waited for the PIN prompt"
        assert session.closed
    finally:
        key.release.set()
    qtbot.waitUntil(lambda: job.is_settled, timeout=20_000)
    assert not target.exists(), "cancelled by the close"


def test_ordinary_jobs_are_still_waited_for_on_close(window: MainWindow, view, qtbot) -> None:
    release = threading.Event()
    job = window.jobs.start("Reading", lambda _j: release.wait(0.5), session=view.session)
    qtbot.waitUntil(lambda: job.started, timeout=5_000)
    window.jobs.cancel_for(view.session)
    assert job.done, "a job that reads the document finishes before it's closed"


def test_target_problems_are_found_before_asking_for_a_signature(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, monkeypatch, warnings
) -> None:
    monkeypatch.setattr(
        sign_controller, "sign_file", lambda *_a, **_k: pytest.fail("must not reach the key")
    )
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.primary_clicked()
    assert window.signing.sign(dialog=dialog, target=view.session.path) is None
    assert "different file name" in warnings[-1]


def test_target_open_in_another_tab_is_refused(
    window: MainWindow, view, fixture_pdf, throwaway_pfx, tmp_path: Path, warnings
) -> None:
    other = tmp_path / "other.pdf"
    shutil.copy2(fixture_pdf("images"), other)
    first = view
    window.open_path(other)
    window.tabs.setCurrentIndex(window._tab_index(first))
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.primary_clicked()
    assert window.signing.sign(dialog=dialog, target=other) is None
    assert "open in a tab" in warnings[-1]


def test_certified_document_is_refused_before_the_dialog(
    window: MainWindow, fixture_pdf, throwaway_pfx, tmp_path: Path, warnings
) -> None:
    from pyhanko.sign.fields import MDPPerm

    from tests.services.test_signing import certified

    path = tmp_path / "locked.pdf"
    certified(fixture_pdf("text_multipage"), path, throwaway_pfx(), MDPPerm.NO_CHANGES)
    window.open_path(path)
    assert window.signing.sign() is None
    assert "certified" in warnings[-1]
    assert window.signing.dialog is None
    # one that allows signing refuses only a visible signature
    path2 = tmp_path / "formfill.pdf"
    certified(fixture_pdf("text_multipage"), path2, throwaway_pfx(), MDPPerm.FILL_FORMS)
    window.open_path(path2)
    assert window.signing.sign(SignaturePlacement(0, Rect(50, 50, 300, 110))) is None
    assert "invisible" in warnings[-1]


def test_encrypted_document_asks_for_its_password(
    window: MainWindow, fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "pdfeditor.ui.main_window.password_prompt", lambda *_a: lambda _attempt: "owner"
    )
    path = tmp_path / "enc.pdf"
    shutil.copy2(fixture_pdf("encrypted"), path)
    window.open_path(path)
    answers = iter(["wrong", "owner"])
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(window.signing, "ask_password", ask)
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.primary_clicked()
    target = tmp_path / "signed.pdf"
    outcome = wait_for(window.signing.sign(dialog=dialog, target=target))
    assert isinstance(outcome, SignResult)
    assert len(prompts) == 2 and "doesn't open" in prompts[1]
    assert signature_status(target, "owner")[0].valid


def test_file_changed_on_disk_is_refused_until_reopened(
    window: MainWindow, view, throwaway_pfx, tmp_path: Path, warnings
) -> None:
    import os

    path = view.session.path
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert view.session.file_changed_on_disk()
    dialog = pfx_dialog(window, throwaway_pfx())
    dialog.accept()
    assert window.signing.sign(dialog=dialog, target=tmp_path / "x.pdf") is None
    assert warnings and "changed outside" in warnings[-1]


def test_saving_doesnt_count_as_a_change_on_disk(window: MainWindow, view) -> None:
    note = AnnotationModel(AnnotationType.SQUARE, 0, Rect(10, 10, 60, 60))
    view.session.execute(AddAnnotationCommand(note))
    assert window.save(view)
    assert not view.session.file_changed_on_disk()


@pytest.mark.parametrize(
    ("typed", "name"),
    [
        ("Contract 1.5", "Contract 1.5.pdf"),
        ("report.v2", "report.v2.pdf"),
        ("plain", "plain.pdf"),
        ("kept.PDF", "kept.PDF"),
    ],
)
def test_target_name_keeps_what_was_typed(
    window: MainWindow, view, tmp_path: Path, monkeypatch, typed: str, name: str
) -> None:
    monkeypatch.setattr(
        sign_controller.QFileDialog,
        "getSaveFileName",
        lambda *_a, **_k: (str(tmp_path / typed), ""),
    )
    assert window.signing.choose_target(view.session.path) == tmp_path / name


def test_appended_name_that_exists_needs_a_yes(
    window: MainWindow, view, tmp_path: Path, monkeypatch
) -> None:
    existing = tmp_path / "Contract 1.5.pdf"
    existing.write_bytes(b"unrelated")
    monkeypatch.setattr(
        sign_controller.QFileDialog,
        "getSaveFileName",
        lambda *_a, **_k: (str(tmp_path / "Contract 1.5"), ""),
    )
    monkeypatch.setattr(window.signing, "confirm_overwrite", lambda _p: False)
    assert window.signing.choose_target(view.session.path) is None
    monkeypatch.setattr(window.signing, "confirm_overwrite", lambda _p: True)
    assert window.signing.choose_target(view.session.path) == existing


def test_unwritable_folder_is_found_before_the_key(
    window: MainWindow, view, tmp_path: Path
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    assert "can't be written" in window.signing._target_problem(
        view.session.path, blocker / "o.pdf"
    )


def test_unexpected_error_in_the_document_check_is_reported(
    window: MainWindow, view, monkeypatch, warnings
) -> None:
    def trips(*_a):
        raise RuntimeError("boom")

    monkeypatch.setattr(sign_controller, "preflight", trips)
    assert window.signing.sign() is None
    assert "can't be signed" in warnings[-1]


def test_second_click_while_the_flow_runs_is_ignored(window: MainWindow, view, monkeypatch) -> None:
    calls: list[int] = []

    def nested(*_a):
        calls.append(1)
        assert window.signing.sign() is None  # what a second click would do meanwhile
        return None

    monkeypatch.setattr(window.signing, "_saved_first", nested)
    assert window.signing.sign() is None
    assert calls == [1]


def test_keys_are_not_leaked_by_dataclass_repr(tmp_path: Path) -> None:
    assert "hunter2" not in repr(PfxKey(tmp_path / "a.pfx", "hunter2"))
