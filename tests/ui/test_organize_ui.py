from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QGuiApplication, QImage

from pdfeditor.model.pages import LabelStyle, PageLabelRule
from pdfeditor.ui.dialogs.pages import (
    BackgroundDialog,
    CombineDialog,
    CropDialog,
    ExtractDialog,
    HeaderFooterDialog,
    InsertPagesDialog,
    PageLabelsDialog,
    SplitDialog,
    WatermarkDialog,
)
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.organizer.organizer import MIME_PAGES, drop

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
def copy_pdf(fixture_pdf, tmp_path: Path):
    def make(name: str) -> Path:
        dst = tmp_path / f"{name}.pdf"
        shutil.copy2(fixture_pdf(name), dst)
        return dst

    return make


def first_lines(view) -> list[str]:
    with view.session.lock:
        doc = view.session.document
        out = []
        for i in range(doc.page_count):
            blocks = doc.page(i).text_page(with_chars=False).blocks
            out.append(blocks[0].lines[0].text if blocks and blocks[0].lines else "")
        return out


def accepted(dialog):
    dialog.accept()
    return dialog


def test_organizer_toggle_and_targets(qtbot, window: MainWindow, copy_pdf) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    tab = window.current_tab()
    assert tab.target_pages() == [0]
    window.organize.act_organize.trigger()
    assert tab.organizing and window.organize.act_organize.isChecked()
    tab.organizer.select_pages([1, 3])
    assert tab.target_pages() == [1, 3]
    tab.organizer.page_activated.emit(3)  # double-click: back to reading at that page
    assert not tab.organizing and view.current_page == 3


def test_delete_rotate_duplicate_with_undo(window: MainWindow, copy_pdf, monkeypatch) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    tab = window.current_tab()
    tab.set_organizing(True)
    tab.organizer.select_pages([1, 2])
    window.organize.delete_pages()
    assert view.page_count == 3 and first_lines(view)[1] == "Page 4 heading"
    assert window.act_undo.text() == "&Undo Delete 2 Pages"
    window.act_undo.trigger()
    assert view.page_count == 5
    tab.organizer.select_pages([0])
    window.organize.rotate(90)
    with view.session.lock:
        assert view.session.document.page(0).rotation == 90
    assert view.page_rect(0).width > view.page_rect(0).height  # view reloaded geometry
    window.organize.duplicate()
    assert view.page_count == 6 and first_lines(view)[1] == first_lines(view)[0]
    # deleting everything is refused with a message, not a crash
    tab.organizer.select_pages(list(range(6)))
    import pdfeditor.ui.organize as organize_module

    shown: list[str] = []
    monkeypatch.setattr(
        organize_module.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2]))
    )
    window.organize.act_delete.trigger()
    assert shown and "at least one page" in shown[0] and view.page_count == 6


def test_drag_reorder_and_file_drop(qtbot, window: MainWindow, copy_pdf, tmp_path: Path) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    mime = QMimeData()
    mime.setData(MIME_PAGES, f"{view.session.id}:3,4".encode())
    assert drop(view, mime, 0)
    assert [line.split()[1] for line in first_lines(view)] == ["4", "5", "1", "2", "3"]
    png = tmp_path / "pic.png"
    Image.new("RGB", (100, 100), (255, 0, 0)).save(png)
    files = QMimeData()
    files.setUrls([QUrl.fromLocalFile(str(png)), QUrl.fromLocalFile(str(copy_pdf("images")))])
    assert drop(view, files, 1)
    assert view.page_count == 7
    # pages dragged from another document are ignored (they'd need a copy, not a move)
    foreign = QMimeData()
    foreign.setData(MIME_PAGES, b"99999:0")
    assert not drop(view, foreign, 0)


def test_insert_dialog_variants(window: MainWindow, copy_pdf) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    d = InsertPagesDialog(view.page_count, 0, window)
    d.page.setValue(1)
    window.organize.insert_pages(accepted(d))  # blank after page 1
    assert view.page_count == 6 and first_lines(view)[0] == "Page 1 heading"
    with view.session.lock:
        assert view.session.document.page(1).text_page().text == ""
    d = InsertPagesDialog(view.page_count, 0, window)
    d.file.setChecked(True)
    d.path_edit.setText(str(copy_pdf("outline")))
    d.range_edit.setText("2-3")
    d.where.setCurrentText("Before")
    d.page.setValue(1)
    window.organize.insert_pages(accepted(d))
    assert view.page_count == 8 and first_lines(view)[0].startswith("Chapter 1, section 2")
    QGuiApplication.clipboard().setImage(QImage(50, 50, QImage.Format.Format_RGB32))
    d = InsertPagesDialog(view.page_count, 0, window)
    d.clipboard.setChecked(True)
    window.organize.insert_pages(accepted(d))
    assert view.page_count == 9


def test_extract_to_new_tab_and_files(window: MainWindow, copy_pdf, tmp_path: Path) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    d = ExtractDialog(view.page_count, 0, [0], window)
    d.range.custom.setChecked(True)
    d.range.edit.setText("2-3")
    d.delete_after.setChecked(True)
    (new_session,) = window.organize.extract_pages(accepted(d))
    assert (
        new_session.page_count == 2 and new_session.display_name == "text_multipage (extract).pdf"
    )
    assert new_session.is_dirty and window.current_view().session is new_session
    assert view.page_count == 3
    window.tabs.setCurrentIndex(0)
    d = ExtractDialog(view.page_count, 0, [0], window)
    d.range.all.setChecked(True)
    d.separate.setChecked(True)
    written = window.organize.extract_pages(accepted(d), folder=tmp_path)
    assert [p.name for p in written] == [f"text_multipage_page{i}.pdf" for i in (1, 2, 3)]


