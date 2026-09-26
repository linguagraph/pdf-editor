from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt

from pdfeditor.core.layout import LayoutMode
from pdfeditor.ui.view.document_view import FitMode
from tests.ui.conftest import wait_rendered

pytestmark = pytest.mark.gui


def _distinct_colors(image, samples: int = 400) -> int:
    colors = set()
    w, h = image.width(), image.height()
    for i in range(samples):
        colors.add(image.pixel((i * 37) % w, (i * 53) % h))
    return len(colors)


def test_renders_pages(qtbot, make_view) -> None:
    view = make_view("images")
    image = wait_rendered(qtbot, view)
    assert _distinct_colors(image) > 5
    assert len(view.renderer.cache) > 0


def test_fit_width_and_zoom_steps(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    assert view.fit_mode is FitMode.WIDTH
    fitted = view.zoom
    view.zoom_in()
    assert view.zoom > fitted and view.fit_mode is FitMode.NONE
    view.set_zoom(1.0)
    view.zoom_out()
    assert view.zoom == pytest.approx(0.75)
    view.set_zoom(1000)
    assert view.zoom == 64.0
    view.fit_page()
    page_px = view.mapFromScene(0, 0).y(), view.mapFromScene(0, 842 + 24).y()
    assert page_px[1] - page_px[0] <= view.viewport().height() + 2


def test_navigation_and_current_page(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.set_zoom(1.0)
    with qtbot.waitSignal(view.current_page_changed):
        view.go_to_page(3)
    assert view.current_page == 3
    view.next_page()
    assert view.current_page == 4
    view.first_page()
    assert view.current_page == 0
    # scrolling updates the current page
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    assert view.current_page == 4


def test_history_back_forward(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.set_zoom(1.0)
    view.go_to_page(2)
    view.go_to_page(4)
    assert view.can_go_back and not view.can_go_forward
    view.go_back()
    assert view.current_page == 2
    view.go_back()
    assert view.current_page == 0
    view.go_forward()
    assert view.current_page == 2
    assert view.can_go_forward


@pytest.mark.parametrize("mode", list(LayoutMode))
def test_layout_modes_keep_page(qtbot, make_view, mode: LayoutMode) -> None:
    view = make_view("text_multipage")
    view.set_zoom(0.5)
    view.go_to_page(3)
    view.set_layout_mode(mode)
    assert view.layout_mode is mode
    assert view.current_page == 3
    if mode is LayoutMode.SINGLE:
        visible = [i for i, item in enumerate(view._items) if item.isVisible()]
        assert visible == [3]
        view.next_page()
        assert [i for i, item in enumerate(view._items) if item.isVisible()] == [4]
    image = wait_rendered(qtbot, view)
    assert _distinct_colors(image) > 3


def test_rotation_swaps_page_shape(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    item = view._items[0]
    w, h = item.boundingRect().width(), item.boundingRect().height()
    view.rotate_view(90)
    assert (item.boundingRect().width(), item.boundingRect().height()) == (h, w)
    view.rotate_view(270)
    assert view.rotation == 0
    wait_rendered(qtbot, view)


def test_internal_link_click_navigates(qtbot, make_view) -> None:
    view = make_view("outline")
    view.set_zoom(1.0)
    view.go_to_page(0, record=False)
    wait_rendered(qtbot, view)
    link_rect = view._scene_rects[0]
    # the GOTO link in the fixture covers (72,100)-(200,120) on page 1
    scene_pt = (link_rect.x0 + 100, link_rect.y0 + 110)
    link = view.link_at(view.mapToScene(view.mapFromScene(*scene_pt)))
    assert link is not None and link.dest is not None and link.dest.page_index == 3
    pos = view.mapFromScene(*scene_pt)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert view.current_page == 3
    assert view.can_go_back


def test_uri_link_emits_signal(qtbot, make_view) -> None:
    view = make_view("outline")
    view.set_zoom(1.0)
    rect = view._scene_rects[0]
    pos: QPoint = view.mapFromScene(rect.x0 + 100, rect.y0 + 140)
    with qtbot.waitSignal(view.link_activated) as blocker:
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert blocker.args[0].uri == "https://example.org/"


def test_page_labels(qtbot, make_view) -> None:
    view = make_view("outline")
    assert view.page_label(1) == "ii"
    assert view.page_index_for_label("iii") == 2
    assert view.page_index_for_label("5") == 4
    assert view.page_index_for_label("99") is None


def test_night_mode_inverts(qtbot, make_view) -> None:
    view = make_view("text_multipage")
    view.set_night_mode(True)
    image = wait_rendered(qtbot, view)
    center = view.mapFromScene(view._scene_rects[0].x0 + 300, view._scene_rects[0].y0 + 400)
    assert image.pixelColor(center).lightness() < 60


def test_layer_toggle_rerenders(qtbot, make_view) -> None:
    view = make_view("layers")
    view.set_zoom(1.0)
    rect = view._scene_rects[0]
    pos = view.mapFromScene(rect.x0 + 150, rect.y0 + 200)
    image = wait_rendered(qtbot, view)
    assert image.pixelColor(pos).red() > 240 and image.pixelColor(pos).green() > 240
    with view.session.lock:
        layer = view.session.document.layers()[1]
        view.session.document.set_layer_visible(layer.id, True)
    view.refresh()
    image = wait_rendered(qtbot, view)
    color = image.pixelColor(pos)
    assert color.red() > 200 and color.green() < 60
