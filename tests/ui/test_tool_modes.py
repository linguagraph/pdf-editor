"""Phase T: leaving tools, discoverability, and tools that respect existing content."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt

from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.ui.dialogs.preferences import PreferencesDialog
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.tools import annotate
from pdfeditor.ui.view.document_view import annotation_tip_text
from pdfeditor.ui.view.note_popup import NotePopup
from pdfeditor.ui.view.text_editor import InlineTextEditor

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.prefs.keep_tools = False  # the default: one use, then back to Select
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


def vp(view, x: float, y: float) -> QPoint:
    return view.mapFromScene(view.page_point_to_scene(0, Point(x, y)))


def drag(qtbot, view, a, b) -> None:
    start, end = vp(view, *a), vp(view, *b)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view.viewport(), pos=(start + end) / 2)
    qtbot.mouseMove(view.viewport(), pos=end)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


def comments(view) -> list[AnnotationModel]:
    return [
        a
        for a in view.page_annotations(0)
        if a.type not in (AnnotationType.POPUP, AnnotationType.REDACT)
    ]


def add_note(view, text: str = "Existing note") -> AnnotationModel:
    from pdfeditor.core.commands import AddAnnotationCommand

    note = AnnotationModel(
        AnnotationType.TEXT, 0, Rect(450, 400, 470, 420), contents=text, author="Ann"
    )
    view.session.execute(AddAnnotationCommand(note))
    return next(a for a in comments(view) if a.contents == text)


def test_clicking_the_active_tool_turns_it_off(window: MainWindow, view) -> None:
    window.tool_actions["note"].trigger()
    assert window.current_tool == "note"
    window.tool_actions["note"].trigger()  # same button again
    assert window.current_tool == "select" and window.tool_actions["select"].isChecked()
    window.protect.act_redact.trigger()
    window.protect.act_redact.trigger()
    assert window.current_tool == "select"
    window.tool_actions["edit"].trigger()
    window.tool_actions["edit"].trigger()
    assert window.current_tool == "select" and not view.show_object_outlines


def test_status_indicator_and_quick_toolbar(qtbot, window: MainWindow, view) -> None:
    quick = window.ribbon.quick.actions()
    assert window.tool_actions["select"] in quick and window.tool_actions["hand"] in quick
    assert window.act_undo in quick and window.act_save in quick
    assert not window.tool_label.isVisible()
    window.set_tool("pen")
    assert window.tool_label.isVisible() and "Pen" in window.tool_label.text()
    assert "Esc" in window.tool_label.text()
    qtbot.mouseClick(window.tool_exit, Qt.MouseButton.LeftButton)
    assert window.current_tool == "select" and not window.tool_label.isVisible()
    view.back_to_select.emit()  # the right-click menu's action
    assert window.current_tool == "select"


def test_tools_are_one_shot_by_default(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, (80, 500), (180, 560))
    assert len(comments(view)) == 1 and window.current_tool == "select"
    window.prefs.keep_tools = True
    window.set_tool("rectangle")
    drag(qtbot, view, (300, 600), (400, 660))
    assert len(comments(view)) == 2 and window.current_tool == "rectangle"


def test_note_tool_selects_existing_note_instead_of_adding(
    qtbot, window: MainWindow, view, monkeypatch
) -> None:
    existing = add_note(view)
    asked: list[str] = []
    monkeypatch.setattr(
        annotate, "ask_text", lambda v, title, initial="": asked.append(title) or "new note"
    )
    window.set_tool("note")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 460, 410))
    assert len(comments(view)) == 1 and not asked  # no second note was created
    assert view.selected_annotations == [(0, existing.name)]
    assert window.current_tool == "note"  # still the note tool: it didn't make anything
    # double-click opens the existing note for editing
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 460, 410))
    assert asked == ["Edit Comment"]
    assert comments(view)[0].contents == "new note"


def test_clicking_a_note_with_select_opens_popup(qtbot, window: MainWindow, view) -> None:
    add_note(view, "Please check the totals")
    window.set_tool("select")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 460, 410))
    popup = window._note_popup
    assert isinstance(popup, NotePopup) and popup.isVisible()
    assert popup.text.toPlainText() == "Please check the totals"
    popup.reply_edit.setText("Done")
    popup.reply_edit.returnPressed.emit()
    assert "Done" in popup.replies.text()
    popup.text.setPlainText("Please check the totals again")
    popup.close()
    note = next(a for a in comments(view) if a.in_reply_to is None)
    assert note.contents == "Please check the totals again"


def test_hover_tip_text_includes_replies(view) -> None:
    note = add_note(view, "Question?")
    reply = AnnotationModel(
        AnnotationType.TEXT, 0, note.rect, contents="Answer", author="Bo", in_reply_to=note.id
    )
    text = annotation_tip_text(note, [note, reply])
    assert "<b>Ann</b>: Question?" in text and "<b>Bo</b>: Answer" in text
    assert (
        annotation_tip_text(AnnotationModel(AnnotationType.SQUARE, 0, Rect(0, 0, 1, 1)), []) == ""
    )


def test_add_text_tool_edits_existing_paragraph(qtbot, window: MainWindow, view) -> None:
    window.set_tool("add_text")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 150, 215))
    editor = view.viewport().findChild(InlineTextEditor)
    assert editor is not None and editor.toPlainText() == "A second paragraph that stays put."


def test_keep_tools_preference(window: MainWindow) -> None:
    dialog = PreferencesDialog(window.prefs, window)
    dialog.keep_tools.setChecked(True)
    dialog.accept()
    assert window.prefs.keep_tools
    dialog = PreferencesDialog(window.prefs, window)
    assert dialog.keep_tools.isChecked()
