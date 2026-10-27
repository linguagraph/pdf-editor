from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QGuiApplication,
    QImage,
)
from PySide6.QtWidgets import QApplication

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
from pdfeditor.ui.organizer.organizer import MIME_PAGES, drop, insertion_slot
from pdfeditor.ui.theme import current_colors

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


def test_duplicate_is_independent_and_undoable(window: MainWindow, copy_pdf) -> None:
    """Issue #49: stamping a duplicate leaves the original alone, through undo and redo."""
    from pdfeditor.model.geometry import Point
    from pdfeditor.model.pages import TextStamp
    from pdfeditor.ui import page_ops

    view = window.open_path(copy_pdf("text_multipage"))
    session = view.session

    def texts() -> list[str]:
        with session.lock:
            doc = session.document
            return [doc.page(i).text_page(with_chars=False).text for i in range(doc.page_count)]

    original = texts()
    page_ops.duplicate_pages(session, [0])
    page_ops.run(
        session, "Stamp", lambda doc: doc.page(1).stamp_text(TextStamp("COPY", Point(72, 800)))
    )
    after = texts()
    assert len(after) == 6 and "COPY" in after[1] and after[0] == original[0]
    window.act_undo.trigger()  # the stamp
    window.act_undo.trigger()  # the duplicate
    assert texts() == original
    window.act_redo.trigger()
    window.act_redo.trigger()
    assert texts() == after


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
    index = window.tabs.currentIndex()
    assert window.tabs.tabText(index) == "Combined.pdf" and window.tabs.is_dirty(index)


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


# -- drop indicator (#78) -----------------------------------------------------------------


def pages_mime(view, rows) -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME_PAGES, f"{view.session.id}:{','.join(map(str, rows))}".encode())
    return mime


def drag(grid, mime, pos, drop=False):
    """Send a drag enter+move (and optionally a drop) to the grid; returns the move event."""
    act = Qt.DropAction.MoveAction
    btn, mod = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    p = QPointF(pos)
    QApplication.sendEvent(grid.viewport(), QDragEnterEvent(pos, act, mime, btn, mod))
    move = QDragMoveEvent(pos, act, mime, btn, mod)
    QApplication.sendEvent(grid.viewport(), move)
    if drop and move.isAccepted():
        QApplication.sendEvent(grid.viewport(), QDropEvent(p, act, mime, btn, mod))
    return move


def organizer_grid(qtbot, window, copy_pdf):
    view = window.open_path(copy_pdf("text_multipage"))
    tab = window.current_tab()
    tab.set_organizing(True)
    grid = tab.organizer.grid
    grid.doItemsLayout()
    qtbot.waitUntil(lambda: grid.visualRect(grid.model().index(4, 0)).isValid())
    rects = [grid.visualRect(grid.model().index(i, 0)) for i in range(5)]
    assert rects[0].top() == rects[1].top()
    return view, tab, grid, rects


