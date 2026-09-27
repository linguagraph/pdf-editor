from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QGraphicsView

from pdfeditor.model.geometry import Rect
from pdfeditor.services.text import LINE_SEP, TextPos, TextSelection
from pdfeditor.ui.dialogs.print_dialog import PrintDialog
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.printing import PrintOptions, Scaling, pdf_printer, print_pages
from pdfeditor.ui.tools.hand import HandTool
from tests.ui.conftest import wait_rendered

pytestmark = pytest.mark.gui


def _word_pos(view, page: int, word: str, offset: int = 0, frac: float = 0.25) -> QPoint:
    """Viewport position inside the ``offset``-th char of ``word`` (``frac`` across it)."""
    index = view.text_cache.get(page)
    box = index.chars[index.text.index(word) + offset].bbox
    x = box.x0 + frac * box.width
    y = box.center.y
    scene = view.page_rect_to_scene(page, Rect(x, y, x, y))
    return view.mapFromScene(scene.center())


def test_drag_selects_text_and_copies(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.set_zoom(1.0)
    view.go_to_page(0, record=False)
    start = _word_pos(view, 0, "Page 1 heading", 0)
    end = _word_pos(view, 0, "heading", 6, frac=0.9)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view.viewport(), pos=end)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view.selected_text() == "Page 1 heading"
    assert view.copy_selection()
    assert QApplication.clipboard().text() == view.selected_text()
    assert view.overlays(0)  # selection is painted


def test_drag_on_empty_area_pans(qtbot, make_view) -> None:
    view = make_view("text_multipage", height=400)
    view.set_zoom(1.0)
    view.go_to_page(0, record=False)
    blank = view.mapFromScene(view._scene_rects[0].x0 + 300, view._scene_rects[0].y0 + 780)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=blank)
    assert view.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=blank)
    assert view.dragMode() == QGraphicsView.DragMode.NoDrag
    assert not view.has_selection()


