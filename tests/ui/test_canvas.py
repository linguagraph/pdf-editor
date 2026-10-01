"""Document canvas (Phase U4): canvas color, page cards, floating pill, zoom anchor, motion."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.panels.thumbnails import ThumbnailsPanel
from pdfeditor.ui.style.tokens import PAGE_COLORS, PAGE_EDGE_MIN, build_colors, contrast
from pdfeditor.ui.theme import Theme, apply_theme, current_colors
from pdfeditor.ui.view import motion
from pdfeditor.ui.view.document_view import DocumentView, FitMode

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qtbot) -> Iterator[QApplication]:
    instance = QApplication.instance()
    assert isinstance(instance, QApplication)
    yield instance
    apply_theme(instance, Theme.SYSTEM)


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    w = MainWindow()
    w.resize(1200, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.close()
    w.deleteLater()


def _close(a: QColor, b: QColor, tol: int = 3) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a.getRgb()[:3], b.getRgb()[:3], strict=True))


# -- canvas -----------------------------------------------------------------------------------
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_pages_stand_out_from_the_canvas(scheme) -> None:
    c = build_colors(scheme)  # type: ignore[arg-type]
    for page in PAGE_COLORS:
        edge = max(contrast(page, c.canvas), contrast(c.page_outline, c.canvas))
        assert edge >= PAGE_EDGE_MIN, (page, c.canvas)
    assert contrast(c.canvas, c.window) > 1.05  # the canvas isn't mistaken for chrome


def test_canvas_color_follows_the_theme(app, qtbot, make_view) -> None:
    apply_theme(app, Theme.LIGHT)
    view = make_view("text_multipage")
    assert view.backgroundBrush().color().name() == build_colors("light").canvas
    apply_theme(app, Theme.DARK)
    canvas = QColor(current_colors().canvas)
    assert view.backgroundBrush().color().name() == canvas.name()
    assert view.page_outline.name() == current_colors().page_outline
    image = view.viewport().grab().toImage()
    # left of the (centered) page is bare canvas
    assert _close(image.pixelColor(3, image.height() // 2), canvas)


def test_pages_have_a_drop_shadow(app, qtbot, make_view) -> None:
    apply_theme(app, Theme.LIGHT)
    view = make_view("text_multipage", 800, 1200)
    view.set_zoom(0.8)
    view.go_to_page(0, record=False)
    qtbot.wait(20)
    image = view.viewport().grab().toImage()
    canvas = QColor(current_colors().canvas)
    page = view.mapFromScene(view.page_point_to_scene(0, view.page_rect(0).bottom_right))
    dpr = image.devicePixelRatio()
    below = image.pixelColor(round((page.x() - 40) * dpr), round((page.y() + 2) * dpr))
    far = image.pixelColor(round((page.x() - 40) * dpr), round((page.y() + 8) * dpr))
    assert below.lightness() < canvas.lightness() - 8  # shadow right under the page
    assert _close(far, canvas, 4)  # and it fades out within a few pixels


def test_pages_are_centered(qtbot, make_view) -> None:
    view = make_view("text_multipage", 900, 700)
    view.set_zoom(0.5)
    assert view.verticalScrollBar().isVisible()  # Qt's own centring is off by half of it
    t = view.viewportTransform()
    left = t.map(view.page_point_to_scene(0, view.page_rect(0).top_left)).x()
    right = t.map(view.page_point_to_scene(0, view.page_rect(0).bottom_right)).x()
    assert abs(left - (view.viewport().width() - right)) <= 1.5
    assert view.horizontalScrollBar().maximum() == 0


# -- thumbnails -------------------------------------------------------------------------------
def _accent_columns(image, y: int, accent: QColor) -> list[int]:
    return [x for x in range(image.width()) if _close(image.pixelColor(x, y), accent, 6)]


def test_thumbnail_card_outlines_the_current_page(app, qtbot, make_view) -> None:
    apply_theme(app, Theme.LIGHT, "#c42b1c")  # red: easy to tell from page pixels
    view = make_view("text_multipage")
    panel = ThumbnailsPanel()
    qtbot.addWidget(panel)
    panel.resize(240, 900)
    panel.show()
    panel.set_view(view)
    view.go_to_page(1)
    qtbot.wait(20)
    lst = panel.list
    assert lst.currentIndex().row() == 1
    image = lst.viewport().grab().toImage()
    dpr = image.devicePixelRatio()
    accent = QColor(current_colors().accent)
    current = lst.visualRect(lst.model().index(1, 0))
    other = lst.visualRect(lst.model().index(0, 0))
    assert len(_accent_columns(image, round(current.center().y() * dpr), accent)) >= 2
    assert _accent_columns(image, round(other.center().y() * dpr), accent) == []
    # no selection fill: the cell's corner shows the panel background, not a highlight
    corner = image.pixelColor(round((current.left() + 1) * dpr), round((current.top() + 1) * dpr))
    assert _close(corner, image.pixelColor(1, 1), 4)
    # page number below the card
    assert lst.model().index(1, 0).data() == "2"


# -- floating pill ----------------------------------------------------------------------------
def test_pill_replaces_the_status_bar_controls(window: MainWindow, fixture_pdf, qtbot) -> None:
    pill = window.pill
    assert window.navigator is pill.navigator and window.zoom_box is pill.zoom_box
    assert not pill.isVisible()  # no document
    view = window.open_path(fixture_pdf("text_multipage"))
    assert view is not None
    assert pill.parent() is view.viewport() and pill.isVisible()
    assert not window.statusBar().isAncestorOf(pill)
    # bottom centre of the page area
    area = view.viewport().rect()
    g = pill.geometry()
    assert abs(g.center().x() - area.center().x()) <= 1
    assert area.bottom() - g.bottom() < 30 and g.bottom() <= area.bottom()
    nav = window.navigator
    assert nav.edit.text() == "1" and nav.total.text() == "/ 5"

    qtbot.mouseClick(nav.next, Qt.MouseButton.LeftButton)
    assert view.current_page == 1 and nav.edit.text() == "2"
    qtbot.mouseClick(nav.prev, Qt.MouseButton.LeftButton)
    assert view.current_page == 0 and not nav.prev.isEnabled()
    nav.edit.setText("4")
    nav.edit.returnPressed.emit()
    assert view.current_page == 3

    view.set_zoom(1.0)
    qtbot.mouseClick(pill.zoom_in_button, Qt.MouseButton.LeftButton)
    assert view.zoom == pytest.approx(1.25)
    assert window.zoom_box.currentText() == "125%"
    qtbot.mouseClick(pill.zoom_out_button, Qt.MouseButton.LeftButton)
    assert view.zoom == pytest.approx(1.0)
    qtbot.mouseClick(pill.fit_page_button, Qt.MouseButton.LeftButton)
    assert view.fit_mode is FitMode.PAGE
    qtbot.mouseClick(pill.fit_width_button, Qt.MouseButton.LeftButton)
    assert view.fit_mode is FitMode.WIDTH

    window.close_current()
    assert pill.parent() is window and not pill.isVisible()


def test_pill_follows_the_current_tab(window: MainWindow, fixture_pdf) -> None:
    a = window.open_path(fixture_pdf("text_multipage"))
    b = window.open_path(fixture_pdf("outline"))
    assert window.pill.parent() is b.viewport()
    assert window.navigator.total.text() == "(1 / 6)"  # page labels: i, ii, ...
    window.tabs.setCurrentIndex(0)
    assert window.pill.parent() is a.viewport() and window.navigator.total.text() == "/ 5"
    window.current_tab().set_organizing(True)
    assert not window.pill.isVisible()  # the page view is hidden
    window.current_tab().set_organizing(False)
    assert window.pill.isVisible()


def test_pill_fades_out_and_comes_back(window: MainWindow, fixture_pdf, qtbot) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    pill = window.pill
    QTest.mouseMove(view.viewport(), QPoint(10, 10))  # not over the pill
    assert pill.opacity == 1.0
    pill._on_idle()
    assert pill.opacity == 0.0 and pill.faded
    QTest.mouseMove(view.viewport(), QPoint(30, 40))
    assert pill.opacity == 1.0 and not pill.faded
    pill._on_idle()
    view.verticalScrollBar().setValue(view.verticalScrollBar().value() + 50)  # scrolling
    assert pill.opacity == 1.0

    # with animations on, it fades over a short time and the idle timer starts it
    motion.set_animations_enabled(True)
    pill.idle_ms = 30
    pill.poke()
    qtbot.waitUntil(lambda: pill.faded, timeout=2000)
    qtbot.waitUntil(lambda: pill.opacity == 0.0, timeout=2000)
    pill.navigator.edit.setFocus()  # keyboard focus brings it back
    qtbot.waitUntil(lambda: pill.opacity == 1.0, timeout=2000)


def test_pill_is_keyboard_reachable_and_named(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    pill = window.pill
    controls = [
        w
        for w in pill.findChildren(QWidget)
        if w.isVisibleTo(pill) and w.focusPolicy() != Qt.FocusPolicy.NoFocus
    ]
    assert len(controls) >= 7
    assert all(w.accessibleName() for w in controls)
    assert pill.accessibleName()
    # Tab from the page (the view and its viewport) goes into the pill
    w = view.nextInFocusChain()
    for _ in range(10):
        tab = w.focusPolicy() & Qt.FocusPolicy.TabFocus
        if tab and w.isEnabled() and w.isVisibleTo(window) and w is not view.viewport():
            break
        w = w.nextInFocusChain()
    assert pill.isAncestorOf(w) and w is pill.navigator.edit  # "previous" is off on page 1


# -- zoom anchor ------------------------------------------------------------------------------
def _ctrl_wheel(view: DocumentView, pos: QPointF, notches: int) -> None:
    event = QWheelEvent(
        pos,
        QPointF(view.viewport().mapToGlobal(pos.toPoint())),
        QPoint(),
        QPoint(0, 120 * notches),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(view.viewport(), event)


def _pinch(view: DocumentView, pos: QPointF, value: float) -> None:
    event = QNativeGestureEvent(
        Qt.NativeGestureType.ZoomNativeGesture,
        QPointingDevice.primaryPointingDevice(),
        2,
        pos,
        pos,
        QPointF(view.viewport().mapToGlobal(pos.toPoint())),
        value,
        QPointF(),
    )
    QApplication.sendEvent(view.viewport(), event)


def _assert_anchored(view: DocumentView, scene: QPointF, pos: QPointF) -> None:
    now = view.mapFromScene(scene)
    assert abs(now.x() - pos.x()) <= 1.5 and abs(now.y() - pos.y()) <= 1.5


@pytest.mark.parametrize("notches", [1, 3, -2])
def test_ctrl_wheel_keeps_the_point_under_the_cursor(qtbot, make_view, notches) -> None:
    view = make_view("text_multipage", 700, 600)
    view.set_zoom(1.5)  # wider than the viewport: both scroll bars move
    view.go_to_page(1, record=False)
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum() // 2)
    pos = QPointF(520, 410)
    scene = view.mapToScene(pos.toPoint())
    before = view.zoom
    _ctrl_wheel(view, pos, notches)
    assert view.zoom == pytest.approx(before * 1.1**notches)
    assert view.fit_mode is FitMode.NONE
    _assert_anchored(view, scene, pos)


def test_pinch_keeps_the_point_under_the_fingers(qtbot, make_view) -> None:
    view = make_view("text_multipage", 700, 600)
    view.set_zoom(1.5)
    view.go_to_page(2, record=False)
    pos = QPointF(200, 150)
    scene = view.mapToScene(pos.toPoint())
    for _ in range(5):
        _pinch(view, pos, 0.08)
    assert view.zoom == pytest.approx(1.5 * 1.08**5)
    _assert_anchored(view, scene, pos)


# -- animations -------------------------------------------------------------------------------
def test_animations_follow_the_override() -> None:
    assert isinstance(motion.system_animations_enabled(), bool)
    motion.set_animations_enabled(True)
    assert motion.animations_enabled()
    motion.set_animations_enabled(False)
    assert not motion.animations_enabled()


def test_animated_wheel_zoom_eases_to_the_target(qtbot, make_view) -> None:
    motion.set_animations_enabled(True)
    view = make_view("text_multipage", 700, 600)
    view.set_zoom(1.5)
    view.go_to_page(1, record=False)
    pos = QPointF(300, 250)
    scene = view.mapToScene(pos.toPoint())
    _ctrl_wheel(view, pos, 2)
    assert view.zoom_animating and view.zoom < 1.5 * 1.21 - 1e-3  # still on its way
    _ctrl_wheel(view, pos, 1)  # a notch while it runs adds to the target
    qtbot.waitUntil(lambda: not view.zoom_animating, timeout=2000)
    assert view.zoom == pytest.approx(1.5 * 1.1**3)
    _assert_anchored(view, scene, pos)


def test_animated_page_jump(qtbot, make_view) -> None:
    view = make_view("text_multipage", 700, 600)
    view.set_zoom(1.0)
    view.go_to_page(3, record=False)
    expected = view.verticalScrollBar().value()
    view.go_to_page(0, record=False)

    motion.set_animations_enabled(True)
    pages: list[int] = []
    view.current_page_changed.connect(pages.append)
    view.go_to_page(3, animated=True)
    assert view.current_page == 3 and pages == [3]  # the page changes right away
    assert view.verticalScrollBar().value() != expected  # the scroll is still under way
    qtbot.waitUntil(lambda: view.verticalScrollBar().value() == expected, timeout=2000)
    qtbot.wait(30)
    assert view.current_page == 3 and pages == [3]  # no flicker through other pages
    assert view.can_go_back  # history is recorded when the jump lands
    view.go_back()
    assert view.current_page == 0


def test_no_animation_when_the_system_turns_them_off(qtbot, make_view) -> None:
    motion.set_animations_enabled(False)
    view = make_view("text_multipage", 700, 600)
    view.set_zoom(1.0)
    view.go_to_page(3, record=False)
    expected = view.verticalScrollBar().value()
    view.go_to_page(0, record=False)
    view.go_to_page(3, animated=True)
    assert view.verticalScrollBar().value() == expected  # landed synchronously
    _ctrl_wheel(view, QPointF(100, 100), 1)
    assert not view.zoom_animating and view.zoom == pytest.approx(1.1)
    view.zoom_in()
    assert view.zoom == pytest.approx(1.25)
