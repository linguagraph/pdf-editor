"""Inline text editing on a two-column layout with the style bar (Phase E)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt

from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point
from pdfeditor.model.objects import Align, ObjectType, family_of
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.tools import edit
from pdfeditor.ui.view.text_editor import InlineTextEditor

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
def view(window: MainWindow, fixture_pdf, tmp_path: Path):
    path = tmp_path / "columns.pdf"
    shutil.copy2(fixture_pdf("two_columns"), path)
    v = window.open_path(path)
    v.set_zoom(1.5)
    v.go_to_page(0, record=False)
    window.set_tool("edit")
    return v


def vp(view, p: Point) -> QPoint:
    return view.mapFromScene(view.page_point_to_scene(0, p))


def texts(view) -> dict[str, object]:
    return {o.text: o for o in view.page_objects(0) if o.type is ObjectType.TEXT}


def open_editor(qtbot, view, label: str) -> InlineTextEditor:
    obj = texts(view)[label]
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, obj.bbox.center))
    editor = view.viewport().findChild(InlineTextEditor)
    assert editor is not None
    return editor


def test_editor_holds_only_its_column(qtbot, view) -> None:
    editor = open_editor(qtbot, view, "Заключване на вратата")
    assert editor.toPlainText() == "Заключване на вратата"
    assert editor.style_bar.isVisible()
    assert editor.style_bar.font_combo.currentText().endswith("(document font)")
    assert editor.style_bar.size_box.value() == pytest.approx(10)
    editor.cancel()


def test_closing_without_changes_leaves_page_alone(qtbot, window, view) -> None:
    before = {t: o.bbox for t, o in texts(view).items()}
    editor = open_editor(qtbot, view, "Бързо")
    editor.commit()
    assert not view.session.undo_stack.can_undo
    assert {t: o.bbox for t, o in texts(view).items()} == before


def test_edit_keeps_place_font_and_other_column(qtbot, window, view) -> None:
    left, right = texts(view)["Бързо"], texts(view)["Заключване на вратата"]
    editor = open_editor(qtbot, view, "Бързо")
    editor.setPlainText("Бързо пране")
    qtbot.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    now = texts(view)
    assert "Бързо" not in now and "Бързо пране" in now
    edited = now["Бързо пране"]
    assert edited.bbox.x0 == pytest.approx(left.bbox.x0, abs=1.5)
    assert edited.bbox.y0 == pytest.approx(left.bbox.y0, abs=2.5)
    assert edited.style.font == left.style.font or "Nimbus" in edited.style.font
    assert edited.style.color.to_hex() == left.style.color.to_hex()
    assert now["Заключване на вратата"].bbox == right.bbox
    window.act_undo.trigger()
    assert "Бързо" in texts(view)


def test_style_bar_applies_to_block(qtbot, window, view) -> None:
    editor = open_editor(qtbot, view, "Предпране")
    bar = editor.style_bar
    bar.size_box.setFocus()  # moving into the bar must not end the edit
    qtbot.wait(20)
    assert view.viewport().findChild(InlineTextEditor) is editor
    bar.size_box.setValue(14)
    bar.bold.setChecked(True)
    bar.set_color(Color(0.8, 0.1, 0.1))
    bar.align_combo.setCurrentIndex(list(Align).index(Align.LEFT))
    assert editor.font().bold() and editor.font().pixelSize() == round(14 * editor._zoom)
    view.setFocus()  # clicking back on the page commits
    qtbot.waitUntil(lambda: view.viewport().findChild(InlineTextEditor) is None, timeout=2000)
    obj = texts(view)["Предпране"]
    assert obj.style.bold and obj.style.size == pytest.approx(14)
    assert obj.style.color.r > 0.7 and obj.style.color.g < 0.3
    assert window.act_undo.text() == "&Undo Edit Text"


def test_add_text_remembers_style(qtbot, window, view) -> None:
    window.set_tool("add_text")
    a, b = vp(view, Point(60, 400)), vp(view, Point(300, 440))
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=a)
    qtbot.mouseMove(view.viewport(), pos=b)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=b)
    editor = view.viewport().findChild(InlineTextEditor)
    editor.setPlainText("Нов текст")
    editor.style_bar.font_combo.setCurrentIndex(editor.style_bar.font_combo.findData("Courier"))
    editor.style_bar.size_box.setValue(16)
    editor.commit()
    obj = texts(view)["Нов текст"]
    assert obj.style.size == pytest.approx(16) and family_of(obj.style.font) == "mono"
    assert edit.AddTextTool.last_style.size == 16  # the next box starts like this one
    edit.AddTextTool.last_style = edit.DEFAULT_TEXT_STYLE
