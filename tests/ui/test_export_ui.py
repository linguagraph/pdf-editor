from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from pdfeditor.ui.dialogs.export import ExportDialog, ExportFormat, TablePickerDialog
from pdfeditor.ui.jobs import wait_for
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


@pytest.fixture
def view(window: MainWindow, fixture_pdf):
    return window.open_path(fixture_pdf("report"))


def dialog_for(window: MainWindow, view, fmt: ExportFormat, target: Path) -> ExportDialog:
    d = ExportDialog(view.page_count, 0, [], view.session.path, fmt, window)
    d.target.setText(str(target))
    d.accept()
    return d


def test_dialog_adapts_to_format(window: MainWindow, view) -> None:
    d = ExportDialog(view.page_count, 0, [], view.session.path, ExportFormat.WORD, window)
    assert d.target.text().endswith("report.docx")
    assert d.form.isRowVisible(d.pictures) and not d.form.isRowVisible(d.dpi)
    # Word keeps the page layout by default; reflow options only apply to flowing text
    assert d.keeps_layout() and d.structure_options().keep_layout
    assert d.form.isRowVisible(d.layout_mode) and not d.form.isRowVisible(d.page_breaks)
    assert d.form.isRowVisible(d.tables)
    d.layout_mode.setCurrentIndex(1)
    assert not d.keeps_layout() and d.form.isRowVisible(d.page_breaks)
    d.set_format(ExportFormat.HTML)
    assert not d.form.isRowVisible(d.layout_mode) and not d.structure_options().keep_layout
    d.set_format(ExportFormat.WORD)
    d.set_format(ExportFormat.JPEG)
    assert d.target.text().endswith("report.jpg")
    assert d.form.isRowVisible(d.quality) and not d.form.isRowVisible(d.pictures)
    d.set_format(ExportFormat.TIFF)
    assert d.form.isRowVisible(d.multipage) and not d.form.isRowVisible(d.quality)
    d.set_format(ExportFormat.TEXT)
    assert not d.form.isRowVisible(d.pictures)
    d.close()


def test_export_word_and_images(window: MainWindow, view, tmp_path: Path) -> None:
    written = wait_for(
        window.export.export(dialog_for(window, view, ExportFormat.WORD, tmp_path / "out"))
    )
    assert written == [tmp_path / "out.docx"]
    with zipfile.ZipFile(written[0]) as z:
        body = z.read("word/document.xml")
        assert b"Quarterly Report" in body and b"<w:framePr" in body  # page layout kept
        assert b"<w:tbl>" in body and b"<w:tblpPr" in body  # a real table at its place
    assert "Exported 2 page(s)" in window.export.last_message
    d = dialog_for(window, view, ExportFormat.PNG, tmp_path / "pg")
    d.dpi.setValue(40)
    written = wait_for(window.export.export(d))
    assert [p.name for p in written] == ["pg-1.png", "pg-2.png"]
    assert not view.session.is_dirty


def test_export_tables_with_picker(window: MainWindow, view, tmp_path: Path) -> None:
    picked: list[TablePickerDialog] = []

    class Picker(TablePickerDialog):
        def __init__(self, tables, parent=None):
            super().__init__(tables, parent)
            picked.append(self)
            self.accept()

    import pdfeditor.ui.export_controller as ec

    original = ec.TablePickerDialog
    ec.TablePickerDialog = Picker  # type: ignore[misc]
    try:
        written = wait_for(
            window.export.export(dialog_for(window, view, ExportFormat.EXCEL, tmp_path / "t"))
        )
    finally:
        ec.TablePickerDialog = original  # type: ignore[misc]
    assert written == [tmp_path / "t.xlsx"] and picked[0].tree.topLevelItemCount() == 1
    # unchecking every table exports nothing
    tables = picked[0].tables
    picker = TablePickerDialog(tables, window)
    picker.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
    picker.accept()
    assert picker.chosen() == []
    assert (
        wait_for(
            window.export.export(
                dialog_for(window, view, ExportFormat.EXCEL, tmp_path / "u"), picker=picker
            )
        )
        == []
    )


def test_no_tables_is_reported(
    window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    shown: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.export_controller.QMessageBox.information",
        lambda _p, _t, msg: shown.append(msg),
    )
    job = window.export.export(dialog_for(window, view, ExportFormat.EXCEL, tmp_path / "x"))
    assert wait_for(job) == []
    assert shown and "No tables" in shown[0]


def test_extract_images_and_fonts(window: MainWindow, view, tmp_path: Path) -> None:
    images = wait_for(window.export.extract_images(tmp_path / "img"))
    assert len(images) == 1 and "Extracted 1 image" in window.export.last_message
    assert wait_for(window.export.extract_fonts(tmp_path / "fonts")) == []
    assert "not embedded" in window.export.last_message


def test_office_without_libreoffice_explains(window: MainWindow, monkeypatch) -> None:
    shown: list[str] = []
    monkeypatch.setattr("pdfeditor.ui.export_controller.find_soffice", lambda: None)
    monkeypatch.setattr(
        "pdfeditor.ui.export_controller.QMessageBox.information",
        lambda _p, _t, msg: shown.append(msg),
    )
    assert window.export.from_office(Path("x.docx")) is None
    assert shown and "LibreOffice" in shown[0]


def test_office_conversion_opens_new_tab(window: MainWindow, fixture_pdf, monkeypatch) -> None:
    data = fixture_pdf("report").read_bytes()
    monkeypatch.setattr("pdfeditor.ui.export_controller.find_soffice", lambda: Path("soffice"))
    monkeypatch.setattr("pdfeditor.ui.export_controller.convert_to_pdf", lambda _p: data)
    view = wait_for(window.export.from_office(Path("letter.docx")))
    assert view is not None and view.page_count == 2
    assert view.session.name_hint == "letter.pdf"


def test_menus_and_ribbon(window: MainWindow) -> None:
    assert window.export.act_export in window.ribbon.tab("Tools").button_actions()
    texts = [a.text() for a in window.menu_bar.actions()]
    assert "&Tools" in texts
