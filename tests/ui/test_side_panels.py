"""Side panels behind icon rails (Phase U3): toggle and collapse, F4, badges, remembered
width and panel, empty states with their main action, and the Properties rail (Ctrl+E)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pdfeditor.core.commands import AddAnnotationCommand
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.geometry import Rect
from pdfeditor.services.accessibility import Status
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.panels.bookmarks import BookmarksPanel
from pdfeditor.ui.side_panels import RailButton

pytestmark = pytest.mark.gui


def make_window(qtbot, register: bool = True) -> MainWindow:
    w = MainWindow()
    if register:  # pytest-qt closes these before fixtures tear down
        qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    qtbot.waitExposed(w)
    settle(qtbot)
    return w


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    w = make_window(qtbot, register=False)  # documents get edited: closed below
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.search_panel.cancel()
    w.close()
    w.deleteLater()


def settle(qtbot) -> None:
    for _ in range(5):
        QApplication.processEvents()
    qtbot.wait(10)


def offered(empty) -> str:
    """The empty state's action with its shortcut, wherever it's shown (label or tooltip)."""
    return empty.button.toolTip() or empty.button.text()


def bookmarks(window: MainWindow) -> BookmarksPanel:
    return next(p for p in window.panels if isinstance(p, BookmarksPanel))


def click(qtbot, button: RailButton) -> None:
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)


def test_rail_lists_every_panel(window: MainWindow) -> None:
    side = window.nav_panels
    assert [p.title for p in side.panels()] == [
        "Pages",
        "Bookmarks",
        "Comments",
        "Redactions",
        "Search",
        "Attachments",
        "Layers",
        "Accessibility",
        "Tags",
    ]
    for panel in side.panels():
        button = side.button(panel)
        assert not button.icon().isNull()
        assert button.accessibleName() == panel.title == button.toolTip()
    # Pages is open on a first start; the old tab strip is gone
    assert side.current() is side.panels()[0] and side.title.text() == "Pages"
    assert window.nav_dock.isVisible() and side.rail.isVisible()


def test_click_opens_switches_and_collapses(qtbot, window: MainWindow) -> None:
    side = window.nav_panels
    marks, comments = bookmarks(window), window.comments_panel
    click(qtbot, side.button(marks))
    assert side.current() is marks and side.stack.currentWidget() is marks
    assert side.content.isVisible() and side.title.text() == "Bookmarks"
    assert side.button(marks).isChecked()
    click(qtbot, side.button(comments))
    assert side.current() is comments
    assert side.button(comments).isChecked() and not side.button(marks).isChecked()
    settle(qtbot)
    open_width = window.nav_dock.width()
    assert open_width > side.rail.width() + 100

    click(qtbot, side.button(comments))  # the active icon again: collapse, rail stays
    settle(qtbot)
    assert side.current() is None and not side.content.isVisible()
    assert not any(b.isChecked() for b in side.rail.buttons())
    assert window.nav_dock.isVisible() and side.rail.isVisible()
    assert window.nav_dock.width() == side.rail.width()

    click(qtbot, side.button(marks))  # opens again at the width it had
    settle(qtbot)
    assert side.current() is marks and abs(window.nav_dock.width() - open_width) <= 4

    qtbot.mouseClick(side.close_button, Qt.MouseButton.LeftButton)  # header ✕ also collapses
    assert side.current() is None


def test_f4_toggles_the_left_side(qtbot, window: MainWindow) -> None:
    window.activateWindow()
    qtbot.keyClick(window, Qt.Key.Key_F4)
    assert not window.nav_dock.isVisible() and not window.act_nav_pane.isChecked()
    qtbot.keyClick(window, Qt.Key.Key_F4)
    assert window.nav_dock.isVisible() and window.act_nav_pane.isChecked()
    # opening a panel from elsewhere brings a hidden side back
    window.act_nav_pane.trigger()
    window.show_comments()
    assert window.nav_dock.isVisible() and window.nav_panels.current() is window.comments_panel


def test_arrow_keys_move_along_the_rail(qtbot, window: MainWindow) -> None:
    side = window.nav_panels
    first, second = side.rail.buttons()[:2]
    first.setFocus()
    qtbot.keyClick(first, Qt.Key.Key_Down)
    assert second.hasFocus()
    qtbot.keyClick(second, Qt.Key.Key_Up)
    assert first.hasFocus()


def test_badges_follow_comments_hits_and_problems(qtbot, window: MainWindow, fixture_pdf) -> None:
    side = window.nav_panels
    view = window.open_path(fixture_pdf("text_multipage"))
    comments = side.button(window.comments_panel)
    assert comments.count == 0 and comments.badge_text() == ""

    note = AnnotationModel(AnnotationType.TEXT, 0, Rect(100, 100, 120, 120), contents="Hi")
    view.session.execute(AddAnnotationCommand(note))
    assert comments.count == 1 and comments.accessibleName() == "Comments, 1 item"
    window.act_undo.trigger()
    assert comments.count == 0 and comments.accessibleName() == "Comments"

    search = side.button(window.search_panel)
    window.show_find()
    window.search_panel.query_edit.setText("needle")
    with qtbot.waitSignal(window.search_panel.search_finished, timeout=10000):
        window.search_panel.start_search()
    assert search.count == 5 and search.badge_text() == "5" and "5" in search.toolTip()
    assert search.accessibleName() == "Search, 5 items"

    access = side.button(window.accessibility_panel)
    assert access.count == 0  # nothing to report before the check has run
    window.accessibility_check()
    problems = [f for f in window.accessibility_panel.findings if f.status is not Status.PASSED]
    assert problems and access.count == len(problems)

    window.open_path(fixture_pdf("outline"))  # badges follow the current document
    assert search.count == 0 and access.count == 0
    search.set_count(250)
    assert search.badge_text() == "99+"


def test_width_and_open_panel_are_remembered(qtbot) -> None:
    w = make_window(qtbot)
    w.nav_panels.open(bookmarks(w))
    settle(qtbot)
    w.resizeDocks([w.nav_dock], [w.nav_panels.rail.width() + 333], Qt.Orientation.Horizontal)
    settle(qtbot)
    assert abs(w.nav_panels.content.width() - 333) <= 4
    w.show_inspector()
    w.close()

    w2 = make_window(qtbot)
    assert w2.nav_panels.current() is bookmarks(w2)
    assert abs(w2.nav_panels.content.width() - 333) <= 4
    assert w2.inspector_panels.current() is w2.inspector and w2.act_inspector.isChecked()
    w2.nav_panels.collapse()
    w2.close()

    w3 = make_window(qtbot)
    assert w3.nav_panels.current() is None
    assert w3.nav_dock.width() == w3.nav_panels.rail.width()
    w3.nav_panels.open(w3.comments_panel)  # the width survives being collapsed
    settle(qtbot)
    assert abs(w3.nav_panels.content.width() - 333) <= 4
    w3.close()


def test_empty_panels_explain_and_offer_their_action(
    qtbot, window: MainWindow, fixture_pdf
) -> None:
    side = window.nav_panels
    # no document: every panel says so and offers Open with its real shortcut
    assert side.no_document.isVisible() and not side.stack.isVisible()
    assert side.no_document.button.text() == "Open (Ctrl+O)"

    window.open_path(fixture_pdf("text_multipage"))
    assert side.stack.isVisible() and not side.no_document.isVisible()
    marks = bookmarks(window)
    window.show_panel(marks)
    assert marks.empty.isVisible() and not marks.tree.isVisible()
    assert marks.empty.title.text() == "No bookmarks yet" and marks.empty.text.text()
    assert marks.empty.button.text() == "Add bookmark"  # no shortcut exists for it
    qtbot.mouseClick(marks.empty.button, Qt.MouseButton.LeftButton)
    assert marks.tree.topLevelItemCount() == 1 and not marks.empty.isVisible()

    comments = window.comments_panel
    window.show_comments()
    assert comments.empty.isVisible() and not comments.tree.isVisible()
    # in a narrow panel the shortcut moves from the label to the tooltip
    assert offered(comments.empty) == "Add sticky note (Ctrl+6)"
    qtbot.mouseClick(comments.empty.button, Qt.MouseButton.LeftButton)
    assert window.current_tool == "note"
    # a changed shortcut shows up in the label
    window.shortcuts.set("sticky-note", ("Ctrl+Alt+6",))
    assert offered(comments.empty) == "Add sticky note (Ctrl+Alt+6)"
    window.shortcuts.reset("sticky-note")

    window.show_panel(window.protect.panel)
    assert window.protect.panel.empty.button.text() == "Mark for redaction"
    window.show_panel(window.accessibility_panel)
    access = window.accessibility_panel
    assert access.empty.isVisible() and access.empty.button.text() == "Run check"
    qtbot.mouseClick(access.empty.button, Qt.MouseButton.LeftButton)
    assert access.findings and not access.empty.isVisible() and access.tree.isVisible()
    window.show_panel(window.tags_panel)
    assert window.tags_panel.empty.isVisible() and not window.tags_panel.tree.isVisible()

    window.show_find()
    search = window.search_panel
    assert search.empty.isVisible() and search.empty.title.text() == "Search this document"
    search.query_edit.setText("no such words anywhere")
    with qtbot.waitSignal(search.search_finished, timeout=10000):
        search.start_search()
    assert search.empty.isVisible() and search.empty.title.text() == "No matches"


def test_properties_rail_and_ctrl_e(qtbot, window: MainWindow, fixture_pdf) -> None:
    right = window.inspector_panels
    view = window.open_path(fixture_pdf("text_multipage"))
    # the right rail is always there; Properties starts collapsed to it
    assert window.inspector_dock.isVisible() and right.rail.isVisible()
    assert right.current() is None and not window.inspector.isVisible()
    assert window.inspector_dock.width() == right.rail.width()

    view.setFocus()
    qtbot.keyClick(window, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
    settle(qtbot)
    assert right.current() is window.inspector and window.inspector.isVisible()
    assert window.act_inspector.isChecked()
    assert window.inspector.empty.isVisible()  # nothing selected yet
    assert window.inspector_dock.width() > right.rail.width() + 100

    qtbot.keyClick(window, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
    assert right.current() is None and not window.act_inspector.isChecked()

    click(qtbot, right.button(window.inspector))  # the rail icon and Ctrl+E stay in step
    assert window.act_inspector.isChecked()
    click(qtbot, right.button(window.inspector))
    assert not window.act_inspector.isChecked()
    window.show_inspector()
    assert right.current() is window.inspector and window.act_inspector.isChecked()
