from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel

from pdfeditor.engine.base import PasswordRequired
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.metadata import EncryptionMethod
from pdfeditor.ui.dialogs import confirm
from pdfeditor.ui.dialogs.properties import PropertiesDialog
from pdfeditor.ui.dialogs.security import SecurityDialog
from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1000, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


def test_dialog_validation(window: MainWindow) -> None:
    d = SecurityDialog(parent=window)
    assert "open password" in d.problem()
    d.require_open.setChecked(True)
    d.user_pw.setText("a")
    d.user_pw2.setText("b")
    assert "don't match" in d.problem()
    d.user_pw2.setText("a")
    assert d.problem() == ""
    s = d.settings()
    assert s.user_password == "a" and s.owner_password == "a" and s.permissions.print
    d.restrict.setChecked(True)
    d.owner_pw.setText("a")
    d.owner_pw2.setText("a")
    assert "must differ" in d.problem()
    d.owner_pw.setText("boss")
    d.owner_pw2.setText("boss")
    d.allow_print.setChecked(False)
    d.method.setCurrentIndex(1)
    s = d.settings()
    assert s.method is EncryptionMethod.AES_128 and not s.permissions.print
    assert s.owner_password == "boss"
    d.close()


def test_encrypt_then_save(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("report"), path)
    view = window.open_path(path)
    d = SecurityDialog(parent=window)
    d.require_open.setChecked(True)
    d.user_pw.setText("secret")
    d.user_pw2.setText("secret")
    d.accept()
    assert window.protect.encrypt(d)
    assert view.session.is_dirty and "when you save" in window.protect.last_message
    props = PropertiesDialog(view.session, window)
    labels = [lbl.text() for lbl in props.findChildren(QLabel)]
    assert "Security will be set to aes-256 with an open password" in labels
    props.close()
    window.act_undo.trigger()
    assert view.session.document.pending_security() is None
    window.act_redo.trigger()
    view.session.save()
    with pytest.raises(PasswordRequired):
        get_engine().open(path)
    get_engine().open(path, "secret").close()


def test_remove_security_needs_owner_password(
    window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "enc.pdf"
    shutil.copy2(fixture_pdf("encrypted"), path)
    monkeypatch.setattr(
        "pdfeditor.ui.main_window.password_prompt", lambda *_a: lambda _attempt: "user"
    )
    view = window.open_path(path)
    warned: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.protect_controller.QMessageBox.warning",
        lambda _p, _t, msg: warned.append(msg),
    )
    assert not window.protect.remove_security(owner_password="wrong")
    assert warned and "not correct" in warned[0]
    # the wrong password didn't disturb the open document
    assert view.session.document.page(0).text_page(with_chars=False).text.strip()
    asked: list[str] = []
    monkeypatch.setattr(confirm, "confirm_destructive", lambda _p, _t, text, _a: asked.append(text))
    assert not window.protect.remove_security(owner_password="owner")  # declined
    assert asked and "anyone can open" in asked[0]
    monkeypatch.setattr(confirm, "confirm_destructive", lambda *_a: True)
    assert window.protect.remove_security(owner_password="owner")
    view.session.save()
    plain = get_engine().open(path)
    assert plain.info().encryption is EncryptionMethod.NONE
    plain.close()


def test_remove_security_on_plain_document(window: MainWindow, fixture_pdf, monkeypatch) -> None:
    window.open_path(fixture_pdf("report"))
    shown: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.protect_controller.QMessageBox.information",
        lambda _p, _t, msg: shown.append(msg),
    )
    assert not window.protect.remove_security()
    assert shown and "no security" in shown[0]
