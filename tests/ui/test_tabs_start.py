"""Document tabs (close button, dirty dot, "+", overflow menu, middle-click) and the start page."""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QAbstractButton, QFileDialog, QMessageBox, QWidget

from pdfeditor.ui.dialogs.properties import PropertiesDialog
from pdfeditor.ui.document_tabs import TabCloseButton
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.recent_files import MAX_RECENT, RecentFiles
from pdfeditor.ui.start_page import MISSING_ROLE, PATH_ROLE, PINNED_ROLE

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    _discard_on_close(w)  # pytest-qt closes the window before fixtures are torn down
    qtbot.addWidget(w)
    w.resize(1100, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.close()


def _discard_on_close(w: MainWindow) -> None:
    w.ask_save_changes = lambda _session: QMessageBox.StandardButton.Discard  # type: ignore[method-assign]


@pytest.fixture
def pdf_copy(fixture_pdf, tmp_path: Path):
    def make(name: str, as_name: str | None = None) -> Path:
        dst = tmp_path / f"{as_name or name}.pdf"
        shutil.copy2(fixture_pdf(name), dst)
        return dst.resolve()

    return make


def _edit_title(window: MainWindow, title: str) -> None:
    view = window.current_view()
    assert view is not None
    dialog = PropertiesDialog(view.session, window)
    dialog.fields["title"].setText(title)
    dialog.accept()


def _modes(window: MainWindow) -> list[str]:
    bar = window.tabs.bar
    buttons = [bar.close_button(i) for i in range(bar.count())]
    return [b.mode if b is not None else "?" for b in buttons]


# -- document tabs -----------------------------------------------------------------------------
def test_close_button_on_active_and_hovered_tab(window: MainWindow, fixture_pdf, qtbot) -> None:
    window.open_path(fixture_pdf("images"))
    window.open_path(fixture_pdf("outline"))
    bar = window.tabs.bar
    assert _modes(window) == ["hidden", "close"]  # only the active tab shows its cross
    close = bar.close_button(1)
    assert isinstance(close, TabCloseButton) and close.isVisible()
    assert close.accessibleName() == "Close outline.pdf" and not close.icon().isNull()
    assert bar.close_button(0).icon().isNull()

    qtbot.mouseMove(bar, bar.tabRect(0).center())
    qtbot.waitUntil(lambda: _modes(window) == ["close", "close"])
    qtbot.mouseMove(window.ribbon, QPoint(5, 5))
    qtbot.waitUntil(lambda: _modes(window) == ["hidden", "close"])

    window.tabs.setCurrentIndex(0)
    assert _modes(window) == ["close", "hidden"]
    # clicking the (custom) close button closes that tab
    qtbot.mouseClick(bar.close_button(0), Qt.MouseButton.LeftButton)
    assert window.tabs.count() == 1 and window.tabs.tabText(0) == "outline.pdf"


def test_dirty_dot_follows_edits_and_save(window: MainWindow, pdf_copy) -> None:
    view = window.open_path(pdf_copy("images"))
    window.open_path(pdf_copy("outline"))
    window.tabs.setCurrentIndex(0)
    assert not window.tabs.is_dirty(0)
    _edit_title(window, "Changed")
    assert view is not None and view.session.is_dirty
    assert window.tabs.tabText(0) == "images.pdf"  # no "*": the dot shows it
    assert window.tabs.is_dirty(0) and not window.tabs.is_dirty(1)
    assert window.tabs.tabBar().accessibleTabName(0) == "images.pdf, unsaved changes"
    assert _modes(window) == ["dirty", "hidden"]  # the dot even on the active tab
    window.act_undo.trigger()
    assert not window.tabs.is_dirty(0) and _modes(window) == ["close", "hidden"]
    window.act_redo.trigger()
    assert window.tabs.is_dirty(0)
    assert window.save()
    assert not window.tabs.is_dirty(0) and _modes(window) == ["close", "hidden"]
    assert window.tabs.tabBar().accessibleTabName(0) == "images.pdf"


def test_tooltip_shows_full_path(window: MainWindow, pdf_copy) -> None:
    path = pdf_copy("images")
    window.open_path(path)
    assert window.tabs.tabToolTip(0) == str(path)


def test_plus_button_opens_a_file(window: MainWindow, pdf_copy, monkeypatch, qtbot) -> None:
    path = pdf_copy("outline")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([str(path)], ""))
    )
    plus = window.tabs.plus_button
    assert plus.accessibleName() == "Open a file" and not plus.icon().isNull()
    window.open_path(pdf_copy("images"))  # the strip (and its corner) shows with a document
    qtbot.mouseClick(plus, Qt.MouseButton.LeftButton)
    assert window.tabs.count() == 2 and window.current_view().session.path == path


