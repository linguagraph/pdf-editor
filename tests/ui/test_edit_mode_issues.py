"""Issues #42 (edit mode indicator), #43 (wheel over the inline editor) and #44 (tools are
per document)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from pdfeditor.model.objects import ObjectType
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.theme import Theme, apply_theme, current_colors
from pdfeditor.ui.view.document_view import DocumentView
from pdfeditor.ui.view.text_editor import InlineTextEditor

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    w = MainWindow()
    w.resize(1200, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()
    apply_theme(QApplication.instance(), Theme.SYSTEM)  # type: ignore[arg-type]


def open_copy(window: MainWindow, fixture_pdf, tmp_path: Path, name: str) -> DocumentView:
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    view = window.open_path(path)
    assert view is not None
    return view


def in_view(widget: QWidget, view: DocumentView) -> QRect:
    return QRect(widget.mapTo(view, QPoint(0, 0)), widget.size())


def edit_first_text(qtbot, view: DocumentView) -> InlineTextEditor:
    heading = next(o for o in view.page_objects(0) if o.type is ObjectType.TEXT)
    center = view.mapFromScene(view.page_point_to_scene(0, heading.bbox.center))
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
    editor = view.viewport().findChild(InlineTextEditor)
    assert editor is not None
    return editor


def wheel(widget: QWidget, dy: int, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    pos = QPointF(widget.rect().center())
    event = QWheelEvent(
        pos,
        QPointF(widget.mapToGlobal(pos)),
        QPoint(),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


# -- #42: mode indicator ----------------------------------------------------------------------
def test_banner_shows_while_editing_and_goes_on_escape_and_done(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path
) -> None:
    view = open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    banner = window.mode_banner
    assert not banner.isVisible() and view.viewportMargins().top() == 0
    window.tool_actions["edit"].trigger()
    assert banner.isVisible() and banner.parentWidget() is view
    assert banner.text_label.text() == "Editing text & images"
    assert "Esc" in banner.hint_label.text()
    assert banner.accessibleName() == "Mode: Editing text & images"
    assert banner.done_button.accessibleName()
    assert window.tool_label.isVisible()  # the status bar indicator stays
    # A row above the pages, not over them: the page area starts below it.
    port = view.viewport().geometry()
    assert view.viewportMargins().top() == banner.height() > 0
    assert banner.geometry().bottom() < port.top()
    # The frame around the page area, and no overlap with the pill at the bottom.
    assert all(edge.isVisible() for edge in banner.edges)
    pill = window.pill
    assert pill.isVisible() and not in_view(pill, view).intersects(banner.geometry())

    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert window.current_tool == "select" and not banner.isVisible()
    assert not any(edge.isVisible() for edge in banner.edges)
    assert view.viewportMargins().top() == 0 and not window.tool_label.isVisible()

    window.tool_actions["edit"].trigger()
    assert banner.isVisible()
    qtbot.mouseClick(banner.done_button, Qt.MouseButton.LeftButton)
    assert window.current_tool == "select" and not banner.isVisible()
    assert not window.tool_actions["edit"].isChecked()


def test_done_button_works_from_the_keyboard(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path
) -> None:
    open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    window.tool_actions["edit"].trigger()
    done = window.mode_banner.done_button
    assert done.focusPolicy() & Qt.FocusPolicy.TabFocus
    done.setFocus()
    qtbot.keyClick(done, Qt.Key.Key_Space)
    assert window.current_tool == "select"


def test_other_modes_have_their_own_banner(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    banner = window.mode_banner
    window.set_tool("redact")
    assert banner.isVisible() and banner.text_label.text() == "Marking for redaction"
    assert banner.fill_color() == QColor(current_colors().danger)
    assert all(edge.isVisible() for edge in banner.edges)
    window.set_tool("note")  # creation tools: the chip, no frame
    assert banner.isVisible() and banner.text_label.text() == "Sticky Note tool"
    assert not any(edge.isVisible() for edge in banner.edges)
    window.set_tool("hand")
    assert not banner.isVisible()


def test_banner_follows_the_theme(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    window.tool_actions["edit"].trigger()
    banner = window.mode_banner
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    fills = {}
    for theme in (Theme.LIGHT, Theme.DARK):
        apply_theme(app, theme)
        colors = current_colors()
        fills[theme] = banner.fill_color()
        assert fills[theme] == QColor(colors.accent)
        text = banner.text_label.palette().color(banner.text_label.foregroundRole())
        assert text == QColor(colors.on_accent)
        chip = banner.chip.grab().toImage()
        assert chip.pixelColor(chip.width() // 2, 2) == QColor(colors.accent)
    assert fills[Theme.LIGHT] != fills[Theme.DARK]


# -- #43: wheel over the inline editor ----------------------------------------------------------
def test_wheel_over_an_overflowing_editor_scrolls_its_text(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path
) -> None:
    view = open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    view.set_zoom(1.0)
    window.set_tool("edit")
    editor = edit_first_text(qtbot, view)
    editor.setPlainText("\n".join(f"line {i}" for i in range(40)))
    text_bar, page_bar = editor.verticalScrollBar(), view.verticalScrollBar()
    assert text_bar.maximum() > 0 and page_bar.maximum() > 0
    page = page_bar.value()
    wheel(editor.viewport(), -120)
    assert text_bar.value() > 0 and page_bar.value() == page
    # At the last line the box keeps the wheel: the page doesn't run away under the pointer.
    text_bar.setValue(text_bar.maximum())
    wheel(editor.viewport(), -120)
    assert page_bar.value() == page
    wheel(editor.viewport(), 120)
    assert text_bar.value() < text_bar.maximum() and page_bar.value() == page

    # A box with nothing to scroll lets the page scroll.
    editor.setPlainText("short")
    assert text_bar.maximum() == 0
    wheel(editor.viewport(), -120)
    assert page_bar.value() > page


def test_ctrl_wheel_over_the_editor_zooms_the_page(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path
) -> None:
    view = open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    view.set_zoom(1.0)
    window.set_tool("edit")
    editor = edit_first_text(qtbot, view)
    font, width = editor.font().pixelSize(), editor.width()
    wheel(editor.viewport(), 120, Qt.KeyboardModifier.ControlModifier)
    assert view.zoom == pytest.approx(1.1)
    # the box follows the page: larger text, larger box (not QPlainTextEdit's own font zoom)
    qtbot.waitUntil(lambda: editor.width() > width)
    assert editor.font().pixelSize() > font
    assert editor.isVisible() and editor.toPlainText()  # still editing


# -- #44: tools per document -------------------------------------------------------------------
def test_tool_state_is_per_document(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    a = open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    b = open_copy(window, fixture_pdf, tmp_path, "mixed_content")
    banner, edit, select = (
        window.mode_banner,
        window.tool_actions["edit"],
        window.tool_actions["select"],
    )
    window.tabs.setCurrentIndex(0)
    edit.trigger()
    assert a.tool.name == "edit" and a.show_object_outlines
    assert b.tool.name == "select" and not b.show_object_outlines

    window.tabs.setCurrentIndex(1)  # B: still selecting
    assert window.current_tool == "select"
    assert select.isChecked() and not edit.isChecked()
    assert not banner.isVisible() and not window.tool_label.isVisible()
    assert b.viewportMargins().top() == 0

    window.tabs.setCurrentIndex(0)  # A: still editing, outlines and all
    assert window.current_tool == "edit" and edit.isChecked()
    assert banner.isVisible() and banner.parentWidget() is a and window.tool_label.isVisible()
    assert a.show_object_outlines

    # B can have a tool of its own without touching A's.
    window.tabs.setCurrentIndex(1)
    window.tool_actions["hand"].trigger()
    window.tabs.setCurrentIndex(0)
    assert window.current_tool == "edit"
    window.tabs.setCurrentIndex(1)
    assert window.tool_actions["hand"].isChecked() and not banner.isVisible()

    # Close A while it's editing, with typed text in an inline editor.
    window.tabs.setCurrentIndex(0)
    editor = edit_first_text(qtbot, a)
    editor.setPlainText("Typed before closing")
    asked = []

    def answer(session):
        asked.append(session)
        return QMessageBox.StandardButton.Discard

    monkeypatch.setattr(window, "ask_save_changes", answer)
    assert window.close_tab(0)
    assert asked  # the typed text counted as a change
    assert window.current_view() is b and window.current_tool == "hand"
    assert window.tool_actions["hand"].isChecked() and not edit.isChecked()
    assert not banner.isVisible() and banner.parentWidget() is b
    assert not window.tool_label.isVisible()

    window.close_tab(0)  # the last one
    assert window.current_tool == window.prefs.default_tool
    assert not banner.isVisible() and banner.parentWidget() is window
    assert not window.tool_label.isVisible()


def test_new_documents_start_with_the_default_tool(
    window: MainWindow, fixture_pdf, tmp_path: Path
) -> None:
    a = open_copy(window, fixture_pdf, tmp_path, "text_multipage")
    window.set_tool("edit")
    window.prefs.default_tool = "hand"
    b = open_copy(window, fixture_pdf, tmp_path, "mixed_content")
    assert a.tool.name == "edit" and b.tool.name == "hand"
    assert window.tool_actions["hand"].isChecked() and not window.mode_banner.isVisible()