def test_double_and_triple_click(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.set_zoom(1.0)
    view.go_to_page(0, record=False)
    pos = _word_pos(view, 0, "heading", 2)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert view.selected_text() == "heading"
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert view.selected_text() == "Page 1 heading"


def test_select_all_spans_document(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.select_all()
    text = view.selected_text()
    assert text.startswith("Page 1 heading") and text.endswith("needle-5")
    assert text.count("Page ") >= 5 and LINE_SEP in text


def test_hand_tool_does_not_select(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.set_zoom(1.0)
    view.set_tool(HandTool())
    assert view.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    pos = _word_pos(view, 0, "heading", 2)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert not view.has_selection()


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    w.resize(1100, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.search_panel.cancel()
    w.close()


def test_search_panel_streams_and_navigates(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    panel = window.search_panel
    window.show_find()
    assert window.nav_tabs.currentWidget() is panel
    panel.query_edit.setText("needle")
    with qtbot.waitSignal(panel.search_finished, timeout=10000) as blocker:
        panel.start_search()
    assert blocker.args == [5]
    assert panel.results.count() == 5 and panel.status.text() == "5 results."
    assert panel.results.currentRow() == 0
    assert window.act_find_next.isEnabled()
    window.act_find_next.trigger()
    assert panel.results.currentRow() == 1
    assert view.current_page == panel.hits[1].page_index
    window.act_find_prev.trigger()
    window.act_find_prev.trigger()
    assert panel.results.currentRow() == 4  # wraps around
    assert any(view.overlays(p) for p in range(5))
    # a new search clears old highlights; invalid regex reports an error
    panel.regex.setChecked(True)
    panel.query_edit.setText("(")
    panel.start_search()
    assert "invalid regular expression" in panel.status.text().lower()
    assert panel.results.count() == 0 and not any(view.overlays(p) for p in range(5))


def test_search_no_matches(qtbot, window: MainWindow, fixture_pdf) -> None:
    window.open_path(fixture_pdf("text_multipage"))
    panel = window.search_panel
    panel.query_edit.setText("zzzz-not-there")
    with qtbot.waitSignal(panel.search_finished, timeout=10000):
        panel.start_search()
    assert panel.status.text() == "No matches."


def test_find_prefills_from_selection(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    index = view.text_cache.get(0)
    i = index.text.index("heading")
    view.set_selection(TextSelection(TextPos(0, i), TextPos(0, i + 7)))
    assert window.act_copy.isEnabled()
    window.show_find()
    assert window.search_panel.query_edit.text() == "heading"


def test_tool_actions_switch_all_views(window: MainWindow, fixture_pdf) -> None:
    v1 = window.open_path(fixture_pdf("text_multipage"))
    v2 = window.open_path(fixture_pdf("images"))
    window.tool_actions["hand"].trigger()
    assert v1.tool.name == "hand" and v2.tool.name == "hand"
    v3 = window.open_path(fixture_pdf("outline"))
    assert v3.tool.name == "hand"
    window.tool_actions["select"].trigger()
    assert all(v.tool.name == "select" for v in (v1, v2, v3))


def _pdf_page_sizes(path: Path) -> list[tuple[float, float]]:
    with pikepdf.open(path) as pdf:
        return [(float(p.mediabox[2]), float(p.mediabox[3])) for p in pdf.pages]


def test_print_to_pdf_page_count_and_content(qtbot, make_view, tmp_path: Path) -> None:
    import pymupdf  # test-only: inspect the printed output

    view = make_view("text_multipage")
    out = tmp_path / "printed.pdf"
    printer = pdf_printer(str(out))
    printed = print_pages(view.session, printer, PrintOptions(pages=(0, 2), scaling=Scaling.FIT))
    assert printed == 2
    assert len(_pdf_page_sizes(out)) == 2
    doc = pymupdf.open(out)
    pix = doc[1].get_pixmap(dpi=40)
    assert len(set(pix.samples[::7])) > 3  # page isn't blank
    doc.close()


def test_print_auto_rotates_landscape_pages(qtbot, make_view, tmp_path: Path) -> None:
    import pymupdf

    view = make_view("rotated_pages")
    out = tmp_path / "rotated.pdf"
    # page 2 is displayed landscape (/Rotate 90) and gets turned to fit portrait paper
    print_pages(view.session, pdf_printer(str(out)), PrintOptions(pages=(1,), auto_rotate=True))
    raster = tmp_path / "rotated-raster.pdf"
    print_pages(
        view.session,
        pdf_printer(str(raster)),
        PrintOptions(pages=(1,), auto_rotate=True, as_image=True),
    )
    vector_page, raster_page = pymupdf.open(out)[0], pymupdf.open(raster)[0]
    assert not vector_page.get_images(full=True) and vector_page.get_drawings()  # vectors
    (image,) = raster_page.get_images(full=True)
    bbox = raster_page.get_image_bbox(image)
    assert bbox.height > bbox.width  # the landscape page turned to fit portrait paper
    # both paths put the page on paper the same way
    a = vector_page.get_pixmap(dpi=40)
    b = raster_page.get_pixmap(dpi=40)
    differing = sum(1 for x, y in zip(a.samples, b.samples, strict=True) if abs(x - y) > 80)
    assert differing < a.width * a.height * 0.01


def test_vector_print_matches_screen(qtbot, make_view, tmp_path: Path) -> None:
    import pymupdf

    from pdfeditor.ui.printing import qt_friendly_svg

    view = make_view("text_multipage")
    out = tmp_path / "vector.pdf"
    printer = pdf_printer(str(out))
    print_pages(view.session, printer, PrintOptions(pages=(0,), scaling=Scaling.ACTUAL))
    printed = pymupdf.open(out)[0]
    assert not printed.get_images(full=True)
    with view.session.lock:
        svg = view.session.document.page(0).to_svg()
    fixed = qt_friendly_svg(svg)
    assert "font_" in svg and fixed != svg  # glyph outlines were enlarged for QtSvg
    assert fixed.count("<use") == svg.count("<use")  # same glyphs, only rescaled
    src = pymupdf.open(view.session.path)[0]
    a, b = src.get_pixmap(dpi=50), printed.get_pixmap(dpi=50)
    assert (a.width, a.height) == (b.width, b.height)
    differing = sum(1 for x, y in zip(a.samples, b.samples, strict=True) if abs(x - y) > 80)
    assert differing < len(a.samples) * 0.01  # glyphs drawn right (QtSvg workaround)


def test_print_dialog_options(
    qtbot, window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch
) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    view.go_to_page(2)
    dialog = PrintDialog(view, window)
    assert dialog.options().pages == (0, 1, 2, 3, 4)
    dialog.current_page.setChecked(True)
    assert dialog.options().pages == (2,)
    dialog.range_pages.setChecked(True)
    dialog.range_edit.setText("2-3")
    dialog.scale_buttons[Scaling.ACTUAL].setChecked(True)
    dialog.annotations.setChecked(False)
    opts = dialog.options()
    assert opts.pages == (1, 2) and opts.scaling is Scaling.ACTUAL and not opts.annotations
    dialog.range_edit.setText("9")
    with pytest.raises(ValueError):
        dialog.options()
    dialog.range_edit.setText("1,5")
    out = tmp_path / "dialog.pdf"
    assert dialog.run(pdf_printer(str(out))) == 2
    assert len(_pdf_page_sizes(out)) == 2
    dialog.close()


def test_rendered_selection_is_visible(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.set_zoom(1.0)
    view.go_to_page(0, record=False)
    view.select_all()
    image = wait_rendered(qtbot, view)
    pos = _word_pos(view, 0, "Lorem", 0)
    color = image.pixelColor(pos.x() + 2, pos.y())
    assert color.blue() > color.red()  # tinted by the blue selection overlay
