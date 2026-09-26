from __future__ import annotations

import pytest

from pdfeditor.core.layout import (
    PAGE_GAP,
    LayoutMode,
    compute_layout,
    layout_bounds,
    page_at,
    rotated_size,
    view_matrix,
)
from pdfeditor.core.render_cache import RenderCache, TileKey, quantize_scale
from pdfeditor.model.geometry import Point, Rect

A4 = (595.0, 842.0)
LETTER_LANDSCAPE = (792.0, 612.0)


def key(page: int, tile: int = 0, doc: int = 1) -> TileKey:
    return TileKey(doc, page, 0, 1000, 0, tile, 0)


def test_continuous_layout_stacks_and_centers() -> None:
    rects = compute_layout([A4, LETTER_LANDSCAPE, A4], LayoutMode.CONTINUOUS)
    assert rects[0] == Rect(
        PAGE_GAP + (792 - 595) / 2, PAGE_GAP, PAGE_GAP + (792 + 595) / 2, PAGE_GAP + 842
    )
    assert rects[1].y0 == pytest.approx(rects[0].y1 + PAGE_GAP)
    assert rects[1].width == 792
    bounds = layout_bounds(rects)
    assert bounds.width == pytest.approx(792 + 2 * PAGE_GAP)


def test_two_up_and_cover() -> None:
    two = compute_layout([A4] * 3, LayoutMode.TWO_UP)
    assert two[0].y0 == two[1].y0 and two[1].x0 > two[0].x1
    assert two[2].y0 > two[0].y1
    cover = compute_layout([A4] * 3, LayoutMode.TWO_UP_COVER)
    assert cover[0].x0 == two[1].x0  # cover sits in the right column
    assert cover[1].y0 == cover[2].y0 and cover[1].y0 > cover[0].y1


def test_rotation_swaps_dimensions() -> None:
    assert rotated_size(100, 200, 90) == (200, 100)
    assert rotated_size(100, 200, 180) == (100, 200)
    rects = compute_layout([A4], LayoutMode.CONTINUOUS, rotation=270)
    assert (rects[0].width, rects[0].height) == (842, 595)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_view_matrix_maps_page_into_positive_quadrant(rotation: int) -> None:
    page = Rect(0, 0, 595, 842)
    m = view_matrix(page, rotation, 2.0)
    mapped = page.transform(m)
    w, h = rotated_size(595, 842, rotation)
    assert mapped.x0 == pytest.approx(0, abs=1e-6) and mapped.y0 == pytest.approx(0, abs=1e-6)
    assert mapped.x1 == pytest.approx(2 * w) and mapped.y1 == pytest.approx(2 * h)
    # round trip through the inverse
    p = Point(100, 200).transform(m).transform(m.inverted())
    assert (p.x, p.y) == pytest.approx((100, 200))


def test_page_at() -> None:
    rects = compute_layout([A4] * 3, LayoutMode.CONTINUOUS)
    assert page_at(rects, rects[1].y0 + 10) == 1
    assert page_at(rects, rects[1].y1 + PAGE_GAP / 2 - 1) in (1, 2)
    assert page_at(rects, -100) == 0
    assert page_at(rects, 1e9) == 2
    assert page_at([], 0) == -1


def test_quantize_scale_is_stable() -> None:
    assert quantize_scale(1.0) == 1000
    assert quantize_scale(1.001) == quantize_scale(0.999)
    assert quantize_scale(2.0) != quantize_scale(1.9)
    with pytest.raises(ValueError):
        quantize_scale(0)


def test_render_cache_lru_and_budget() -> None:
    cache: RenderCache[str] = RenderCache(max_bytes=100)
    cache.put(key(0), "a", 40)
    cache.put(key(1), "b", 40)
    assert cache.get(key(0)) == "a"  # touch: key(1) is now least recent
    cache.put(key(2), "c", 40)
    assert key(1) not in cache and key(0) in cache and key(2) in cache
    assert cache.size_bytes == 80
    cache.put(key(2), "c2", 10)  # replace updates cost
    assert cache.size_bytes == 50
    cache.put(key(3, doc=2), "d", 10)
    cache.drop_document(1)
    assert len(cache) == 1 and cache.size_bytes == 10
    cache.clear()
    assert len(cache) == 0 and cache.size_bytes == 0


def test_oversized_item_still_cached() -> None:
    cache: RenderCache[str] = RenderCache(max_bytes=10)
    cache.put(key(0), "big", 50)
    assert cache.get(key(0)) == "big"
