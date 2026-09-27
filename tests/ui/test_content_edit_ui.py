from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMessageBox

from pdfeditor.model.geometry import Point
from pdfeditor.model.objects import ObjectType
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
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("mixed_content"), path)
    v = window.open_path(path)
    v.set_zoom(1.0)
    v.go_to_page(0, record=False)
    return v


def vp(view, x: float, y: float) -> QPoint:
    return view.mapFromScene(view.page_point_to_scene(0, Point(x, y)))


def drag(qtbot, view, a: tuple[float, float], b: tuple[float, float], steps: int = 5) -> None:
    start, end = vp(view, *a), vp(view, *b)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    for i in range(1, steps + 1):
        qtbot.mouseMove(view.viewport(), pos=start + (end - start) * i / steps)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


def objects(view, t: ObjectType) -> list:
    return [o for o in view.page_objects(0) if o.type is t]


def test_edit_tool_moves_and_resizes_image(qtbot, window: MainWindow, view) -> None:
    window.set_tool("edit")
    assert view.show_object_outlines and view.object_outlines(0)
    drag(qtbot, view, (480, 130), (380, 530))  # grab the image, move by (-100, +400)
    (image,) = objects(view, ObjectType.IMAGE)
    assert image.bbox.x0 == pytest.approx(320, abs=2) and image.bbox.y0 == pytest.approx(480, abs=2)
    assert window.act_undo.text() == "&Undo Move Object"
    assert view.selected_objects and view.selected_objects[0][1] == image.key  # reselected
    # bottom-right handle: make it 40 points wider
    drag(qtbot, view, (image.bbox.x1, image.bbox.y1), (image.bbox.x1 + 40, image.bbox.y1))
    (bigger,) = objects(view, ObjectType.IMAGE)
    assert bigger.bbox.width == pytest.approx(160, abs=3)
    window.act_undo.trigger()
    window.act_undo.trigger()
    (back,) = objects(view, ObjectType.IMAGE)
    assert back.bbox.x0 == pytest.approx(420, abs=1)


def test_marquee_select_and_delete_key(qtbot, window: MainWindow, view) -> None:
    window.set_tool("edit")
    drag(qtbot, view, (60, 290), (460, 400))  # around the vector box and the form logo
    kinds = {k.split(":")[0] for _p, k in view.selected_objects}
    assert kinds == {"path", "form"}
    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert not objects(view, ObjectType.PATH) and not objects(view, ObjectType.FORM)
    assert window.act_undo.text() == "&Undo Delete 2 Objects"


def test_double_click_edits_text_inline(qtbot, window: MainWindow, view) -> None:
    window.set_tool("edit")
    heading = objects(view, ObjectType.TEXT)[0]
    pos = vp(view, heading.bbox.center.x, heading.bbox.center.y)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    editor = view.viewport().findChild(InlineTextEditor)
    assert editor is not None and editor.toPlainText() == "Mixed content heading"
    editor.setPlainText("A new heading")
    qtbot.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    texts = [o.text for o in objects(view, ObjectType.TEXT)]
    assert texts[0] == "A new heading"
    assert window.act_undo.text() == "&Undo Edit Text"
    window.act_undo.trigger()
    assert objects(view, ObjectType.TEXT)[0].text == "Mixed content heading"


def test_escape_cancels_inline_edit(qtbot, window: MainWindow, view) -> None:
    window.set_tool("edit")
    heading = objects(view, ObjectType.TEXT)[0]
    qtbot.mouseDClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=vp(view, heading.bbox.center.x, heading.bbox.center.y),
    )
    editor = view.viewport().findChild(InlineTextEditor)
    editor.setPlainText("discard me")
    qtbot.keyClick(editor, Qt.Key.Key_Escape)
    assert objects(view, ObjectType.TEXT)[0].text == "Mixed content heading"
    assert not view.session.undo_stack.can_undo


def test_add_text_image_and_shapes(
    qtbot, window: MainWindow, view, monkeypatch, tmp_path: Path
) -> None:
    window.set_tool("add_text")
    drag(qtbot, view, (100, 600), (400, 640))
    editor = view.viewport().findChild(InlineTextEditor)
    editor.setPlainText("Hello added text")
    editor.commit()
    assert any(o.text == "Hello added text" for o in objects(view, ObjectType.TEXT))
    png = tmp_path / "blue.png"
    Image.new("RGB", (80, 60), (0, 0, 255)).save(png)
    monkeypatch.setattr(edit, "ask_image", lambda v: png)
    window.set_tool("add_image")
    drag(qtbot, view, (100, 700), (180, 760))
    assert len(objects(view, ObjectType.IMAGE)) == 2
    before = len(objects(view, ObjectType.PATH))
    window.set_tool("add_rectangle")
    drag(qtbot, view, (300, 700), (400, 780))
    window.set_tool("add_line")
    drag(qtbot, view, (420, 700), (520, 760))
    assert len(objects(view, ObjectType.PATH)) == before + 2
    window.save()
    with pikepdf.open(view.session.path) as pdf:
        assert len(pdf.pages) == 1


def test_replace_and_export_image(qtbot, window: MainWindow, view, tmp_path: Path) -> None:
    window.set_tool("edit")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 480, 130))
    window._update_ui()
    assert window.edit.act_replace_image.isEnabled()
    out = window.edit.export_image(tmp_path / "out.png")
    assert out is not None and Image.open(out).size == (120, 100)
    red = tmp_path / "red.png"
    Image.new("RGB", (30, 30), (255, 0, 0)).save(red)
    assert window.edit.replace_image(red)
    from pdfeditor.engine.base import ColorMode, RenderRequest
    from pdfeditor.model.geometry import Rect

    with view.session.lock:
        sample = (
            view.session.document.page(0)
            .render(RenderRequest(color=ColorMode.RGB, clip=Rect(480, 130, 481, 131)))
            .samples
        )
    assert sample[0] > 200 and sample[1] < 80


def test_font_substitution_is_reported(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "subset.pdf"
    shutil.copy2(fixture_pdf("subset_fonts"), path)
    v = window.open_path(path)
    messages: list[str] = []
    monkeypatch.setattr(edit, "notify", messages.append)
    (block,) = (o for o in v.page_objects(0) if o.type is ObjectType.TEXT)
    assert edit.run_edit(
        v, "Edit Text", lambda doc: doc.page(0).replace_text(block.key, "Zyxwvu 42")
    )
    assert messages and "instead" in messages[0]


def test_signed_document_warning(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    import pymupdf

    src = pymupdf.open(fixture_pdf("images"))
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    widget.field_name = "Sig1"
    widget.rect = pymupdf.Rect(50, 50, 200, 100)
    src[0].add_widget(widget)
    path = tmp_path / "signed.pdf"
    src.save(path)
    src.close()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm.SigFlags = 3
        pdf.save(path)
    window.open_path(path)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    window.set_tool("edit")
    assert window.current_tool != "edit"
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window.set_tool("edit")
    assert window.current_tool == "edit"