def test_overflow_menu_lists_and_switches(window: MainWindow, pdf_copy, qtbot) -> None:
    window.open_path(pdf_copy("images"))
    assert not window.tabs.overflow_button.isVisible()
    window.resize(520, 700)
    names = ["a_long_document_name", "another_long_document", "third_long_document", "fourth"]
    for name in names:
        window.open_path(pdf_copy("outline", name))
    qtbot.waitUntil(window.tabs.overflow_button.isVisible)
    assert window.tabs.overflow_button.accessibleName() == "All open documents"
    _edit_title(window, "dirty")
    window.tabs.fill_documents_menu()
    actions = window.tabs.documents_menu.actions()
    assert [a.text().removesuffix("  \u2022") for a in actions] == [
        "images.pdf",
        *(f"{n}.pdf" for n in names),
    ]
    assert actions[-1].isChecked() and actions[-1].text().endswith("\u2022")
    actions[0].trigger()
    assert window.tabs.currentIndex() == 0
    window.resize(1600, 800)
    qtbot.waitUntil(lambda: not window.tabs.overflow_button.isVisible())


def test_middle_click_closes_and_still_prompts(
    window: MainWindow, pdf_copy, monkeypatch, qtbot
) -> None:
    window.open_path(pdf_copy("images"))
    window.open_path(pdf_copy("outline"))
    bar = window.tabs.bar
    qtbot.mouseClick(bar, Qt.MouseButton.MiddleButton, pos=bar.tabRect(0).center())
    assert window.tabs.count() == 1 and window.tabs.tabText(0) == "outline.pdf"

    _edit_title(window, "unsaved")
    asked: list[str] = []
    answer = QMessageBox.StandardButton.Cancel

    def ask(session):
        asked.append(session.display_name)
        return answer

    monkeypatch.setattr(window, "ask_save_changes", ask)
    qtbot.mouseClick(bar, Qt.MouseButton.MiddleButton, pos=bar.tabRect(0).center())
    assert asked == ["outline.pdf"] and window.tabs.count() == 1  # cancelled
    answer = QMessageBox.StandardButton.Discard
    qtbot.mouseClick(bar, Qt.MouseButton.MiddleButton, pos=bar.tabRect(0).center())
    assert asked == ["outline.pdf"] * 2 and window.tabs.count() == 0
    assert window.welcome.isVisible()


# -- start page --------------------------------------------------------------------------------
def test_start_page_shown_only_without_documents(window: MainWindow, fixture_pdf) -> None:
    start = window.start_page
    assert window.welcome is start and start.isVisible() and not window.tabs.isVisible()
    window.open_path(fixture_pdf("images"))
    assert not start.isVisible() and window.tabs.isVisible()
    window.close_current()
    assert start.isVisible()


def test_start_page_widgets_are_named(window: MainWindow) -> None:
    unnamed = []
    for w in window.start_page.findChildren(QWidget):
        if not w.isVisibleTo(window) or w.focusPolicy() == Qt.FocusPolicy.NoFocus:
            continue
        text = w.text() if isinstance(w, QAbstractButton) else ""
        if not (w.accessibleName() or text or w.toolTip()):
            unnamed.append(type(w).__name__)
    assert unnamed == []


def _window_with_recent(qtbot, paths: list[Path], pinned: list[Path] = ()) -> MainWindow:
    settings = QSettings()
    settings.setValue("recent_files", [str(p) for p in paths])
    settings.setValue("recent_pinned", [str(p) for p in pinned])
    w = MainWindow()
    _discard_on_close(w)
    qtbot.addWidget(w)
    w.resize(1100, 800)
    w.show()
    qtbot.waitExposed(w)
    return w


def _recent_paths(w: MainWindow) -> list[str]:
    view = w.start_page.recent_view
    return [view.item(i).data(PATH_ROLE) for i in range(view.count())]


