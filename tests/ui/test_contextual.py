"""Contextual actions (Phase U7): mini toolbars over selected text, comments and organizer
pages, and the "Search tools" box in the ribbon."""

from __future__ import annotations

import copy
import shutil
from collections.abc import Iterator
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QApplication, QWidget

from pdfeditor.core.commands import UpdateAnnotationCommand
from pdfeditor.core.session import DocumentSession
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.services.text import TextPos, TextSelection
from pdfeditor.ui.contextual import MiniToolbar, place
from pdfeditor.ui.dialogs.preferences import PreferencesDialog
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.tools import annotate
from pdfeditor.ui.view.document_view import DocumentView

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    w = MainWindow()
    w.resize(1280, 860)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


def settle(qtbot) -> None:
    for _ in range(5):
        QApplication.processEvents()
    qtbot.wait(10)


def select_words(view: DocumentView, page: int = 0, start: int = 0, end: int = 20) -> None:
    view.set_selection(TextSelection(TextPos(page, start), TextPos(page, end)))


def annots(view: DocumentView, page: int = 0) -> list[AnnotationModel]:
    return view.page_annotations(page)


def by_type(view: DocumentView, kind: AnnotationType) -> AnnotationModel:
    return next(a for a in annots(view) if a.type is kind)


def click(button: QWidget) -> None:
    assert button.isVisible(), button.accessibleName()
    button.click()  # type: ignore[attr-defined]


def inside(outer: QRect, inner: QRect) -> bool:
    return outer.contains(inner)


# -- placement ------------------------------------------------------------------------------
def test_place_above_below_and_inside() -> None:
    area = QRect(0, 0, 800, 600)
    size = QSize(200, 40)
    # room above: centred above the target, not over it
    target = QRect(300, 200, 100, 20)
    p = place(size, target, area)
    bar = QRect(p, size)
    assert bar.bottom() < target.top() and abs(bar.center().x() - target.center().x()) <= 1
    # no room above: below
    target = QRect(300, 10, 100, 20)
    bar = QRect(place(size, target, area), size)
    assert bar.top() > target.bottom() and inside(area, bar)
    # near the right edge: pushed inside the area
    target = QRect(760, 300, 40, 20)
    bar = QRect(place(size, target, area), size)
    assert inside(area, bar) and not bar.intersects(target)
    # the target fills the area: inside anyway
    bar = QRect(place(size, area, area), size)
    assert inside(area, bar)


