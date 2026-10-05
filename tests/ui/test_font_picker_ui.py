"""Font picker (Phase F4/F5): sections, filtering, bold/italic availability, recents, and
end-to-end use from the text style bar and the header/footer dialog."""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QFontInfo

from pdfeditor.model.fonts import FontRef, FontRefKind
from pdfeditor.model.objects import ObjectType, TextStyle
from pdfeditor.services.fonts import FontCatalog
from pdfeditor.ui.dialogs.pages import HeaderFooterDialog
from pdfeditor.ui.font_picker import FontPicker, remember_font
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.view.text_editor import InlineTextEditor, TextStyleBar

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
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
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("mixed_content"), path)
    v = window.open_path(path)
    v.set_zoom(1.0)
    v.go_to_page(0, record=False)
    return v


def _rows(picker: FontPicker) -> list[str]:
    return [picker._model.item(r).text() for r in range(picker._model.rowCount())]


def _row_of(picker: FontPicker, text: str) -> int:
    for row, label in enumerate(_rows(picker)):
        if label == text:
            return row
    raise AssertionError(f"{text!r} not in {_rows(picker)}")


# -- sections & headers -------------------------------------------------------------------------


def test_sections_have_header_rows_and_installed_families(qtbot, font_catalog: FontCatalog):
    picker = FontPicker(document_fonts=["Arial"])
    qtbot.addWidget(picker)
    rows = _rows(picker)
    assert "In this document" in rows
    assert "Standard" in rows
    assert "Installed" in rows
    assert "Test Sans" in rows
    assert "Test Collection One" in rows


def test_header_rows_are_not_selectable(qtbot, font_catalog: FontCatalog):
    picker = FontPicker(document_fonts=["Arial"])
    qtbot.addWidget(picker)
    for row, label in enumerate(_rows(picker)):
        item = picker._model.item(row)
        if label in ("In this document", "Standard", "Installed", "Recent"):
            assert not (item.flags() & Qt.ItemFlag.ItemIsSelectable)
            assert not (item.flags() & Qt.ItemFlag.ItemIsEnabled)
        else:
            assert item.flags() & Qt.ItemFlag.ItemIsSelectable


def test_restricted_font_is_disabled_with_reason(qtbot, font_catalog: FontCatalog):
    picker = FontPicker()
    qtbot.addWidget(picker)
    row = _row_of(picker, "Test Restricted")
    item = picker._model.item(row)
    assert not (item.flags() & Qt.ItemFlag.ItemIsEnabled)
    assert "restrict" in item.toolTip()


def test_filtering_by_typing(qtbot, font_catalog: FontCatalog):
    picker = FontPicker()
    qtbot.addWidget(picker)
    completer = picker.completer()
    assert completer is not None
    completer.setCompletionPrefix("Test Coll")
    filtered = completer.completionCount()
    assert filtered == 2  # "Test Collection One" and "Test Collection Two"
    for i in range(filtered):
        completer.setCurrentRow(i)
        assert "Test Coll" in completer.currentCompletion()
    completer.setCompletionPrefix("Nonexistent Family Zzz")
    assert completer.completionCount() == 0


# -- recents --------------------------------------------------------------------------------------


def test_recents_persist_and_cap_at_eight(qtbot, font_catalog: FontCatalog):
    for i in range(10):
        remember_font(FontRef.file(f"/fake/font{i}.ttf", 0, f"Fake Font {i}"))
    recents = AppSettings().recent_fonts
    assert len(recents) == 8
    assert recents[0].name == "Fake Font 9"  # most recent first
    assert recents[-1].name == "Fake Font 2"


def test_remember_font_moves_existing_entry_to_front(qtbot, font_catalog: FontCatalog):
    a = FontRef.file("/fake/a.ttf", 0, "A")
    b = FontRef.file("/fake/b.ttf", 0, "B")
    remember_font(a)
    remember_font(b)
    remember_font(a)
    names = [r.name for r in AppSettings().recent_fonts]
    assert names == ["A", "B"]