def test_recent_grid_has_thumbnails(qtbot, pdf_copy, tmp_path: Path) -> None:
    files = [pdf_copy("images"), pdf_copy("outline")]
    encrypted = pdf_copy("encrypted")  # needs a password: no preview, but no prompt either
    w = _window_with_recent(qtbot, [*files, encrypted])
    try:
        start = w.start_page
        assert _recent_paths(w) == [str(p) for p in [*files, encrypted]]
        qtbot.waitUntil(lambda: not start.thumbnails_pending(), timeout=10000)
        assert all(start.has_thumbnail(str(p)) for p in files)
        assert not start.has_thumbnail(str(encrypted))
        item = start.recent_view.item(0)
        pixmap = item.data(Qt.ItemDataRole.DecorationRole)
        assert not pixmap.isNull() and pixmap.height() > pixmap.width()  # a portrait page
        assert item.text() == "images.pdf" and item.toolTip() == str(files[0])
        assert start.recent_view.isVisible() and not start.empty_label.isVisible()
        # a refresh with unchanged files reuses the previews instead of rendering again
        start.refresh()
        assert not start.thumbnails_pending()
        assert not start.recent_view.item(1).data(Qt.ItemDataRole.DecorationRole).isNull()
    finally:
        w.close()


def test_empty_recent_list(window: MainWindow) -> None:
    start = window.start_page
    assert start.recent_view.count() == 0
    assert start.empty_label.isVisible() and not start.recent_view.isVisible()


def test_pin_order_persists(qtbot, pdf_copy) -> None:
    files = [pdf_copy(n) for n in ("images", "outline", "report")]
    w = _window_with_recent(qtbot, files)
    try:
        view = w.start_page.recent_view
        menu = view.build_menu(view.item(2))  # report.pdf
        pin = next(a for a in menu.actions() if a.text() == "&Pin to Top")
        pin.trigger()
        assert _recent_paths(w) == [str(files[2]), str(files[0]), str(files[1])]
        assert view.item(0).data(PINNED_ROLE) and not view.item(1).data(PINNED_ROLE)
        assert "pinned" in view.item(0).data(Qt.ItemDataRole.AccessibleTextRole)
        # File > Open Recent shows the same order
        w._fill_recent_menu()
        labels = [a.text() for a in w.recent_menu.actions()]
        assert labels[0].endswith("report.pdf") and labels[1].endswith("images.pdf")
        # opening another file doesn't move the pinned one, and many files don't push it out
        w.open_path(files[1])
        w.close_current()
        assert _recent_paths(w)[:2] == [str(files[2]), str(files[1])]
        for i in range(MAX_RECENT + 2):
            w._add_recent(Path(f"C:/nowhere/file{i}.pdf"))
        assert _recent_paths(w)[0] == str(files[2])
        assert len(_recent_paths(w)) == MAX_RECENT + 1
        # Clear List keeps pins
        w.recent.clear()
        assert _recent_paths(w) == [str(files[2])]
    finally:
        w.close()
    assert RecentFiles(QSettings()).files() == [str(files[2])]
    w2 = _window_with_recent(qtbot, [*files], [files[2]])
    try:
        view = w2.start_page.recent_view
        assert _recent_paths(w2)[0] == str(files[2])
        menu = view.build_menu(view.item(0))
        next(a for a in menu.actions() if a.text() == "Un&pin").trigger()
        assert _recent_paths(w2) == [str(p) for p in files]
        assert RecentFiles(QSettings()).pinned() == []
    finally:
        w2.close()


def test_remove_and_missing_files(qtbot, pdf_copy, tmp_path: Path) -> None:
    gone = tmp_path / "gone.pdf"
    files = [pdf_copy("images"), gone, pdf_copy("outline")]
    w = _window_with_recent(qtbot, files)
    try:
        view = w.start_page.recent_view
        missing = view.item(1)
        assert missing.data(MISSING_ROLE) and not view.item(0).data(MISSING_ROLE)
        assert "file not found" in missing.data(Qt.ItemDataRole.AccessibleTextRole)
        menu = view.build_menu(missing)
        assert not next(a for a in menu.actions() if a.text() == "&Open").isEnabled()
        # Delete removes the current card
        view.setFocus()
        view.setCurrentRow(1)
        qtbot.keyClick(view, Qt.Key.Key_Delete)
        assert _recent_paths(w) == [str(files[0]), str(files[2])]
        assert w._recent_files() == [str(files[0]), str(files[2])]
        # and so does the context menu
        menu = view.build_menu(view.item(1))
        next(a for a in menu.actions() if a.text() == "&Remove from List").trigger()
        assert _recent_paths(w) == [str(files[0])]
    finally:
        w.close()