def test_replace_pages(window: MainWindow, copy_pdf) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    tab = window.current_tab()
    tab.set_organizing(True)
    tab.organizer.select_pages([1, 2])
    window.organize.replace_pages(copy_pdf("outline"), "5-6")
    lines = first_lines(view)
    assert (
        len(lines) == 5
        and lines[1].startswith("Chapter 3, section 1")
        and lines[3] == "Page 4 heading"
    )


def test_split_and_combine(window: MainWindow, copy_pdf, tmp_path: Path) -> None:
    window.open_path(copy_pdf("outline"))
    d = SplitDialog("book", tmp_path / "parts", window)
    d.by_bookmarks.setChecked(True)
    written = window.organize.split_document(accepted(d))
    assert len(written) == 3 and all(p.exists() for p in written)
    d = SplitDialog("n", tmp_path / "ranges", window)
    d.by_ranges.setChecked(True)
    d.ranges.setText("1; 2-4")
    assert len(window.organize.split_document(accepted(d))) == 2

    png = tmp_path / "cover.png"
    Image.new("RGB", (120, 160), (0, 0, 200)).save(png)
    c = CombineDialog([png, written[0], written[2]], window)
    c.list.item(2).setData(256, (str(written[2]), "2"))  # only page 2 of chapter 3
    session = window.organize.combine(accepted(c))
    assert session is not None and session.page_count == 1 + 2 + 1
    titles = [i.title for i in session.document.outline()]
    assert titles == ["cover", "book_Chapter 1", "book_Chapter 3"]
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Combined.pdf*"


def test_crop_and_trim(window: MainWindow, copy_pdf) -> None:
    view = window.open_path(copy_pdf("vector_art"))
    d = CropDialog(view.page_count, 0, [0], (10, 20, 30, 40), window)
    window.organize.crop_pages(accepted(d))
    assert view.page_rect(0).width == pytest.approx(595 - 40, abs=1)
    assert view.page_rect(0).height == pytest.approx(842 - 60, abs=1)
    window.act_undo.trigger()
    d = CropDialog(view.page_count, 0, [0], parent=window)
    d.trim.setChecked(True)
    window.organize.crop_pages(accepted(d))
    assert view.page_rect(0).width < 500 and view.page_rect(0).height < 500


def test_page_labels_dialog(window: MainWindow, copy_pdf) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    d = PageLabelsDialog([], view.page_count, window)
    d.add_rule(PageLabelRule(0, LabelStyle.ROMAN_LOWER))
    d.add_rule(PageLabelRule(2, LabelStyle.DECIMAL, "P-", 1))
    window.organize.page_labels(accepted(d))
    assert [view.page_label(i) for i in range(5)] == ["i", "ii", "P-1", "P-2", "P-3"]
    assert window.navigator.edit.text() == view.page_label(view.current_page)
    window.act_undo.trigger()
    assert view.page_label(0) == "1"


def test_header_footer_bates_watermark_background(window: MainWindow, copy_pdf) -> None:
    view = window.open_path(copy_pdf("text_multipage"))
    d = HeaderFooterDialog(view.page_count, 0, [0], bates=True, parent=window)
    d.bates_prefix.setText("DOC")
    d.bates_digits.setValue(4)
    window.organize.header_footer(True, accepted(d))
    with view.session.lock:
        text = view.session.document.page(4).text_page(with_chars=False).text
    assert "DOC0005" in text
    assert window.act_undo.text() == "&Undo Add Bates Numbers"
    w = WatermarkDialog(view.page_count, 0, [0], window)
    w.text.setText("SECRET")
    window.organize.watermark(accepted(w))
    b = BackgroundDialog(view.page_count, 0, [0], window)
    b.range.current.setChecked(True)
    window.organize.background(accepted(b))
    with view.session.lock:
        assert "SECRET" in view.session.document.page(2).text_page(with_chars=False).text
    window.save()
    with pikepdf.open(view.session.path) as pdf:
        assert len(pdf.pages) == 5


def test_bookmark_editing(qtbot, window: MainWindow, copy_pdf) -> None:
    from pdfeditor.ui.panels.bookmarks import BookmarksPanel

    view = window.open_path(copy_pdf("outline"))
    panel = next(p for p in window.panels if isinstance(p, BookmarksPanel))
    view.go_to_page(3)
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    panel.add_bookmark("New mark")
    titles = [panel.tree.topLevelItem(i).text(0) for i in range(panel.tree.topLevelItemCount())]
    assert titles == ["Chapter 1", "New mark", "Chapter 2", "Chapter 3"]
    with view.session.lock:
        new = view.session.document.outline()[1]
    assert new.dest.page_index == 3
    panel.tree.topLevelItem(1).setText(0, "Renamed")
    with view.session.lock:
        assert view.session.document.outline()[1].title == "Renamed"
    panel.tree.setCurrentItem(panel.tree.topLevelItem(1))
    panel.delete_bookmark()
    with view.session.lock:
        assert [i.title for i in view.session.document.outline()] == [
            "Chapter 1",
            "Chapter 2",
            "Chapter 3",
        ]
    # nest chapter 3 under chapter 2 (what a drag does), then commit
    chapter3 = panel.tree.takeTopLevelItem(2)
    panel.tree.topLevelItem(1).addChild(chapter3)
    panel.commit("Move Bookmark")
    with view.session.lock:
        roots = view.session.document.outline()
    assert [r.title for r in roots] == ["Chapter 1", "Chapter 2"] and roots[1].children[
        -1
    ].title == "Chapter 3"
    window.act_undo.trigger()
    with view.session.lock:
        assert len(view.session.document.outline()) == 3
