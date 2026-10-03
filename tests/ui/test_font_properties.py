"""Fonts tab of Document Properties: where each font is used and how edits treat it (F6)."""

from __future__ import annotations

import pytest

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.metadata import FontInfo
from pdfeditor.ui.dialogs.properties import editing_use, page_ranges

pytestmark = pytest.mark.gui


def font(**kw: object) -> FontInfo:
    base: dict[str, object] = dict(
        name="Nowhere Sans", type="TrueType", encoding="", embedded=True, subset=False, ref=1
    )
    base.update(kw)
    return FontInfo(**base)  # type: ignore[arg-type]


def test_page_ranges() -> None:
    assert page_ranges(()) == ""
    assert page_ranges((0,)) == "1"
    assert page_ranges((0, 1, 2, 6)) == "1-3, 7"
    assert page_ranges((4, 0, 2, 1)) == "1-3, 5"


def test_editing_use() -> None:
    assert editing_use(font())[0] == "Reused"
    assert editing_use(font(subset=True))[0] == "Partly reused"
    assert editing_use(font(embedded=False))[0] == "Standard substitute"
    assert editing_use(font(type="Type3"))[0] == "Can't edit"
    # a family the (synthetic) installed-font catalog has: edits use the installed copy
    assert editing_use(font(name="ABCDEF+TestSans-Bold", subset=True))[0] == "Installed copy"


def test_fonts_report_the_pages_using_them(fixture_pdf) -> None:
    doc = get_engine().open(fixture_pdf("text_multipage"))
    try:
        fonts = doc.fonts()
        assert fonts and all(f.pages for f in fonts)
        assert max(p for f in fonts for p in f.pages) == doc.page_count - 1
    finally:
        doc.close()


def test_same_font_family() -> None:
    from pdfeditor.services.fonts import same_font_family

    assert same_font_family("ABCDEF+TimesNewRomanPSMT", "Times New Roman")
    assert same_font_family("Helvetica-Bold", "Helvetica")
    assert not same_font_family("Helvetica", "Courier")
    assert not same_font_family("", "")


def test_show_text_in_this_font_selects_its_blocks(qtbot, fixture_pdf) -> None:
    from pdfeditor.ui.dialogs.properties import PropertiesDialog
    from pdfeditor.ui.main_window import MainWindow

    window = MainWindow()
    window.resize(1000, 800)
    window.show()
    qtbot.waitExposed(window)
    try:
        view = window.open_path(fixture_pdf("text_multipage"))
        dialog = PropertiesDialog(view.session, window)
        assert not dialog.show_font_button.isEnabled()
        row = next(i for i, f in enumerate(dialog.font_rows) if f.pages and f.type != "Type3")
        dialog.fonts_table.selectRow(row)
        assert dialog.show_font_button.isEnabled()
        dialog.show_font_button.click()
        font = dialog.show_font
        assert font is not None and dialog.result()
        count = window.show_text_in_font(view, font.name, font.pages)
        assert count > 0 and window.current_tool == "edit"
        assert len(view.selected_objects) == count
        assert {p for p, _ in view.selected_objects} <= set(font.pages)
    finally:
        for v in window.views():
            v.session.undo_stack.set_clean()
        window.close()
        window.deleteLater()
