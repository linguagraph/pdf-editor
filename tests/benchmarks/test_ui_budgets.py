"""Performance budgets for the modern UI (Phase U11), headless like CI.

Each figure is the median of several runs, compared with a cap well above the value measured
on a developer machine (Windows 11, offscreen), so a slow CI runner passes while a real
regression (a pixmap per page again, a style sheet applied per widget, ...) does not.
Run them with:  QT_QPA_PLATFORM=offscreen pytest tests/benchmarks/test_ui_budgets.py -s
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget

from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.theme import Theme, apply_theme
from tests.ui.conftest import isolated_settings, no_animations  # noqa: F401 - autouse here too

pytestmark = [pytest.mark.slow, pytest.mark.gui]

# Budgets in ms, about 2.5x the baseline (median of 3 sessions on a Windows 11 dev machine,
# offscreen, Python 3.11, recorded in PLAN.md U11) for slower CI runners.
# PDFEDITOR_PERF_FACTOR scales them all for a known-slow machine.
FACTOR = float(os.environ.get("PDFEDITOR_PERF_FACTOR", "1"))
WINDOW_FIRST_PAINT_MS = 300 * FACTOR  # baseline 105 ms: MainWindow() + show() to first paint
DOCUMENT_FIRST_PAINT_MS = 250 * FACTOR  # baseline 88 ms: open 1000 pages to a painted page
SCROLL_FRAME_MS = 16 * FACTOR  # one 60 fps frame; baseline 5 ms per scroll step and repaint
THEME_SWITCH_MS = 350 * FACTOR  # baseline 120 ms: light <-> dark with a document open
RUNS = 5


class _PaintWatch(QObject):
    """Notes when ``widget`` receives its first paint event."""

    def __init__(self, widget: QWidget) -> None:
        super().__init__()
        self.painted = False
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Paint:
            self.painted = True
        return False


def _pump_until(done: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.perf_counter() + timeout_s
    while not done():
        QApplication.processEvents()
        assert time.perf_counter() < deadline, "timed out"
    QApplication.processEvents()  # the rest of this paint pass


def _median_ms(samples: list[float]) -> float:
    return statistics.median(samples) * 1000


def _report(name: str, samples: list[float], budget_ms: float) -> float:
    median = _median_ms(samples)
    print(f"\n{name}: median {median:.1f} ms (min {min(samples) * 1000:.1f}), budget {budget_ms}")
    return median


def _flush_deletes() -> None:
    """Delete closed widgets now: every live widget is repolished on a theme switch."""
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def windows() -> Iterator[list[MainWindow]]:
    _flush_deletes()
    made: list[MainWindow] = []
    yield made
    for w in made:
        for view in w.views():
            view.session.undo_stack.set_clean()
        w.close()
        w.deleteLater()
    _flush_deletes()


def _new_window(windows: list[MainWindow]) -> MainWindow:
    w = MainWindow()
    windows.append(w)
    w.resize(1280, 800)
    return w


def test_window_first_paint(qtbot, windows: list[MainWindow]) -> None:
    _new_window(windows).close()  # first window pays one-off imports, icon and font caches
    samples = []
    for _ in range(RUNS):
        start = time.perf_counter()
        w = _new_window(windows)
        watch = _PaintWatch(w)
        w.show()
        _pump_until(lambda watch=watch: watch.painted)
        samples.append(time.perf_counter() - start)
        w.close()
    assert _report("window first paint", samples, WINDOW_FIRST_PAINT_MS) < WINDOW_FIRST_PAINT_MS


def test_large_document_first_paint(qtbot, windows: list[MainWindow], fixture_pdf) -> None:
    w = _new_window(windows)
    w.show()
    qtbot.waitExposed(w)
    path = fixture_pdf("large_1000")
    samples = []
    for _ in range(RUNS):
        start = time.perf_counter()
        view = w.open_path(path)
        assert view is not None
        watch = _PaintWatch(view.viewport())
        _pump_until(lambda watch=watch: watch.painted)
        samples.append(time.perf_counter() - start)
        w.renderer.wait_idle(5000)
        view.session.undo_stack.set_clean()
        w.close_current()
        QApplication.processEvents()
    budget = DOCUMENT_FIRST_PAINT_MS
    assert _report("1000-page document first paint", samples, budget) < budget


def test_scrolling_1000_pages_stays_above_60_fps(
    qtbot, windows: list[MainWindow], fixture_pdf
) -> None:
    w = _new_window(windows)
    w.show()
    qtbot.waitExposed(w)
    view = w.open_path(fixture_pdf("large_1000"))
    assert view is not None
    w.renderer.wait_idle(5000)
    QApplication.processEvents()
    bar = view.verticalScrollBar()
    step = max(1, bar.maximum() // 300)  # 300 frames from the first page to the last
    medians = []
    for _ in range(3):
        frames = []
        for value in range(0, bar.maximum() + 1, step):
            start = time.perf_counter()
            bar.setValue(value)
            QApplication.processEvents()  # layout, tile requests and the repaint
            frames.append(time.perf_counter() - start)
        medians.append(statistics.median(frames))
        assert view.current_page >= 990
        bar.setValue(0)
        w.renderer.wait_idle(5000)
        QApplication.processEvents()
    w.renderer.wait_idle(5000)
    assert _report("scroll frame", medians, SCROLL_FRAME_MS) < SCROLL_FRAME_MS


def test_theme_switch(qtbot, windows: list[MainWindow], fixture_pdf) -> None:
    w = _new_window(windows)
    w.show()
    qtbot.waitExposed(w)
    assert w.open_path(fixture_pdf("text_multipage")) is not None
    w.renderer.wait_idle(5000)
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    samples = []
    try:
        for i in range(RUNS * 2):
            theme = Theme.DARK if i % 2 == 0 else Theme.LIGHT
            start = time.perf_counter()
            apply_theme(app, theme, "#0067c0")
            QApplication.processEvents()  # repolish and repaint
            samples.append(time.perf_counter() - start)
    finally:
        apply_theme(app, Theme.SYSTEM)
    assert _report("theme switch", samples, THEME_SWITCH_MS) < THEME_SWITCH_MS