# -- text selection -------------------------------------------------------------------------
def test_text_selection_shows_toolbar_above_or_below(qtbot, window: MainWindow, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    assert view is not None
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    view.setFocus()
    bar = window.contextual.text_bar
    assert bar.isHidden()
    scroll = view.verticalScrollBar()

    def select_at(y: int) -> QRect:
        """Select some words and scroll them to ``y`` px below the top of the page area."""
        select_words(view, 0, 400, 420)
        sel = view.selection_viewport_rect()
        assert sel is not None
        scroll.setValue(scroll.value() + sel.top() - y)
        assert bar.isHidden()  # scrolling hides it
        view.clear_selection()
        select_words(view, 0, 400, 420)
        settle(qtbot)
        sel = view.selection_viewport_rect()
        assert sel is not None and abs(sel.top() - y) <= 8
        return sel

    sel = select_at(300)
    assert bar.isVisible() and bar.parentWidget() is view.viewport()
    assert inside(view.viewport().rect(), bar.geometry())
    assert not bar.geometry().intersects(sel)
    assert bar.geometry().bottom() < sel.top()  # above
    assert abs(bar.geometry().center().x() - sel.center().x()) <= 2
    assert QApplication.focusWidget() is view  # showing it didn't take the keyboard
    names = [b.accessibleName() for b in bar.visible_buttons()]
    assert names[:6] == [
        "Copy",
        "Highlight",
        "Underline",
        "Strikethrough",
        "Add note",
        "Mark for redaction",
    ]
    assert ("Edit text" in names) == view.session.engine.capabilities.content_edit

    # at the top edge there's no room above, so the bar goes below
    sel = select_at(4)
    assert bar.isVisible() and bar.geometry().top() > sel.bottom()
    assert inside(view.viewport().rect(), bar.geometry())


def test_text_toolbar_hides_on_escape_scroll_zoom_and_clear(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    bar = window.contextual.text_bar
    view.setFocus()

    select_words(view)
    assert bar.isVisible()
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert bar.isHidden() and not view.has_selection()

    select_words(view)
    assert bar.isVisible()
    view.verticalScrollBar().setValue(view.verticalScrollBar().value() + 40)
    assert bar.isHidden()
    window.contextual.refresh()  # stays dismissed until the next selection
    assert bar.isHidden()

    select_words(view, end=25)
    assert bar.isVisible()
    view.zoom_in(animated=False)
    assert bar.isHidden()
    view.fit_width()
    view.go_to_page(0)

    select_words(view, end=12)
    assert bar.isVisible()
    view.clear_selection()
    assert bar.isHidden()

    # another tool: no text bar
    select_words(view)
    window.set_tool("hand")
    assert bar.isHidden()
    window.set_tool("select")


def test_text_toolbar_waits_for_the_mouse(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    bar = window.contextual.text_bar
    view.pointer_pressed.emit()  # dragging out a selection
    select_words(view)
    assert bar.isHidden()
    view.pointer_released.emit()
    assert bar.isVisible()


def test_text_toolbar_keyboard(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    view.setFocus()
    select_words(view)
    bar = window.contextual.text_bar
    buttons = bar.visible_buttons()
    assert all(b.focusPolicy() == Qt.FocusPolicy.TabFocus for b in buttons)
    view.focusNextChild()  # Tab from the page
    assert QApplication.focusWidget() is buttons[0]
    qtbot.keyClick(buttons[0], Qt.Key.Key_Right)
    assert QApplication.focusWidget() is buttons[1]
    qtbot.keyClick(buttons[1], Qt.Key.Key_Escape)
    assert bar.isHidden() and QApplication.focusWidget() is view
    assert view.has_selection()  # Esc in the bar keeps the selection


@pytest.mark.parametrize(
    ("button", "kind"),
    [
        ("highlight_button", AnnotationType.HIGHLIGHT),
        ("underline_button", AnnotationType.UNDERLINE),
        ("strikeout_button", AnnotationType.STRIKEOUT),
    ],
)
def test_markup_buttons(qtbot, window, fixture_pdf, button: str, kind: AnnotationType):
    view = window.open_path(fixture_pdf("text_multipage"))
    select_words(view)
    text = view.selected_text()
    click(getattr(window.contextual, button))
    created = [a for a in annots(view) if a.type is kind]
    assert len(created) == 1 and created[0].contents == text
    assert not view.has_selection() and window.current_tool == "select"
    # the new comment is selected, so its own bar takes over
    assert window.contextual.annotation_bar.isVisible()
    assert window.contextual.text_bar.isHidden()
    window.undo()
    assert not [a for a in annots(view) if a.type is kind]


def test_add_note_and_redact_buttons(qtbot, window, fixture_pdf, monkeypatch):
    view = window.open_path(fixture_pdf("text_multipage"))
    monkeypatch.setattr(annotate, "ask_text", lambda *_a, **_k: "Check this figure")
    select_words(view)
    click(window.contextual.note_button)
    note = by_type(view, AnnotationType.HIGHLIGHT)
    assert note.contents == "Check this figure"
    assert view.session.undo_stack.undo_label == "Add Note to Text"
    window.undo()
    assert not annots(view)

    monkeypatch.setattr(annotate, "ask_text", lambda *_a, **_k: None)  # cancelled
    select_words(view)
    click(window.contextual.note_button)
    assert not annots(view) and view.has_selection()

    click(window.contextual.redact_button)
    marks = [a for a in annots(view) if a.type is AnnotationType.REDACT]
    assert len(marks) == 1 and not view.has_selection()
    window.undo()
    assert not annots(view)


def test_copy_button(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    select_words(view)
    QApplication.clipboard().clear()
    click(window.contextual.copy_button)
    assert QApplication.clipboard().text() == view.selected_text() != ""


def test_note_round_trip(qtbot, window, fixture_pdf, monkeypatch, tmp_path: Path):
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), path)
    view = window.open_path(path)
    monkeypatch.setattr(annotate, "ask_text", lambda *_a, **_k: "Round trip")
    select_words(view)
    click(window.contextual.note_button)
    assert window.contextual.set_annotation_color(Color(0, 0, 1))
    assert window.contextual.set_annotation_opacity(0.5)
    out = tmp_path / "out.pdf"
    assert window._save_to(view, out)
    session = DocumentSession.open(out)
    try:
        with session.lock:
            saved = session.document.page(0).annotations()
    finally:
        session.close()
    highlight = next(a for a in saved if a.type is AnnotationType.HIGHLIGHT)
    assert highlight.contents == "Round trip" and abs(highlight.opacity - 0.5) < 0.01
    assert highlight.color is not None and highlight.color.rgb()[2] > 0.9
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages[0].Annots) >= 1
    doc = pdfium.PdfDocument(out)
    assert doc[0].render(scale=0.5).to_pil().size[0] > 0
    doc.close()


# -- comments ------------------------------------------------------------------------------
def test_annotation_toolbar_color_opacity_undo(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("annotations"))
    square = by_type(view, AnnotationType.SQUARE)
    view.set_annotation_selection([(0, square.name)])
    bar = window.contextual.annotation_bar
    assert bar.isVisible()
    names = [b.accessibleName() for b in bar.visible_buttons()]
    assert names == ["Color", "Opacity", "Reply", "Delete"]
    target = view.viewport_rect([(0, square.rect)])
    assert target is not None and not bar.geometry().intersects(target)
    assert inside(view.viewport().rect(), bar.geometry())

    blue = next(a for a in window.contextual.color_menu.actions() if a.text() == "Blue")
    blue.trigger()
    changed = by_type(view, AnnotationType.SQUARE)
    assert changed.color is not None and changed.color.rgb()[2] > 0.8
    assert bar.isVisible()  # still selected, still there
    window.contextual.opacity_actions[50].trigger()
    assert abs(by_type(view, AnnotationType.SQUARE).opacity - 0.5) < 0.01
    window.contextual._check_opacity()
    assert window.contextual.opacity_actions[50].isChecked()
    window.undo()
    assert abs(by_type(view, AnnotationType.SQUARE).opacity - 1.0) < 0.01
    window.undo()
    restored = by_type(view, AnnotationType.SQUARE).color
    assert restored is not None and restored.rgb()[0] > 0.9

    # a text box's color is its text color
    box = by_type(view, AnnotationType.FREE_TEXT)
    view.set_annotation_selection([(0, box.name)])
    assert window.contextual.set_annotation_color(Color(0, 0.6, 0.2))
    after = by_type(view, AnnotationType.FREE_TEXT)
    assert after.text_color is not None and after.text_color.rgb()[1] > 0.5


def test_annotation_toolbar_delete_and_reply(qtbot, window, fixture_pdf, monkeypatch):
    view = window.open_path(fixture_pdf("annotations"))
    square = by_type(view, AnnotationType.SQUARE)
    count = len(annots(view))
    view.set_annotation_selection([(0, square.name)])
    monkeypatch.setattr(annotate, "ask_text", lambda *_a, **_k: "Agreed")
    click(window.contextual.reply_button)
    replies = [a for a in annots(view) if a.in_reply_to == square.id]
    assert [r.contents for r in replies] == ["Agreed"]
    window.undo()

    view.set_annotation_selection([(0, square.name)])
    click(window.contextual.delete_button)
    assert len(annots(view)) == count - 1
    assert window.contextual.annotation_bar.isHidden()
    window.undo()
    assert len(annots(view)) == count


def test_locked_annotation_shows_reply_and_info(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("annotations"))
    square = by_type(view, AnnotationType.SQUARE)
    locked = copy.deepcopy(square)
    locked.locked = True
    view.session.execute(UpdateAnnotationCommand(square, locked, "Lock"))
    view.set_annotation_selection([(0, square.name)])
    bar = window.contextual.annotation_bar
    assert bar.isVisible()
    assert [b.accessibleName() for b in bar.visible_buttons()] == [
        "Reply",
        "Locked: show properties",
    ]
    assert not window.contextual.set_annotation_color(Color(0, 0, 1))
    click(window.contextual.info_button)
    assert window.inspector_panels.is_open()


def test_annotation_toolbar_hides_while_dragging(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("annotations"))
    square = by_type(view, AnnotationType.SQUARE)
    view.set_annotation_selection([(0, square.name)])
    bar = window.contextual.annotation_bar
    view.pointer_pressed.emit()
    assert bar.isHidden()
    view.pointer_released.emit()
    assert bar.isVisible()


# -- organizer ------------------------------------------------------------------------------
def test_organizer_toolbar_rotates_and_deletes(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    pages = view.page_count
    window.organize.act_organize.trigger()
    tab = window.current_tab()
    assert tab is not None and tab.organizer is not None
    settle(qtbot)
    tab.organizer.select_pages([0, 1])
    bar = window.contextual.page_bar
    assert bar.isVisible() and bar.parentWidget() is tab.organizer.grid.viewport()
    assert inside(tab.organizer.grid.viewport().rect(), bar.geometry())
    assert [b.accessibleName() for b in bar.visible_buttons()] == [
        "Rotate left",
        "Rotate right",
        "Delete pages",
        "Extract pages",
    ]
    click(bar.buttons[1])  # rotate right
    with view.session.lock:
        assert [view.session.document.page(i).rotation for i in (0, 1, 2)] == [90, 90, 0]
    window.undo()
    tab.organizer.select_pages([1])
    assert bar.isVisible()
    click(bar.buttons[2])  # delete
    assert view.page_count == pages - 1
    window.undo()
    assert view.page_count == pages
    tab.organizer.select_pages([])
    assert bar.isHidden()
    window.organize.act_organize.trigger()  # back to reading
    assert bar.isHidden()


# -- search tools ---------------------------------------------------------------------------
def test_search_box_filters_and_runs(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    search = window.command_search
    settle(qtbot)
    assert search.edit.isVisible() and search.button.isHidden()
    # in the tab row, right of the tabs and left of the collapse chevron
    ribbon = window.ribbon
    assert search.geometry().left() >= ribbon.bar.geometry().right()
    assert search.geometry().right() <= ribbon.collapse_button.geometry().left()
    search.activate()
    qtbot.keyClicks(search.edit, "rotate view clock")
    assert search.popup_open
    assert search.popup.item(0).text().startswith("Rotate View Clockwise")
    before = view.rotation
    qtbot.keyClick(search.edit, Qt.Key.Key_Return)
    assert view.rotation == (before + 90) % 360
    assert not search.popup_open and search.edit.text() == ""

    search.activate()
    qtbot.keyClicks(search.edit, "no such command at all")
    assert search.popup.count() == 1 and not search.run_current()
    qtbot.keyClick(search.edit, Qt.Key.Key_Escape)
    assert not search.popup_open and search.edit.text() == ""

    # Down arrow moves through the matches
    search.activate()
    qtbot.keyClicks(search.edit, "zoom")
    first = search.popup.currentRow()
    qtbot.keyClick(search.edit, Qt.Key.Key_Down)
    assert search.popup.currentRow() == first + 1
    qtbot.keyClick(search.edit, Qt.Key.Key_Escape)


def test_ctrl_shift_p_focuses_the_search_box(qtbot, window):
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    assert window.act_palette.shortcut().toString() == "Ctrl+Shift+P"
    assert window.act_prefs.shortcut().toString() == "Ctrl+K"  # Acrobat's Preferences
    qtbot.keyClick(
        window,
        Qt.Key.Key_P,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    assert window.command_search.edit.hasFocus()


def test_search_box_collapses_when_narrow_or_compact(qtbot, window):
    search = window.command_search
    window.resize(800, 700)
    settle(qtbot)
    assert search.collapsed and search.button.isVisible() and search.edit.isHidden()
    assert search.button.accessibleName() == "Search tools"
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    window.act_palette.trigger()  # Ctrl+Shift+P opens the box for now
    assert search.edit.isVisible() and search.edit.hasFocus()
    qtbot.keyClick(search.edit, Qt.Key.Key_Escape)
    assert search.edit.isHidden() and search.button.isVisible()

    window.resize(1400, 860)
    settle(qtbot)
    assert not search.collapsed and search.edit.isVisible()
    window.act_compact_ribbon.trigger()
    settle(qtbot)
    assert search.collapsed and search.button.isVisible()
    window.act_compact_ribbon.trigger()
    settle(qtbot)
    assert not search.collapsed


# -- preferences and accessibility ---------------------------------------------------------
def test_preference_turns_mini_toolbars_off(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("text_multipage"))
    dialog = PreferencesDialog(window.prefs, window)
    assert dialog.mini_toolbars.isChecked()  # on by default
    dialog.mini_toolbars.setChecked(False)
    dialog.accept()
    assert not AppSettings().show_mini_toolbars
    window._apply_prefs()
    select_words(view)
    assert window.contextual.text_bar.isHidden()
    # the same commands are still in the ribbon
    assert window.tool_actions["highlight"] in window.ribbon.button_actions()
    window.prefs.show_mini_toolbars = True
    window._apply_prefs()
    assert window.contextual.text_bar.isVisible()


def test_mini_toolbar_widgets_are_named(qtbot, window, fixture_pdf):
    view = window.open_path(fixture_pdf("annotations"))
    select_words(view, end=8)
    bars: list[MiniToolbar] = [window.contextual.text_bar, window.contextual.annotation_bar]
    assert bars[0].isVisible()
    square = by_type(view, AnnotationType.SQUARE)
    view.set_annotation_selection([(0, square.name)])
    assert bars[1].isVisible()
    for bar in (*bars, window.contextual.page_bar):
        assert bar.accessibleName()
        for b in bar.buttons:
            assert b.accessibleName() and b.toolTip()
    search = window.command_search
    for w in (search.edit, search.button, search.popup):
        assert w.accessibleName()