def test_remember_font_skips_document_refs(qtbot, font_catalog: FontCatalog):
    remember_font(FontRef.document("Some Document Font"))
    assert AppSettings().recent_fonts == []


def test_recent_section_shown_in_picker(qtbot, font_catalog: FontCatalog, fixtures_dir):
    # only installed fonts are remembered: standard ones are always listed anyway
    remember_font(
        FontRef.file(str(fixtures_dir / "fonts" / "TestSans-Regular.ttf"), 0, "Test Sans")
    )
    picker = FontPicker()
    qtbot.addWidget(picker)
    assert "Recent" in _rows(picker)


# -- bold/italic availability --------------------------------------------------------------------


def test_bold_italic_enabled_only_when_faces_exist(qtbot, font_catalog: FontCatalog, fixtures_dir):
    regular = str(fixtures_dir / "fonts" / "TestSans-Regular.ttf")
    style = TextStyle(font="Test Sans", font_ref=FontRef.file(regular, 0, "Test Sans"))
    bar = TextStyleBar(None, style, lambda: None)
    qtbot.addWidget(bar)
    assert bar.bold.isEnabled()
    assert bar.italic.isEnabled()


def test_toggling_bold_switches_file_ref_to_bold_face(
    qtbot, font_catalog: FontCatalog, fixtures_dir
):
    regular = str(fixtures_dir / "fonts" / "TestSans-Regular.ttf")
    style = TextStyle(font="Test Sans", font_ref=FontRef.file(regular, 0, "Test Sans"))
    bar = TextStyleBar(None, style, lambda: None)
    qtbot.addWidget(bar)
    bar.bold.setChecked(True)
    ref = bar.font_picker.current_ref()
    assert ref.kind is FontRefKind.FILE
    assert "bold" in ref.path.lower()


def test_standard_font_always_has_bold_italic_enabled(qtbot, font_catalog: FontCatalog):
    style = TextStyle(font="Helvetica", font_ref=FontRef.standard("Helvetica"))
    bar = TextStyleBar(None, style, lambda: None)
    qtbot.addWidget(bar)
    assert bar.bold.isEnabled()
    assert bar.italic.isEnabled()


# -- end-to-end: TextStyleBar commit embeds the chosen installed font ---------------------------


def test_commit_with_installed_font_embeds_it(
    qtbot, window: MainWindow, view, fixtures_dir, tmp_path: Path
):
    window.set_tool("edit")
    obj = next(o for o in view.page_objects(0) if o.type is ObjectType.TEXT)
    center = view.mapFromScene(view.page_point_to_scene(0, obj.bbox.center))
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
    editor = view.viewport().findChild(InlineTextEditor)
    assert editor is not None
    regular = str(fixtures_dir / "fonts" / "TestSans-Regular.ttf")
    editor.style_bar.font_picker.set_selection(FontRef.file(regular, 0, "Test Sans"), "Test Sans")
    editor.setPlainText("ABC abc")  # the synthetic "Test Sans" fixture only covers these glyphs
    editor.commit()
    qtbot.waitUntil(lambda: view.viewport().findChild(InlineTextEditor) is None, timeout=2000)

    fonts = view.session.document.fonts()
    embedded = [f for f in fonts if f.embedded and "test sans" in f.name.lower()]
    assert embedded, [f.name for f in fonts]

    out = tmp_path / "saved.pdf"
    view.session.save(out)
    with pikepdf.open(out):
        pass  # must open cleanly (round-trip rule)

    recents = AppSettings().recent_fonts
    assert any(r.name == "Test Sans" for r in recents)


