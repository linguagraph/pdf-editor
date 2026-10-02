from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from pdfeditor.services.ocr import OcrOptions, installed_languages
from pdfeditor.ui.dialogs.ocr import BatchOcrDialog, OcrDialog
from pdfeditor.ui.jobs import wait_for
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.settings import AppSettings

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
    result = wait_for(window.tools.recognize(dialog))
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
    written = wait_for(window.tools.batch_ocr(dialog))
    assert [p.name for p in written] == ["scanned.pdf"]
    assert written[0].exists()


def test_options_box_maps_cleanup_and_deskew(qtbot) -> None:
    dialog = OcrDialog(1, 0, [])
    qtbot.addWidget(dialog)
    assert not dialog.box.options().deskew
    dialog.box.preprocess.setChecked(True)
    dialog.box.deskew.setChecked(True)
    options = dialog.box.options()
    assert options.preprocess and options.deskew


@pytest.fixture
def bulgarian(tmp_path: Path, monkeypatch) -> None:
    """Install a "bul" language for the test (English data under another name)."""
    from pdfeditor.services import ocr

    folder = tmp_path / "tessdata"
    folder.mkdir()
    shutil.copy2(installed_languages()["eng"] / "eng.traineddata", folder / "bul.traineddata")
    monkeypatch.setattr(ocr, "user_tessdata", lambda: folder)


def check(dialog: OcrDialog | BatchOcrDialog, languages: set[str]) -> None:
    box = dialog.box.languages
    for i in range(box.count()):
        item = box.item(i)
        code = item.data(Qt.ItemDataRole.UserRole)
        item.setCheckState(Qt.CheckState.Checked if code in languages else Qt.CheckState.Unchecked)


def test_last_ocr_options_are_preselected(
    window: MainWindow, fixture_pdf, tmp_path: Path, bulgarian
) -> None:
    path = tmp_path / "scan.pdf"
    shutil.copy2(fixture_pdf("scanned"), path)
    view = window.open_path(path)
    dialog = OcrDialog(view.page_count, 0, [], window)
    assert dialog.box.languages.languages() == ("eng",)  # nothing remembered yet
    check(dialog, {"bul", "eng"})
    dialog.box.dpi.setValue(200)
    dialog.box.preprocess.setChecked(True)
    dialog.box.skip_text.setChecked(False)
    dialog.accept()
    assert wait_for(window.tools.recognize(dialog)) is not None
    again = OcrDialog(view.page_count, 0, [], window)
    assert again.box.options() == OcrOptions(("bul", "eng"), 200, False, True, False)
    batch = BatchOcrDialog(window)
    assert batch.box.options() == again.box.options()
    assert AppSettings().ocr_options() == again.box.options()
    # Batch OCR remembers its choice for the next OCR as well
    check(batch, {"bul"})
    batch.add_path(fixture_pdf("scanned"))
    batch.out_dir.setText(str(tmp_path / "out"))
    batch.accept()
    wait_for(window.tools.batch_ocr(batch))
    assert OcrDialog(1, 0, [], window).box.languages.languages() == ("bul",)


def test_remembered_languages_that_are_gone_fall_back_to_english(qtbot) -> None:
    prefs = AppSettings()
    prefs.ocr_languages = ["bul", "xyz"]
    dialog = OcrDialog(1, 0, [])
    qtbot.addWidget(dialog)
    assert dialog.box.languages.languages() == ("eng",)
    assert prefs.ocr_options().languages == ("eng",)
    prefs.ocr_languages = ["xyz", "eng"]
    assert prefs.ocr_options().languages == ("eng",)


def test_ocr_chip_is_busy_and_names_the_page(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("scanned"))
    chip = window.progress_chip
    states: list[tuple[str, int, bool]] = []
    window.jobs.changed.connect(
        lambda: states.append((chip.button.text(), chip.bar.maximum(), chip.isVisible()))
    )
    dialog = OcrDialog(view.page_count, 0, [], window)
    dialog.box.dpi.setValue(150)
    dialog.accept()
    assert wait_for(window.tools.recognize(dialog)) is not None
    # shown at once (no reveal delay) with a busy bar while page 1 of 1 is read, then full
    assert ("Recognizing text · page 1 of 1", 0, True) in states
    assert ("Recognizing text · page 1 of 1", 1, True) in states
    view.session.undo_stack.set_clean()
