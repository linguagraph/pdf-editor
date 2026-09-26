from __future__ import annotations

import pytest

from pdfeditor.core.jobs import Cancelled, CancelToken
from pdfeditor.core.session import DocumentSession
from pdfeditor.model.geometry import Point
from pdfeditor.model.text import SearchOptions
from pdfeditor.services.pages import format_page_ranges, parse_page_ranges
from pdfeditor.services.search import SearchQuery, compile_query, search_document
from pdfeditor.services.text import LINE_SEP, TextIndexCache, TextPos, TextSelection


@pytest.fixture
def session(fixture_pdf):
    s = DocumentSession.open(fixture_pdf("text_multipage"))
    yield s
    s.close()


@pytest.fixture
def cache(session) -> TextIndexCache:
    return TextIndexCache(session)


def test_index_text_matches_chars(cache: TextIndexCache) -> None:
    index = cache.get(0)
    assert len(index.text) == len(index.chars)
    assert index.text.startswith("Page 1 heading" + LINE_SEP + "Lorem ipsum")
    assert index.text.endswith("needle-1")


def test_cache_reuses_until_revision_changes(cache: TextIndexCache, session) -> None:
    first = cache.get(0)
    assert cache.get(0) is first
    session.document.mark_page_changed(0)
    assert cache.get(0) is not first


def test_hit_test_and_word_line_ranges(cache: TextIndexCache) -> None:
    index = cache.get(0)
    heading = index.line_rects[0]
    # a point in the middle of "heading" (third word of the first line)
    h = index.text.index("heading")
    mid = index.chars[h + 3].bbox.center
    caret = index.hit_test(mid)
    assert caret is not None and h + 3 <= caret <= h + 4
    assert index.extract(*index.word_range(h + 3)) == "heading"
    assert index.extract(*index.line_range(h)) == "Page 1 heading"
    # far outside any text
    assert index.hit_test(Point(5, 5)) is None
    # left of a line snaps to its start; nearest() never gives up
    assert index.hit_test(Point(heading.x0 - 2, heading.center.y)) == 0
    assert index.nearest(Point(5, 5)) == 0


def test_rects_one_per_line(cache: TextIndexCache) -> None:
    index = cache.get(0)
    start = index.text.index("Page")
    end = index.text.index("ipsum") + len("ipsum")
    rects = index.rects(start, end)
    assert len(rects) == 2  # heading line + first body line
    assert rects[0].y1 <= rects[1].y0 + 1


def test_multi_page_selection(cache: TextIndexCache) -> None:
    last0 = len(cache.get(0))
    sel = TextSelection(TextPos(1, 5), TextPos(0, last0 - 8))  # anchor after focus: normalized
    assert sel.start == TextPos(0, last0 - 8)
    assert sel.text(cache) == "needle-1" + LINE_SEP + "Page "
    rects = sel.rects(cache)
    assert set(rects) == {0, 1}
    assert TextSelection(TextPos(0, 3), TextPos(0, 3)).is_empty


def test_compile_query_variants() -> None:
    assert compile_query(SearchQuery("dolor sit")).search("DOLOR\nsit")
    assert not compile_query(SearchQuery("Dolor", SearchOptions(case_sensitive=True))).search(
        "dolor"
    )
    whole = compile_query(SearchQuery("sit", SearchOptions(whole_word=True)))
    assert whole.search("dolor sit amet") and not whole.search("situation")
    assert compile_query(SearchQuery(r"needle-\d", SearchOptions(regex=True))).search("needle-4")
    assert compile_query(SearchQuery("a.b")).search("a.b") and not compile_query(
        SearchQuery("a.b")
    ).search("axb")
    with pytest.raises(ValueError):
        compile_query(SearchQuery("   "))
    with pytest.raises(ValueError):
        compile_query(SearchQuery("(", SearchOptions(regex=True)))


def test_search_document(cache: TextIndexCache) -> None:
    hits = search_document(cache, SearchQuery(r"needle-\d", SearchOptions(regex=True)))
    assert [h.page_index for h in hits] == [0, 1, 2, 3, 4]
    assert hits[2].text == "needle-3" and "[needle-3]" in hits[2].context
    # start page wraps around, streaming callback + progress
    seen: list[int] = []
    progress: list[tuple[int, int]] = []
    search_document(
        cache,
        SearchQuery("needle"),
        on_page=lambda p, _h: seen.append(p),
        progress=lambda d, t: progress.append((d, t)),
        start_page=3,
    )
    assert seen == [3, 4, 0, 1, 2] and progress[-1] == (5, 5)
    # restricted pages
    only = search_document(cache, SearchQuery("needle", SearchOptions(page_indices=(1,))))
    assert [h.page_index for h in only] == [1]


def test_search_across_line_break(cache: TextIndexCache) -> None:
    index = cache.get(0)
    # the heading line ends with "heading" and the next line starts with "Lorem"
    hits = search_document(cache, SearchQuery("heading Lorem", SearchOptions(page_indices=(0,))))
    assert len(hits) == 1 and len(hits[0].quads) == 2
    assert hits[0].quads[0].rect.y1 <= hits[0].quads[1].rect.y0 + 1
    assert index.text.count("heading") >= 1


def test_search_cancel(cache: TextIndexCache) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        search_document(cache, SearchQuery("needle"), token=token)


def test_search_rotated_page_hits_visible_space(fixture_pdf) -> None:
    session = DocumentSession.open(fixture_pdf("rotated_pages"))
    cache = TextIndexCache(session)
    (hit,) = search_document(cache, SearchQuery("Rotation 90"))
    engine_hit = session.document.page(1).search("Rotation 90")[0]
    assert hit.rect.intersects(engine_hit.rect)
    session.close()


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("", [0, 1, 2, 3, 4]),
        ("all", [0, 1, 2, 3, 4]),
        ("1-3, 5", [0, 1, 2, 4]),
        ("-2", [0, 1]),
        ("4-", [3, 4]),
        ("2;2,2", [1]),
    ],
)
def test_parse_page_ranges(spec: str, expected: list[int]) -> None:
    assert parse_page_ranges(spec, 5) == expected


@pytest.mark.parametrize("spec", ["0", "6", "3-2", "x", "1-99"])
def test_parse_page_ranges_errors(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_page_ranges(spec, 5)


def test_format_page_ranges() -> None:
    assert format_page_ranges([0, 1, 2, 4, 7, 8]) == "1-3, 5, 8-9"
    assert format_page_ranges([]) == ""
