"""Organize ▸ Header & Footer / Bates / Watermark / Background: Add, Update, Remove
(issues #39, #40)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PySide6.QtWidgets import QMessageBox, QToolButton

from pdfeditor.model.color import Color
from pdfeditor.model.pages import MarkKind
from pdfeditor.ui.dialogs.pages import HeaderFooterDialog, WatermarkDialog
from pdfeditor.ui.main_window import MainWindow

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
def doc_path(fixture_pdf, tmp_path: Path) -> Path:
    dst = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), dst)
    return dst


def accepted(dialog):
    dialog.accept()
    return dialog


def page_text(view, index: int) -> str:
    with view.session.lock:
        return view.session.document.page(index).text_page(with_chars=False).text


def add_header(window: MainWindow, text: str) -> None:
    view = window.current_view()
    d = HeaderFooterDialog(view.page_count, 0, [0], parent=window)
    d.slots[next(iter(d.slots))].setText(text)  # header left
    d.range.all.setChecked(True)
    window.organize.header_footer(False, accepted(d))


def test_mark_buttons_offer_add_update_remove(window: MainWindow, doc_path: Path) -> None:
    window.open_path(doc_path)
    organize = window.organize
    group = next(g for g in window.ribbon.tab("Organize").groups if g.label.text() == "Marks")
    for kind, main in (
        (MarkKind.HEADER_FOOTER, organize.act_header),
        (MarkKind.BATES, organize.act_bates),
        (MarkKind.WATERMARK, organize.act_watermark),
        (MarkKind.BACKGROUND, organize.act_background),
    ):
        button = group.bar.widgetForAction(main)
        assert isinstance(button, QToolButton)
        assert button.popupMode() is QToolButton.ToolButtonPopupMode.MenuButtonPopup
        menu = main.menu()
        assert menu is not None and menu.actions() == list(organize.mark_actions[kind])
        texts = [a.text().replace("&", "") for a in menu.actions()]
        assert texts[0].startswith("Add ") and texts[1].startswith("Update ")
        assert texts[2].startswith("Remove ")
        assert all(a.isEnabled() for a in menu.actions())
    assert [a.text() for a in organize.mark_actions[MarkKind.HEADER_FOOTER]] == [
        "&Add Header && Footer…",
        "&Update Header && Footer…",
        "&Remove Header && Footer",
    ]


def test_update_header_prefills_and_replaces(
    window: MainWindow, doc_path: Path, monkeypatch
) -> None:
    view = window.open_path(doc_path)
    add_header(window, "FIRST-HEADER")
    assert page_text(view, 2).count("FIRST-HEADER") == 1

    info = window.organize.find_marks(MarkKind.HEADER_FOOTER)
    assert info is not None and info.pages == [0, 1, 2, 3, 4]
    d = window.organize.header_footer_dialog(False, info)
    assert d.windowTitle() == "Update Header & Footer"
    assert d.slots[next(iter(d.slots))].text() == "FIRST-HEADER"  # pre-filled
    assert d.range.all.isChecked()
    d.slots[next(iter(d.slots))].setText("SECOND-HEADER")
    d.size_box.setValue(12)
    window.organize.header_footer(False, accepted(d), update=True)
    assert window.act_undo.text() == "&Undo Update Header & Footer"
    for index in range(5):
        text = page_text(view, index)
        assert "FIRST-HEADER" not in text and text.count("SECOND-HEADER") == 1

    # the plain command asks first; "Replace Existing" replaces again
    asked: list[MarkKind] = []
    monkeypatch.setattr(
        window.organize, "ask_replace", lambda kind, _info: asked.append(kind) or True
    )
    add_header(window, "THIRD-HEADER")
    assert asked == [MarkKind.HEADER_FOOTER]
    assert page_text(view, 0).count("THIRD-HEADER") == 1
    assert "SECOND-HEADER" not in page_text(view, 0)

    window.act_undo.trigger()
    assert page_text(view, 0).count("SECOND-HEADER") == 1
    assert "THIRD-HEADER" not in page_text(view, 0)

    window.save()
    with pikepdf.open(doc_path) as pdf:
        assert len(pdf.pages) == 5
    pd = pdfium.PdfDocument(doc_path)
    try:
        assert pd[0].get_textpage().get_text_range().count("SECOND-HEADER") == 1
    finally:
        pd.close()


def test_add_new_stacks_and_cancel_does_nothing(
    window: MainWindow, doc_path: Path, monkeypatch
) -> None:
    view = window.open_path(doc_path)
    add_header(window, "ONE")
    monkeypatch.setattr(window.organize, "ask_replace", lambda _kind, _info: False)
    add_header(window, "TWO")  # "Add New": both stay
    text = page_text(view, 0)
    assert "ONE" in text and "TWO" in text
    assert window.act_undo.text() == "&Undo Add Header & Footer"

    monkeypatch.setattr(window.organize, "ask_replace", lambda _kind, _info: None)
    shown: list[str] = []
    view.session.undo_stack.set_clean()
    d = HeaderFooterDialog(view.page_count, 0, [0], parent=window)
    d.exec = lambda: shown.append("dialog") or 0  # type: ignore[method-assign]
    window.organize.header_footer(False, d)  # cancelled at the question
    assert shown == [] and not view.session.is_dirty


def test_update_and_remove_watermark(window: MainWindow, doc_path: Path, monkeypatch) -> None:
    view = window.open_path(doc_path)
    w = WatermarkDialog(view.page_count, 0, [0], window)
    w.text.setText("SECRET-ONE")
    w.range.custom.setChecked(True)
    w.range.edit.setText("2-3")
    window.organize.watermark(accepted(w))

    info = window.organize.find_marks(MarkKind.WATERMARK)
    assert info is not None and info.pages == [1, 2]
    d = window.organize.watermark_dialog(info)
    assert d.text.text() == "SECRET-ONE" and d.opacity.value() == 30
    assert d.range.custom.isChecked() and d.range.edit.text() == "2-3"
    d.text.setText("SECRET-TWO")
    window.organize.watermark(accepted(d), update=True)
    for index in (1, 2):
        text = page_text(view, index)
        assert "SECRET-ONE" not in text and text.count("SECRET-TWO") == 1

    remove = window.organize.mark_actions[MarkKind.WATERMARK][2]
    remove.trigger()
    assert window.act_undo.text() == "&Undo Remove Watermark"
    assert all("SECRET" not in page_text(view, i) for i in range(5))
    assert "Page 2 heading" in page_text(view, 1)

    # nothing left: Update and Remove say so instead of doing anything
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    remove.trigger()
    window.organize.mark_actions[MarkKind.WATERMARK][1].trigger()
    assert len(told) == 2 and all("no watermark" in t for t in told)
    assert window.act_undo.text() == "&Undo Remove Watermark"

    window.act_undo.trigger()
    assert page_text(view, 1).count("SECRET-TWO") == 1


def test_update_bates_and_background(window: MainWindow, doc_path: Path) -> None:
    view = window.open_path(doc_path)
    d = HeaderFooterDialog(view.page_count, 0, [0], bates=True, parent=window)
    d.bates_prefix.setText("AAA")
    d.range.all.setChecked(True)
    window.organize.header_footer(True, accepted(d))
    assert "AAA000005" in page_text(view, 4)
    info = window.organize.find_marks(MarkKind.BATES)
    assert info is not None
    d = window.organize.header_footer_dialog(True, info)
    assert d.bates_prefix.text() == "AAA" and d.windowTitle() == "Update Bates Numbers"
    d.bates_prefix.setText("BBB")
    window.organize.header_footer(True, accepted(d), update=True)
    text = page_text(view, 4)
    assert "AAA" not in text and text.count("BBB000005") == 1

    from pdfeditor.ui.dialogs.pages import BackgroundDialog

    b = BackgroundDialog(view.page_count, 0, [0], window)
    b.color_button.set(Color(1, 0, 0))
    b.range.current.setChecked(True)
    window.organize.background(accepted(b))
    info = window.organize.find_marks(MarkKind.BACKGROUND)
    assert info is not None and info.pages == [0]
    b = window.organize.background_dialog(info)
    assert b.color_button.color == Color(1, 0, 0) and b.range.current.isChecked()
    window.organize.remove_marks(MarkKind.BACKGROUND)
    assert window.organize.find_marks(MarkKind.BACKGROUND) is None
    assert window.organize.find_marks(MarkKind.BATES) is not None
