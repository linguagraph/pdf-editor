from __future__ import annotations

import io
import shutil
from pathlib import Path

import pikepdf
import pytest

from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1000, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    if w.pdfa.dialog is not None:
        w.pdfa.dialog.close()
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


def test_preflight_and_save_as_pdfa(
    window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "report.pdf"
    shutil.copy2(fixture_pdf("report"), path)
    view = window.open_path(path)
    issues = window.pdfa.preflight()
    assert issues and {i.code for i in issues} == {"output-intent", "identification", "fonts"}
    dialog = window.pdfa.dialog
    assert dialog is not None and dialog.convert_button is not None
    assert dialog.issues.count() == 3
    dialog.close()
    result = window.pdfa.save_as_pdfa(tmp_path / "out.pdf")
    assert result is not None and result.conforming
    assert "PDF/A-2b copy" in window.pdfa.last_message
    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        assert pdf.open_metadata()["pdfaid:part"] == "2"
    assert not view.session.is_dirty  # the open document is untouched
    warned: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.pdfa_controller.QMessageBox.warning", lambda _p, _t, msg: warned.append(msg)
    )
    assert window.pdfa.save_as_pdfa(path) is None  # never over the open file
    assert warned and "different file name" in warned[0]


def test_unfixable_problems_are_shown(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    window.open_path(fixture_pdf("cjk_text"))
    result = window.pdfa.save_as_pdfa(tmp_path / "cjk.pdf")
    assert result is not None and not result.conforming
    assert "isn't PDF/A yet" in window.pdfa.last_message
    assert window.pdfa.dialog is not None and window.pdfa.dialog.issues.count() == 1


def test_encrypted_needs_owner_password(
    window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "pdfeditor.ui.main_window.password_prompt", lambda *_a: lambda _attempt: "user"
    )
    window.open_path(fixture_pdf("encrypted"))
    monkeypatch.setattr(
        "pdfeditor.ui.protect_controller.QInputDialog.getText", lambda *_a, **_k: ("owner", True)
    )
    result = window.pdfa.save_as_pdfa(tmp_path / "plain.pdf")
    assert result is not None
    with pikepdf.open(io.BytesIO((tmp_path / "plain.pdf").read_bytes())) as pdf:
        assert not pdf.is_encrypted
