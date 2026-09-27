from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pdfeditor.services.ocr import installed_languages
from pdfeditor.ui.dialogs.ocr import BatchOcrDialog, OcrDialog
from pdfeditor.ui.main_window import MainWindow

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif("eng" not in installed_languages(), reason="English OCR data missing"),
]


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


def page_text(view) -> str:
    with view.session.lock:
        return view.session.document.page(0).text_page(with_chars=False).text


def test_recognize_text_is_undoable(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    shutil.copy2(fixture_pdf("scanned"), path)
    view = window.open_path(path)
    dialog = OcrDialog(view.page_count, 0, [], window)
    dialog.box.dpi.setValue(200)
    dialog.accept()
    result = window.tools.recognize(dialog)
    assert result is not None and list(result.layers) == [0]
    assert "Scanned" in page_text(view)
    assert "Recognized text on 1 page" in window.tools.last_message
    view.session.undo()
    assert page_text(view).strip() == ""
    view.session.redo()
    assert "Scanned" in page_text(view)


def test_no_language_is_reported(window: MainWindow, fixture_pdf, monkeypatch) -> None:
    view = window.open_path(fixture_pdf("scanned"))
    dialog = OcrDialog(view.page_count, 0, [], window)
    for i in range(dialog.box.languages.count()):
        dialog.box.languages.item(i).setCheckState(
            __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.CheckState.Unchecked
        )
    warnings: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.tools_controller.QMessageBox.warning",
        lambda _p, _t, msg: warnings.append(msg),
    )
    dialog.accept()
    assert window.tools.recognize(dialog) is None
    assert warnings and "language" in warnings[0]


def test_batch_ocr(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    dialog = BatchOcrDialog(window)
    dialog.add_path(fixture_pdf("scanned"))
    dialog.out_dir.setText(str(tmp_path / "out"))
    dialog.box.dpi.setValue(150)
    dialog.accept()
    written = window.tools.batch_ocr(dialog)
    assert [p.name for p in written] == ["scanned.pdf"]
    assert written[0].exists()