def test_reediting_after_reopen_keeps_the_installed_font(
    qtbot, window: MainWindow, view, fixtures_dir, tmp_path: Path, font_catalog: FontCatalog
):
    """The saved subset has no cmap, so neither Qt nor the engine can use it for new text:
    a re-edit must map the reopened name back to the installed face, not fall back to a
    base-14 font or preview in a fallback font."""

    def edit(v, text: str, ref: FontRef | None = None) -> InlineTextEditor:
        obj = next(o for o in v.page_objects(0) if o.type is ObjectType.TEXT)
        center = v.mapFromScene(v.page_point_to_scene(0, obj.bbox.center))
        qtbot.mouseDClick(v.viewport(), Qt.MouseButton.LeftButton, pos=center)
        editor = v.viewport().findChild(InlineTextEditor)
        assert editor is not None
        if ref is not None:
            editor.style_bar.font_picker.set_selection(ref, ref.name)
        editor.setPlainText(text)
        return editor

    window.set_tool("edit")
    regular = fixtures_dir / "fonts" / "TestSans-Regular.ttf"
    edit(view, "ABC abc", FontRef.file(str(regular), 0, "Test Sans")).commit()
    qtbot.waitUntil(lambda: view.viewport().findChild(InlineTextEditor) is None, timeout=2000)
    saved = tmp_path / "saved.pdf"
    view.session.save(saved)
    view.session.undo_stack.set_clean()

    reopened = window.open_path(saved)
    reopened.set_zoom(1.0)
    reopened.go_to_page(0, record=False)
    window.set_tool("edit")
    # "installed" for Qt too, so the preview can resolve it by family name
    font_id = QFontDatabase.addApplicationFont(str(regular))
    assert font_id >= 0
    try:
        editor = edit(reopened, "abc ABC")
        ref = editor.style_bar.font_picker.current_ref()
        assert ref.kind is FontRefKind.DOCUMENT and Path(ref.path) == regular
        assert QFontInfo(editor.font()).family() == "Test Sans"  # not a fallback font
        editor.commit()
        qtbot.waitUntil(
            lambda: reopened.viewport().findChild(InlineTextEditor) is None, timeout=2000
        )
    finally:
        QFontDatabase.removeApplicationFont(font_id)

    obj = next(o for o in reopened.page_objects(0) if o.type is ObjectType.TEXT)
    assert obj.text == "abc ABC"
    assert obj.style is not None and "test sans" in obj.style.font.lower()

    # round trip: the re-edited file keeps the font and opens in independent implementations
    again = tmp_path / "again.pdf"
    reopened.session.save(again)
    reopened.session.undo_stack.set_clean()
    with pikepdf.open(again) as pdf:
        names = {
            str(f.get("/BaseFont", "")) for f in pdf.pages[0].Resources.get("/Font", {}).values()
        }
    assert any("TestSans" in n or "Test Sans" in n for n in names), names
    pdfium_doc = pdfium.PdfDocument(again)
    try:
        assert pdfium_doc[0].render(scale=0.5).to_pil().size[0] > 0
    finally:
        pdfium_doc.close()
    final = window.open_path(again)
    text = next(o for o in final.page_objects(0) if o.type is ObjectType.TEXT)
    assert text.text == "abc ABC" and text.style is not None
    assert "test sans" in text.style.font.lower()


# -- header/footer dialog round-trip -------------------------------------------------------------


def test_header_footer_dialog_round_trips_file_font(qtbot, fixtures_dir):
    regular = str(fixtures_dir / "fonts" / "TestSans-Regular.ttf")
    ref = FontRef.file(regular, 0, "Test Sans")

    dialog = HeaderFooterDialog(5, 0, [0])
    qtbot.addWidget(dialog)
    dialog.font_picker.set_selection(ref, "Test Sans")
    spec = dialog.spec()
    assert spec.font_ref == ref

    dialog2 = HeaderFooterDialog(5, 0, [0])
    qtbot.addWidget(dialog2)
    dialog2.load(spec)
    assert dialog2.font_picker.current_ref() == ref


def test_header_footer_dialog_round_trips_standard_font(qtbot):
    dialog = HeaderFooterDialog(5, 0, [0])
    qtbot.addWidget(dialog)
    spec = dialog.spec()
    assert spec.font_ref is None
    assert spec.font == "helv"

    dialog2 = HeaderFooterDialog(5, 0, [0])
    qtbot.addWidget(dialog2)
    dialog2.load(spec)
    assert dialog2.font_picker.current_ref().kind is FontRefKind.STANDARD