def gap_pos(rects, slot):
    """A point in the gap before page ``slot`` (left of it when it starts a line)."""
    if rects[slot].top() != rects[slot - 1].top():
        return QPoint(rects[slot].left() - 2, rects[slot].center().y())
    return QPoint((rects[slot - 1].right() + rects[slot].left()) // 2, rects[slot].center().y())


def test_insertion_slot_math() -> None:
    def cell(i: int) -> QRect:
        return QRect((i % 3) * 110, (i // 3) * 150, 100, 140)

    def slot(x, y, count=7):
        return insertion_slot(count, cell, QPoint(x, y), 400, 10)

    assert slot(20, 50)[0] == 0
    assert slot(80, 50)[0] == 1
    before, rect = slot(215, 50)
    assert before == 2 and rect is not None and 209 < rect.center().x() < 220
    assert slot(390, 50)[0] == 3
    assert slot(5, 200)[0] == 3
    assert slot(390, 400)[0] == 7 and slot(10, 900)[0] == 6
    assert slot(20, -50)[0] == 0
    assert slot(20, 145)[0] == 0  # vertical gap belongs to the line above
    assert insertion_slot(0, cell, QPoint(0, 0), 400, 10) == (0, None)
    assert insertion_slot(1, cell, QPoint(10, 10), 400, 10)[0] == 0
    assert insertion_slot(1, cell, QPoint(90, 10), 400, 10)[0] == 1


def test_drag_shows_insertion_line_between_pages(qtbot, window: MainWindow, copy_pdf) -> None:
    view, _tab, grid, rects = organizer_grid(qtbot, window, copy_pdf)
    move = drag(grid, pages_mime(view, [0]), gap_pos(rects, 2))
    ok, slot_ = move.isAccepted(), grid.drop_slot
    assert ok and slot_ == 2, (ok, slot_)
    ind = grid.drop_indicator
    assert ind is not None and rects[1].right() < ind.center().x() < rects[2].left()
    grid.repaint()
    img = grid.viewport().grab().toImage()
    got = QColor(img.pixel(ind.center().x(), ind.center().y()))
    want = QColor(current_colors().accent_text)
    assert abs(got.red() - want.red()) + abs(got.green() - want.green()) < 20
    QApplication.sendEvent(grid.viewport(), QDragLeaveEvent())
    assert grid.drop_indicator is None and grid.drop_slot is None


def test_drop_in_gap_moves_to_indicated_slot(qtbot, window: MainWindow, copy_pdf) -> None:
    view, tab, grid, rects = organizer_grid(qtbot, window, copy_pdf)
    tab.organizer.select_pages([0])
    drag(grid, pages_mime(view, [0]), gap_pos(rects, 3), drop=True)
    assert [x.split()[1] for x in first_lines(view)] == ["2", "3", "1", "4", "5"]
    assert tab.organizer.selected_pages() == [2]
    assert window.act_undo.text() == "&Undo Move Page"
    window.act_undo.trigger()
    assert [x.split()[1] for x in first_lines(view)] == ["1", "2", "3", "4", "5"]


def test_drop_before_first_and_after_last_multiselect(qtbot, window: MainWindow, copy_pdf) -> None:
    view, tab, grid, rects = organizer_grid(qtbot, window, copy_pdf)
    tab.organizer.select_pages([1, 3])
    far = QPoint(grid.viewport().width() - 4, rects[4].center().y())
    drag(grid, pages_mime(view, [1, 3]), far, drop=True)
    assert [x.split()[1] for x in first_lines(view)] == ["1", "3", "5", "2", "4"]
    assert tab.organizer.selected_pages() == [3, 4]
    rects = [grid.visualRect(grid.model().index(i, 0)) for i in range(5)]
    left = QPoint(max(rects[0].left() - 2, 1), rects[0].center().y())
    drag(grid, pages_mime(view, [4]), left, drop=True)
    assert [x.split()[1] for x in first_lines(view)] == ["4", "1", "3", "5", "2"]


def test_drop_on_own_position_is_noop(qtbot, window: MainWindow, copy_pdf) -> None:
    view, tab, grid, rects = organizer_grid(qtbot, window, copy_pdf)
    tab.organizer.select_pages([1, 2])
    undo_text = window.act_undo.text()
    for slot in (1, 2, 3):
        move = drag(grid, pages_mime(view, [1, 2]), gap_pos(rects, slot), drop=True)
        assert move.isAccepted()
    assert [x.split()[1] for x in first_lines(view)] == ["1", "2", "3", "4", "5"]
    assert window.act_undo.text() == undo_text


def test_drag_rejects_foreign_pages_and_unknown_data(qtbot, window: MainWindow, copy_pdf) -> None:
    view, _tab, grid, rects = organizer_grid(qtbot, window, copy_pdf)
    foreign = QMimeData()
    foreign.setData(MIME_PAGES, b"99999:0")
    text = QMimeData()
    text.setText("hello")
    for mime in (foreign, text):
        move = drag(grid, mime, gap_pos(rects, 2), drop=True)
        assert not move.isAccepted() and grid.drop_indicator is None
    assert [x.split()[1] for x in first_lines(view)] == ["1", "2", "3", "4", "5"]


def test_file_drop_inserts_at_indicated_slot(
    qtbot, window: MainWindow, copy_pdf, tmp_path: Path
) -> None:
    view, tab, grid, rects = organizer_grid(qtbot, window, copy_pdf)
    png = tmp_path / "pic.png"
    Image.new("RGB", (100, 100), (255, 0, 0)).save(png)
    files = QMimeData()
    files.setUrls([QUrl.fromLocalFile(str(png))])
    move = drag(grid, files, gap_pos(rects, 1), drop=True)
    assert move.isAccepted() and grid.drop_slot is None
    assert view.page_count == 6
    assert first_lines(view)[1] == "" and first_lines(view)[0].startswith("Page 1")
    assert tab.organizer.selected_pages() == [1]
