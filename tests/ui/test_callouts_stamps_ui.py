"""Callout tool and custom image stamps in the window."""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image
from PySide6.QtCore import Qt

from pdfeditor.model.annotations import AnnotationType
from pdfeditor.model.geometry import Point
from pdfeditor.services.custom_stamps import StampLibrary
from pdfeditor.ui import stamp_menu
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.tools import annotate

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.prefs.keep_tools = True
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
    shutil.copy2(fixture_pdf("text_multipage"), path)
    v = window.open_path(path)
    v.set_zoom(1.0)
    v.go_to_page(0, record=False)
    return v


def vp(view, page: int, x: float, y: float):
    return view.mapFromScene(view.page_point_to_scene(page, Point(x, y)))


def drag(qtbot, view, a: tuple[float, float], b: tuple[float, float], steps: int = 5) -> None:
    start, end = vp(view, 0, *a), vp(view, 0, *b)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    for i in range(1, steps + 1):
        qtbot.mouseMove(view.viewport(), pos=start + (end - start) * i / steps)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


def click(qtbot, view, x: float, y: float) -> None:
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 0, x, y))


def comments(view) -> list:
    return [a for a in view.page_annotations(0) if a.type is not AnnotationType.POPUP]


def test_callout_click_point_then_drag_box(qtbot, window: MainWindow, view, monkeypatch) -> None:
    monkeypatch.setattr(annotate, "ask_text", lambda v, title, initial="": "Check this")
    window.set_tool("callout")
    click(qtbot, view, 100, 150)  # the point being called out
    assert view.tool.busy
    drag(qtbot, view, (250, 300), (420, 350))  # the box
    (callout,) = comments(view)
    assert callout.type is AnnotationType.FREE_TEXT and callout.is_callout
    assert callout.contents == "Check this"
    assert callout.vertices[0].x == pytest.approx(100, abs=2)
    assert callout.vertices[0].y == pytest.approx(150, abs=2)
    assert callout.rect.x0 == pytest.approx(250, abs=2)
    assert callout.rect.x1 == pytest.approx(420, abs=2)
    assert callout.line_endings[0] == "OpenArrow"
    assert not view.tool.busy
    assert window.act_undo.text() == "&Undo Add Callout"
    window.act_undo.trigger()
    assert comments(view) == []
    window.act_redo.trigger()
    assert comments(view)[0].is_callout

    # a single drag from the point places a default-size box where it ends
    drag(qtbot, view, (100, 500), (300, 560))
    second = next(a for a in comments(view) if a.name != callout.name)
    assert second.is_callout and second.vertices[0].y == pytest.approx(500, abs=2)
    assert second.rect.x0 == pytest.approx(300, abs=2)

    window.save()
    path = view.session.path
    with pikepdf.open(path) as pdf:
        callouts = [a for a in pdf.pages[0].Annots if a.get("/IT") == "/FreeTextCallout"]
        assert len(callouts) == 2
    doc = pdfium.PdfDocument(path)
    doc[0].render(scale=1)  # renders with the appearance streams
    doc.close()


def test_cancelled_callout_creates_nothing(qtbot, window: MainWindow, view, monkeypatch) -> None:
    monkeypatch.setattr(annotate, "ask_text", lambda v, title, initial="": None)
    window.set_tool("callout")
    drag(qtbot, view, (100, 150), (300, 200))
    assert comments(view) == []
    assert not view.tool.busy


def test_resizing_a_callout_keeps_its_point(qtbot, window: MainWindow, view, monkeypatch) -> None:
    monkeypatch.setattr(annotate, "ask_text", lambda v, title, initial="": "Resize me")
    window.set_tool("callout")
    click(qtbot, view, 100, 150)
    drag(qtbot, view, (250, 300), (420, 350))
    window.set_tool("select")
    (callout,) = comments(view)
    view.set_annotation_selection([(0, callout.name)])
    box = callout.rect
    drag(qtbot, view, (box.x1, box.y1), (box.x1 + 60, box.y1 + 30))  # bottom-right handle
    (resized,) = comments(view)
    assert resized.rect.x1 == pytest.approx(box.x1 + 60, abs=3)
    assert resized.vertices[0].x == pytest.approx(100, abs=2)  # tip unchanged
    assert resized.vertices[-1].x == pytest.approx(resized.rect.x0, abs=2)  # line meets box
    # moving takes the whole callout along
    drag(qtbot, view, (300, resized.rect.center.y), (340, resized.rect.center.y + 20))
    (moved,) = comments(view)
    assert moved.vertices[0].x == pytest.approx(140, abs=3)


def _png(path: Path) -> Path:
    img = Image.new("RGBA", (120, 60), (0, 0, 0, 0))
    img.paste((200, 0, 0, 255), (0, 0, 120, 30))
    out = io.BytesIO()
    img.save(out, "PNG")
    path.write_bytes(out.getvalue())
    return path


def test_custom_image_stamp(qtbot, window: MainWindow, view, monkeypatch, tmp_path: Path) -> None:
    source = _png(tmp_path / "My Logo.png")
    monkeypatch.setattr(stamp_menu, "ask_stamp_image", lambda parent: source)
    menu = window.stamp_menu
    key = menu.add_custom()
    assert key is not None
    assert window.prefs.custom_stamps == [key]
    stored = StampLibrary().get(key)
    assert stored is not None and stored.path.read_bytes() == source.read_bytes()
    source.unlink()  # the library keeps its own copy
    assert window.current_tool == "stamp" and window.stamp_name == "image:" + key
    menu.rebuild_items()
    labels = [a.text() for a in menu.actions()]
    assert "My Logo" in labels and "Approved" in labels

    click(qtbot, view, 300, 400)
    (stamp,) = comments(view)
    assert stamp.type is AnnotationType.STAMP and stamp.image is not None
    # 120x60 px at 96 DPI = 90x45 pt, centered on the click
    assert stamp.rect.width == pytest.approx(90, abs=2)
    assert stamp.rect.center.x == pytest.approx(300, abs=2)
    window.act_undo.trigger()
    assert comments(view) == []
    window.act_redo.trigger()
    assert comments(view)[0].image is not None

    window.save()
    path = view.session.path
    with pikepdf.open(path) as pdf:
        annot = next(a for a in pdf.pages[0].Annots if a.Subtype == "/Stamp")
        assert any(
            annot.AP.N.Resources.XObject[k].Subtype == "/Image"
            for k in annot.AP.N.Resources.XObject
        )
    doc = pdfium.PdfDocument(path)
    pil = doc[0].render(scale=1).to_pil().convert("RGB")
    doc.close()
    r, g, b = pil.getpixel((300, 400 - 11))  # the red upper half of the image
    assert r > 150 and g < 100 and b < 100

    # removing it from the library leaves placed stamps alone
    menu.remove_custom(key)
    menu.rebuild_items()
    assert window.prefs.custom_stamps == []
    assert "My Logo" not in [a.text() for a in menu.actions()]
    assert comments(view)[0].image is not None


def test_bad_custom_stamp_is_refused(
    qtbot, window: MainWindow, monkeypatch, tmp_path: Path
) -> None:
    from PySide6.QtWidgets import QMessageBox

    bogus = tmp_path / "fake.png"
    bogus.write_bytes(b"not an image")
    monkeypatch.setattr(stamp_menu, "ask_stamp_image", lambda parent: bogus)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda parent, title, text: warnings.append(text))
    )
    assert window.stamp_menu.add_custom() is None
    assert warnings and "PNG or JPEG" in warnings[0]
    assert window.prefs.custom_stamps == []
