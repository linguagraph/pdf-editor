from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from pdfeditor.model.annotations import AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point
from pdfeditor.services.text import TextPos, TextSelection
from pdfeditor.ui.dialogs.redaction import (
    ApplyRedactionsDialog,
    RedactionPropertiesDialog,
    SanitizeDialog,
)
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
def view(window: MainWindow, fixture_pdf, tmp_path: Path):
    path = tmp_path / "sensitive.pdf"
    shutil.copy2(fixture_pdf("sensitive"), path)
    v = window.open_path(path)
    v.set_zoom(1.0)
    v.go_to_page(0, record=False)
    return v


def marks(view) -> list:
    return [a for a in view.page_annotations(0) if a.type is AnnotationType.REDACT]


def page_text(view) -> str:
    with view.session.lock:
        return view.session.document.page(0).text_page(with_chars=False).text


def accepted(dialog):
    dialog.accept()
    return dialog


def test_redact_tool_and_selection_marks(qtbot, window: MainWindow, view) -> None:
    window.protect.act_redact.trigger()
    assert window.current_tool == "redact"
    a = view.mapFromScene(view.page_point_to_scene(0, Point(70, 250)))
    b = view.mapFromScene(view.page_point_to_scene(0, Point(300, 270)))
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=a)
    qtbot.mouseMove(view.viewport(), pos=b)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=b)
    assert len(marks(view)) == 1
    # selecting text first and clicking Redact marks exactly the selection
    window.set_tool("select")
    index = view.text_cache.get(0)
    start = index.text.index("123-45-6789")
    view.set_selection(TextSelection(TextPos(0, start), TextPos(0, start + 11)))
    window.protect.act_redact.trigger()
    assert window.current_tool == "select" and len(marks(view)) == 2
    assert "123-45-6789" in page_text(view)  # only marked so far


def test_find_mark_apply_verify_and_undo(qtbot, window: MainWindow, view) -> None:
    dialog = window.protect.mark_dialog(view)
    dialog.preset_boxes["Email addresses"].setChecked(True)
    dialog.preset_boxes["Credit card numbers"].setChecked(True)
    dialog.search_button.click()
    assert len(dialog.hits) == 2
    assert window.protect.find_and_mark(accepted(dialog)) == 2
    panel = window.protect.panel
    assert window.nav_panels.current() is panel and panel.list.count() == 2
    report = window.protect.apply(None, accepted(ApplyRedactionsDialog(2, 0, window)))
    assert report is not None and report.ok and report.images_checked == 1
    text = page_text(view)
    assert "jane.example@example.com" not in text and "4111 1111 1111 1111" not in text
    assert "Public paragraph that must survive." in text
    assert marks(view) == [] and panel.list.count() == 0
    assert view.session.require_full_save
    assert window.act_undo.text() == "&Undo Apply 2 Redactions"
    window.act_undo.trigger()
    assert "jane.example@example.com" in page_text(view) and len(marks(view)) == 2


def test_selective_apply_from_panel(qtbot, window: MainWindow, view) -> None:
    dialog = window.protect.mark_dialog(view)
    dialog.preset_boxes["Email addresses"].setChecked(True)
    dialog.preset_boxes["US Social Security numbers"].setChecked(True)
    dialog.search_button.click()
    window.protect.find_and_mark(accepted(dialog))
    panel = window.protect.panel
    panel.list.item(0).setSelected(True)
    chosen = panel.selected_marks()
    d = ApplyRedactionsDialog(2, 1, window)
    assert d.selected.isChecked()
    window.protect.apply(chosen, accepted(d))
    assert len(marks(view)) == 1
    remaining = panel.marks()
    window.protect.remove_marks(remaining)
    assert marks(view) == []


def test_sanitize_then_save(qtbot, window: MainWindow, view) -> None:
    removed = window.protect.sanitize(accepted(SanitizeDialog(window)))
    assert any("JavaScript" in r for r in removed) and any("attached" in r for r in removed)
    assert "Save the document" in window.protect.last_message
    window.save()
    raw = view.session.path.read_bytes()
    assert b"secret attachment" not in raw and b"app.alert" not in raw
    assert not view.session.require_full_save  # cleared by the save


def test_redaction_properties_persist(qtbot, window: MainWindow, view) -> None:
    d = RedactionPropertiesDialog(window.protect.style, window)
    d.overlay.setText("WITHHELD")
    d.fill.set(Color(0.2, 0.2, 0.2))
    window.protect.edit_properties(accepted(d))
    window.protect.mark_pages()
    (mark,) = marks(view)
    assert mark.overlay_text == "WITHHELD"
    assert window.protect._load_style().overlay_text == "WITHHELD"


def test_sanitize_hidden_layers_and_undo(qtbot, window: MainWindow, fixture_pdf, tmp_path) -> None:
    path = tmp_path / "hidden_layers.pdf"
    shutil.copy2(fixture_pdf("hidden_layers"), path)
    view = window.open_path(path)
    dialog = SanitizeDialog(window)
    assert dialog.boxes["hidden_layers"].isChecked() and dialog.boxes["off_page_text"].isChecked()
    dialog.boxes["off_page_text"].setChecked(False)
    assert not dialog.options().off_page_text and dialog.options().hidden_layers
    removed = window.protect.sanitize(accepted(dialog))
    assert any('"Secret layer"' in r for r in removed)
    with view.session.lock:
        assert [layer.name for layer in view.session.document.layers()] == ["Shown layer"]
    view.session.undo()
    with view.session.lock:
        names = [layer.name for layer in view.session.document.layers()]
    assert names == ["Shown layer", "Secret layer"]
