"""The Combine Files panel in the main area (#76)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from pdfeditor.ui.combine.grid import remove_rect
from pdfeditor.ui.combine.model import (
    DETAIL_ROLE,
    MIME_COMBINE,
    CombineModel,
    EntryState,
)
from pdfeditor.ui.dialogs.pages import SplitDialog
from pdfeditor.ui.jobs import wait_for
from pdfeditor.ui.main_window import MainWindow
from tests.ui.test_a11y_u10 import unnamed
from tests.ui.test_organize_ui import accepted

pytestmark = pytest.mark.gui

ACTIONS = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
NONE = Qt.MouseButton.NoButton
NOMOD = Qt.KeyboardModifier.NoModifier


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1200, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.combine.close_panel()
    w.jobs.cancel_all()
    w.toasts.clear()
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    QApplication.processEvents()  # the toasts' queued placement, before the window goes
    w.deleteLater()


@pytest.fixture
def copy_pdf(fixture_pdf, tmp_path: Path):
    def make(name: str) -> Path:
        dst = tmp_path / f"{name}.pdf"
        shutil.copy2(fixture_pdf(name), dst)
        return dst

    return make


@pytest.fixture
def png(tmp_path: Path) -> Path:
    path = tmp_path / "cover.png"
    Image.new("RGB", (120, 160), (0, 0, 200)).save(path)
    return path


def settle(qtbot, panel) -> None:
    qtbot.waitUntil(lambda: not panel.describing(), timeout=20000)


def files_of(panel) -> list[str]:
    return [e.path.name for e in panel.model.entries]


def mime_of(*paths: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def drop(widget, mime: QMimeData, pos: QPoint) -> None:
    widget.dragEnterEvent(QDragEnterEvent(pos, ACTIONS, mime, NONE, NOMOD))
    widget.dropEvent(QDropEvent(QPointF(pos), ACTIONS, mime, NONE, NOMOD))


def open_three(window, qtbot, copy_pdf, png):
    panel = window.combine.panel
    window.combine.open_panel([png, copy_pdf("outline"), copy_pdf("report")])
    settle(qtbot, panel)
    qtbot.waitUntil(lambda: panel.grid.visualRect(panel.model.index(2)).isValid())
    return panel


def toast_texts(window) -> list[str]:
    return [window.toasts.last_text()]


# -- 1: the action opens the panel in the main area -----------------------------------------------
def test_action_opens_and_closes_the_panel(qtbot, window: MainWindow) -> None:
    panel = window.combine.panel
    assert window.start_page.isVisible() and not panel.isVisible()
    window.organize.act_combine.trigger()
    assert panel.isVisible() and window.combine.active
    assert not window.start_page.isVisible() and not window.tabs.isVisible()
    assert window.windowTitle() == "Combine Files — Elanero - PDF Editor"
    assert panel.drop_zone.isVisible() and not panel.grid.isVisible()
    assert not panel.combine_button.isEnabled()
    qtbot.keyClick(panel.focus_target(), Qt.Key.Key_Escape)
    assert not panel.isVisible() and window.start_page.isVisible()
    assert window.windowTitle() == "Elanero - PDF Editor"


def test_panel_over_a_document_hides_docks_and_document_actions(
    qtbot, window: MainWindow, copy_pdf
) -> None:
    view = window.open_path(copy_pdf("report"))
    window.nav_dock.show()
    window.inspector_dock.show()
    assert window.tabs.isVisible() and window.nav_dock.isVisible()
    window.organize.act_combine.trigger()
    panel = window.combine.panel
    assert panel.isVisible() and not window.tabs.isVisible()
    assert not window.nav_dock.isVisible() and not window.inspector_dock.isVisible()
    assert not window.act_save.isEnabled() and not window.act_close.isEnabled()
    qtbot.keyClick(panel.focus_target(), Qt.Key.Key_Escape)
    assert window.tabs.isVisible() and not panel.isVisible()
    assert window.nav_dock.isVisible() and window.inspector_dock.isVisible()
    assert window.act_close.isEnabled()
    assert window.focusWidget() is view or view.isAncestorOf(window.focusWidget())


# -- 2: cards ---------------------------------------------------------------------------------
def test_cards_show_previews_and_counts(qtbot, window: MainWindow, copy_pdf, png) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    model = panel.model
    details = [model.data(model.index(i), DETAIL_ROLE) for i in range(3)]
    assert details == ["Image", "6 pages", model.data(model.index(2), DETAIL_ROLE)]
    assert all(e.state is EntryState.READY for e in model.entries)
    assert all(e.preview is not None and not e.preview.isNull() for e in model.entries)
    assert panel.summary.text().startswith("3 files")
    assert panel.combine_button.isEnabled()
    assert panel.grid.isVisible() and not panel.drop_zone.isVisible()
    names = [model.data(model.index(i), Qt.ItemDataRole.AccessibleTextRole) for i in range(3)]
    assert names[0] == "cover.png, Image, file 1 of 3"
    panel.model.remove_rows([1, 2])
    assert not panel.combine_button.isEnabled()
    assert "Add at least two files" in panel.status.text()


# -- 3: files dropped from the file manager ------------------------------------------------------
def test_drop_files_from_explorer(qtbot, window: MainWindow, copy_pdf, png, tmp_path) -> None:
    window.organize.act_combine.trigger()
    panel = window.combine.panel
    notes = tmp_path / "notes.txt"
    notes.write_text("x")
    drop(panel.drop_zone, mime_of(copy_pdf("report"), png, notes), QPoint(20, 20))
    assert files_of(panel) == ["report.pdf", "cover.png"]
    assert "1 file skipped" in toast_texts(window)[-1]
    settle(qtbot, panel)
    qtbot.waitUntil(lambda: panel.grid.visualRect(panel.model.index(0)).isValid())
    card = panel.grid.card_at(0)
    drop(panel.grid, mime_of(copy_pdf("outline")), QPoint(card.left() + 5, card.center().y()))
    assert files_of(panel) == ["outline.pdf", "report.pdf", "cover.png"]
    settle(qtbot, panel)


def test_window_drop_while_combining_adds_to_the_panel(
    qtbot, window: MainWindow, copy_pdf, png
) -> None:
    window.open_path(copy_pdf("report"))
    window.organize.act_combine.trigger()
    panel = window.combine.panel
    before = window.tabs.count()
    mime = mime_of(copy_pdf("outline"), png)
    window.dragEnterEvent(QDragEnterEvent(QPoint(5, 5), ACTIONS, mime, NONE, NOMOD))
    window.dropEvent(QDropEvent(QPointF(5, 5), ACTIONS, mime, NONE, NOMOD))
    assert files_of(panel) == ["outline.pdf", "cover.png"] and window.tabs.count() == before
    settle(qtbot, panel)


# -- 4: dragging cards to reorder ----------------------------------------------------------------
def test_drag_reorders_with_an_insertion_marker(qtbot, window: MainWindow, copy_pdf, png) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    grid, model = panel.grid, panel.model
    card = grid.card_at(2)
    pos = QPoint(card.right() - 10, card.center().y())
    assert grid.insertion_index(pos) == 3
    assert (
        grid.insertion_index(QPoint(grid.card_at(0).left() + 5, grid.card_at(0).center().y())) == 0
    )
    below = QPoint(10, grid.viewport().height() - 2)
    assert grid.insertion_index(below) == 3

    mime = model.mimeData([model.index(0)])
    assert mime.hasFormat(MIME_COMBINE)
    grid.dragEnterEvent(QDragEnterEvent(pos, Qt.DropAction.MoveAction, mime, NONE, NOMOD))
    grid.dragMoveEvent(QDragMoveEvent(pos, Qt.DropAction.MoveAction, mime, NONE, NOMOD))
    bar = grid.drop_indicator()
    assert bar is not None and bar.width() == 3 and bar.height() == grid.card_at(2).height()
    grid.dropEvent(QDropEvent(QPointF(pos), Qt.DropAction.MoveAction, mime, NONE, NOMOD))
    assert files_of(panel) == ["outline.pdf", "report.pdf", "cover.png"]
    assert grid.selected_rows() == [2]
    assert grid.drop_indicator() is None
    # the markers leave with the drag
    grid.dragEnterEvent(QDragEnterEvent(pos, Qt.DropAction.MoveAction, mime, NONE, NOMOD))
    assert grid.drop_indicator() is not None
    grid.dragLeaveEvent(None)  # type: ignore[arg-type]
    assert grid.drop_indicator() is None


def test_a_drag_from_another_list_is_refused(qtbot, window: MainWindow, copy_pdf, png) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    foreign = QMimeData()
    foreign.setData(MIME_COMBINE, b"12345:0")
    assert panel.model.internal_rows(foreign) is None
    pos = QPoint(10, 10)
    event = QDragEnterEvent(pos, Qt.DropAction.MoveAction, foreign, NONE, NOMOD)
    event.ignore()
    panel.grid.dragEnterEvent(event)
    assert not event.isAccepted() and panel.grid.drop_indicator() is None
    before = files_of(panel)
    panel.grid.dropEvent(QDropEvent(QPointF(pos), Qt.DropAction.MoveAction, foreign, NONE, NOMOD))
    assert files_of(panel) == before


def test_moves_keep_the_selection_and_do_not_reset(qtbot, window, copy_pdf, png) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    model = panel.model
    resets: list[bool] = []
    model.modelReset.connect(lambda: resets.append(True))
    panel.grid.select_rows([0])
    persistent = [model.index(i).siblingAtRow(i) for i in range(3)]
    assert model.move_rows([0], 3) == [2]
    assert files_of(panel) == ["outline.pdf", "report.pdf", "cover.png"]
    assert resets == [] and model.entry(persistent[0].row()) is not None


# -- 5: keyboard --------------------------------------------------------------------------------
def test_keyboard_reorders_removes_and_names(qtbot, window: MainWindow, copy_pdf, png) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    grid = panel.grid
    grid.setFocus()
    grid.select_rows([0])
    ctrl = Qt.KeyboardModifier.ControlModifier
    qtbot.keyClick(grid, Qt.Key.Key_Right, ctrl)
    assert files_of(panel) == ["outline.pdf", "cover.png", "report.pdf"]
    assert grid.selected_rows() == [1]
    qtbot.keyClick(grid, Qt.Key.Key_End, ctrl)
    assert files_of(panel)[-1] == "cover.png"
    qtbot.keyClick(grid, Qt.Key.Key_Left, ctrl)
    qtbot.keyClick(grid, Qt.Key.Key_Home, ctrl)
    assert files_of(panel)[0] == "cover.png"
    # the menu
    menu = grid.build_menu(1)
    labels = [a.text().replace("&", "") for a in menu.actions() if a.text()]
    assert labels == [
        "Choose Pages…",
        "Move to Start",
        "Move Earlier",
        "Move Later",
        "Move to End",
        "Show in Folder",
        "Remove",
    ]
    assert not grid.build_menu(0).actions()[0].isEnabled()  # an image has no pages to choose
    qtbot.keyClick(grid, Qt.Key.Key_Delete)
    assert len(files_of(panel)) == 2
    qtbot.keyClick(grid, Qt.Key.Key_Backspace)
    assert len(files_of(panel)) == 1
    assert unnamed(panel) == []


# -- 6: the remove button -----------------------------------------------------------------------
def test_remove_button_removes_without_selecting(qtbot, window, copy_pdf, png) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    grid = panel.grid
    grid.clearSelection()
    button = remove_rect(grid.card_at(1))
    qtbot.mouseMove(grid.viewport(), button.center())
    qtbot.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=button.center())
    assert files_of(panel) == ["cover.png", "report.pdf"]


# -- 7: password protected files -----------------------------------------------------------------
def test_encrypted_file_is_unlocked_with_its_password(
    qtbot, window: MainWindow, copy_pdf, monkeypatch
) -> None:
    window.combine.open_panel([copy_pdf("encrypted"), copy_pdf("report")])
    panel = window.combine.panel
    settle(qtbot, panel)
    model = panel.model
    assert model.entry(0).state is EntryState.LOCKED and not panel.combine_button.isEnabled()
    assert "Unlock 1 password-protected file" in panel.status.text()
    answers = iter([("nope", True), ("user", True)])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    panel.activate(0)
    settle(qtbot, panel)
    assert model.entry(0).state is EntryState.LOCKED
    assert any("Incorrect password" in t for t in toast_texts(window))
    panel.activate(0)
    settle(qtbot, panel)
    assert model.entry(0).state is EntryState.READY and panel.combine_button.isEnabled()
    assert model.sources()[0].password == "user"


# -- 8: unreadable files -------------------------------------------------------------------------
def test_unreadable_file_blocks_combine(qtbot, window: MainWindow, copy_pdf, tmp_path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    window.combine.open_panel([junk, copy_pdf("report")])
    panel = window.combine.panel
    settle(qtbot, panel)
    assert panel.model.entry(0).state is EntryState.ERROR
    assert not panel.combine_button.isEnabled()
    assert "Remove 1 file that can't be opened" in panel.status.text()
    assert window.combine.combine() is None
    panel.model.remove_rows([0])
    assert "Add at least two files" in panel.status.text()


# -- 9, 10: choose pages and combine -------------------------------------------------------------
def test_choose_pages_and_combine(
    qtbot, window: MainWindow, copy_pdf, png, tmp_path, monkeypatch
) -> None:
    window.open_path(copy_pdf("outline"))
    d = SplitDialog("book", tmp_path / "parts", window)
    d.by_bookmarks.setChecked(True)
    written = window.organize.split_document(accepted(d))
    window.combine.open_panel([png, written[0], written[2]])
    panel = window.combine.panel
    settle(qtbot, panel)
    answers = iter([("9", True), ("2", True)])
    warnings: list[str] = []
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    panel.activate(2)
    assert len(warnings) == 1 and "outside" in warnings[0]
    assert panel.model.entry(2).pages == "2"
    assert panel.model.data(panel.model.index(2), DETAIL_ROLE).startswith("Pages 2 of ")
    assert panel.model.total_pages() == 4

    tabs_before = window.tabs.count()
    session = wait_for(window.combine.combine())
    assert session is not None and session.page_count == 4
    titles = [i.title for i in session.document.outline()]
    assert titles == ["cover", "book_Chapter 1", "book_Chapter 3"]
    index = window.tabs.currentIndex()
    assert window.tabs.count() == tabs_before + 1
    assert window.tabs.tabText(index) == "Combined.pdf" and window.tabs.is_dirty(index)
    assert not panel.isVisible() and not window.combine.active and panel.model.rowCount() == 0
    assert "Combined 3 files into 4 pages" in toast_texts(window)[-1]

    # the saved file opens in independent implementations too
    out = tmp_path / "combined.pdf"
    window.current_view().session.save(out)
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 4
    page = pdfium.PdfDocument(str(out))[0].render(scale=0.5).to_pil().convert("L")
    assert page.getextrema() != (255, 255)


# -- 11: closing while busy ----------------------------------------------------------------------
def test_close_while_combining_cancels(qtbot, window: MainWindow, copy_pdf, png) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    job = window.combine.combine()
    assert job is not None and not panel.combine_button.isEnabled()
    assert panel.combine_button.text() == "Combining…"
    panel.close_button.click()
    wait_for(job)
    assert window.tabs.count() == 0 and not panel.isVisible()
    assert panel.model.rowCount() == 0 and not window.combine.active


def test_closing_the_window_with_the_panel_open(qtbot, copy_pdf, png) -> None:
    w = MainWindow()
    w.show()
    qtbot.waitExposed(w)
    w.nav_dock.show()
    w.combine.open_panel([png, copy_pdf("report")])
    assert not w.nav_dock.isVisible()
    w.close()
    assert not w.combine.active and w.combine._docks[0]  # the docks were put back first
    w.deleteLater()


# -- 12: opening a document -----------------------------------------------------------------------
def test_opening_a_document_hides_the_panel_and_keeps_the_list(
    qtbot, window: MainWindow, copy_pdf, png
) -> None:
    panel = open_three(window, qtbot, copy_pdf, png)
    window.open_path(copy_pdf("text_multipage"))
    assert not panel.isVisible() and not window.combine.active and window.tabs.isVisible()
    assert panel.model.rowCount() == 3
    window.organize.act_combine.trigger()
    assert panel.isVisible() and panel.model.rowCount() == 3


# -- 13: accessibility ---------------------------------------------------------------------------
def test_panel_widgets_are_named_and_focus_cycles(qtbot, window: MainWindow, copy_pdf, png) -> None:
    window.organize.act_combine.trigger()
    panel = window.combine.panel
    assert unnamed(panel) == []
    assert panel.accessibleName() == "Combine Files"
    open_three(window, qtbot, copy_pdf, png)
    assert unnamed(panel) == []
    assert panel.grid.accessibleName() == "Files to combine"
    assert panel.close_button.accessibleName() and panel.close_button.toolTip()
    names = [window.cycle_focus() for _ in range(6)]
    assert "page" in names


def test_model_is_independent_of_widgets() -> None:
    model = CombineModel()
    assert model.add_paths([Path("a.pdf"), Path("b.png")]) == 2
    assert model.entry(1).image and not model.entry(0).image
    assert not model.ready()
    assert model.move_rows([1], 0) == [0]
    assert [e.path.name for e in model.entries] == ["b.png", "a.pdf"]
