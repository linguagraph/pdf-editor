"""Performance benchmarks on the 1000-page fixture.

By default (``--benchmark-disable`` in pyproject) each runs once as a smoke test with generous
time limits that catch gross regressions. For real numbers:

    pytest tests/benchmarks --benchmark-enable
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtGui import QImage

from pdfeditor.core.render_cache import RenderCache
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import RenderRequest
from pdfeditor.model.geometry import Matrix
from pdfeditor.services.search import SearchQuery, search_document
from pdfeditor.services.text import TextIndexCache

pytestmark = pytest.mark.slow


@pytest.fixture
def large(fixture_pdf):
    path = fixture_pdf("large_1000")
    yield path


def test_open_large(benchmark, large) -> None:
    def open_close() -> int:
        s = DocumentSession.open(large)
        n = s.page_count
        s.close()
        return n

    t0 = time.perf_counter()
    assert benchmark(open_close) == 1000
    assert time.perf_counter() - t0 < 5


def test_render_first_page_150(benchmark, large) -> None:
    s = DocumentSession.open(large)
    page = s.document.page(0)
    request = RenderRequest(matrix=Matrix.scale(1.5 * 96 / 72))
    t0 = time.perf_counter()
    result = benchmark(page.render, request)
    assert result.width > 1000
    assert time.perf_counter() - t0 < 5
    s.close()


def test_search_whole_document(benchmark, large) -> None:
    s = DocumentSession.open(large)

    def run() -> int:
        cache = TextIndexCache(s)  # cold cache each round: measures extraction too
        return len(search_document(cache, SearchQuery("Large document page 999")))

    t0 = time.perf_counter()
    assert benchmark(run) == 1
    assert time.perf_counter() - t0 < 60
    s.close()


@pytest.mark.gui
def test_view_first_paint_and_scroll(qtbot, benchmark, large) -> None:
    from pdfeditor.ui.view.document_view import DocumentView
    from pdfeditor.ui.view.renderer import TileRenderer
    from tests.ui.conftest import wait_rendered

    renderer = TileRenderer(RenderCache[QImage](128 * 1024 * 1024))
    s = DocumentSession.open(large)

    def first_paint() -> DocumentView:
        view = DocumentView(s, renderer)
        qtbot.addWidget(view)
        view.resize(900, 700)
        view.show()
        qtbot.waitExposed(view)
        wait_rendered(qtbot, view)
        return view

    t0 = time.perf_counter()
    view = benchmark.pedantic(first_paint, rounds=1, iterations=1)
    first = time.perf_counter() - t0
    assert first < 5

    # scroll through the whole document in viewport steps, painting each position
    bar = view.verticalScrollBar()
    t0 = time.perf_counter()
    step = max(1, bar.maximum() // 60)
    for value in range(0, bar.maximum() + 1, step):
        bar.setValue(value)
        view.viewport().repaint()
    scroll = time.perf_counter() - t0
    assert view.current_page >= 990
    assert scroll < 20
    renderer.wait_idle()
    s.close()