def test_keyboard_open_with_enter(qtbot, pdf_copy) -> None:
    files = [pdf_copy("images"), pdf_copy("outline")]
    w = _window_with_recent(qtbot, files)
    try:
        view = w.start_page.recent_view
        view.setFocus()
        view.setCurrentRow(0)
        qtbot.keyClick(view, Qt.Key.Key_Right)
        assert view.currentRow() == 1
        qtbot.keyClick(view, Qt.Key.Key_Return)
        assert w.current_view() is not None and w.current_view().session.path == files[1]
        assert not w.start_page.isVisible()
    finally:
        w.close()


def test_open_button_and_drop_zone(window: MainWindow, pdf_copy, monkeypatch, qtbot) -> None:
    path = pdf_copy("images")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([str(path)], ""))
    )
    start = window.start_page
    start.open_button.setFocus()
    qtbot.keyClick(start.open_button, Qt.Key.Key_Space)
    assert window.tabs.count() == 1
    window.close_current()
    # a file dropped on the drop zone opens
    other = pdf_copy("outline")
    mime = QMimeData()
    mime.setUrls(
        [QUrl.fromLocalFile(str(other)), QUrl.fromLocalFile(str(other.with_suffix(".txt")))]
    )
    zone = start.drop_zone
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    zone.dropEvent(event)
    assert window.tabs.count() == 1 and window.current_view().session.path == other
    assert zone.property("dragging") is False


def test_quick_actions_trigger_their_actions(
    window: MainWindow, pdf_copy, monkeypatch, qtbot
) -> None:
    start = window.start_page
    expected = {
        "Combine files": window.organize.act_combine,
        "Create from Office": window.export.act_from_office,
        "Compare files": window.compare.act_compare,
    }
    assert list(start.quick_buttons) == [*expected, "OCR a scan"]
    for label, action in expected.items():
        action.triggered.disconnect()  # no real dialogs in a test
        fired: list[bool] = []
        action.triggered.connect(lambda _=False, f=fired: f.append(True))
        button = start.quick_buttons[label]
        assert button.isVisible() and button.accessibleName() == label
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert fired == [True], label

    # OCR a scan: choose a file, open it, then run OCR on it
    scan = pdf_copy("scanned")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(scan), ""))
    )
    window.tools.act_ocr.triggered.disconnect()
    ocr: list[Path] = []
    window.tools.act_ocr.triggered.connect(
        lambda _=False: ocr.append(window.current_view().session.path)
    )
    qtbot.mouseClick(start.quick_buttons["OCR a scan"], Qt.MouseButton.LeftButton)
    assert ocr == [scan]
    window.close_current()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    qtbot.mouseClick(start.quick_buttons["OCR a scan"], Qt.MouseButton.LeftButton)
    assert ocr == [scan] and window.tabs.count() == 0


def test_keyboard_tab_order(qtbot, pdf_copy) -> None:
    w = _window_with_recent(qtbot, [pdf_copy("images")])
    try:
        start = w.start_page
        order = [start.open_button, *start.quick_buttons.values(), start.recent_view]
        for a, b in itertools.pairwise(order):
            assert _next_focusable(a) is b, (a, b)
    finally:
        w.close()


def _next_focusable(w: QWidget) -> QWidget:
    nxt = w.nextInFocusChain()
    while nxt.focusPolicy() == Qt.FocusPolicy.NoFocus or not nxt.isVisible():
        nxt = nxt.nextInFocusChain()
    return nxt


def test_thumbnail_job_stops_on_close(qtbot, pdf_copy) -> None:
    files = [pdf_copy("large_1000")] + [pdf_copy("images", f"copy{i}") for i in range(8)]
    w = _window_with_recent(qtbot, files)
    start = w.start_page
    assert start.thumbnails_pending()
    w.close()  # while the job runs
    assert not start.thumbnails_pending()
    qtbot.wait(100)  # queued signals from the cancelled job must not reach the page
    start.refresh()  # closed: does nothing, starts no job
    assert not start.thumbnails_pending()
