"""The text calls of W3.4 (text.layer, text.extract, text.search) through dispatch, on fixtures.

The chunked calls are tested with a small page budget (``limits.TEXT_PAGES_PER_SLICE`` is read at
call time) so that one document takes several slices.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from typing import Any

import pytest

from pdfeditor.model import platform as model_platform
from pdfeditor.services.text import LINE_SEP, TextIndexCache, TextPos, TextSelection
from pdfeditor.webapi import dispatch, limits
from pdfeditor.webapi.errors import INVALID_ARGS, NO_DOCUMENT, STALE_CURSOR
from pdfeditor.webapi.platform import configure_web
from pdfeditor.webapi.registry import REGISTRY


@pytest.fixture
def web() -> Iterator[None]:
    previous = model_platform.current()
    configure_web()
    try:
        yield
    finally:
        REGISTRY.close_all()
        model_platform.configure(previous)


def call_raw(
    method: str, params: dict[str, Any], buffers: list[bytes] | None = None
) -> tuple[dict[str, Any], str, list[bytes]]:
    request = json.dumps({"v": 1, "id": 7, "method": method, "params": params})
    reply_json, out = dispatch(request, buffers or [])
    return json.loads(reply_json), reply_json, out


def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    reply, _, _ = call_raw(method, params)
    return reply


def ok(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["ok"] is True, reply
    result: dict[str, Any] = reply["result"]
    return result


def error(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["ok"] is False, reply
    err: dict[str, Any] = reply["error"]
    return err


def open_bytes(fixture_pdf: Any, name: str) -> str:
    data = fixture_pdf(name).read_bytes()
    reply, _, out = call_raw(
        "session.open",
        {"name": f"{name}.pdf", "data": {"$bin": 0}, "password": None},
        [data],
    )
    assert out == []
    return str(ok(reply)["handle"])


def layer(handle: str, page: int) -> dict[str, Any]:
    return ok(call("text.layer", {"handle": handle, "page": page}))


def search_all(
    handle: str, query: str, *, start: int = 0, budget: int = 1000, **options: bool
) -> list[dict[str, Any]]:
    """Every slice of a chunked search, as a list of slice results (cursor followed each time)."""
    opts = {"case_sensitive": False, "whole_word": False, "regex": False, **options}
    slices: list[dict[str, Any]] = []
    cursor = None
    while True:
        result = ok(
            call(
                "text.search",
                {
                    "handle": handle,
                    "query": query,
                    "options": opts,
                    "start_page": start,
                    "cursor": cursor,
                    "budget_ms": budget,
                },
            )
        )
        slices.append(result)
        cursor = result["cursor"]
        if cursor is None:
            return slices
        assert len(slices) < 100, "the search never finished"


def hits_of(slices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [hit for piece in slices for hit in piece["hits"]]


def page_rect(handle: str, page: int) -> tuple[float, float]:
    sizes = ok(call("session.pages", {"handle": handle}))["pages"]
    return sizes[page]["width"], sizes[page]["height"]


def decode_boxes(data: bytes) -> list[tuple[float, float, float, float]]:
    values = struct.unpack(f"<{len(data) // 4}f", data)
    return [tuple(values[i : i + 4]) for i in range(0, len(values), 4)]  # type: ignore[misc]


def split_chars(out: list[bytes]) -> tuple[bytes, bytes]:
    """The text layer's one buffer: the char boxes (16 bytes a char), then the line numbers."""
    assert len(out) == 1
    count = len(out[0]) // 20
    assert len(out[0]) == count * 20
    return out[0][: count * 16], out[0][count * 16 :]


def decode_u32(data: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(data) // 4}I", data))


# -- text layer --------------------------------------------------------------------------------


def test_text_layer_boxes_are_visible_space_points(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    width, height = page_rect(handle, 2)
    reply, _, out = call_raw("text.layer", {"handle": handle, "page": 2})
    result = ok(reply)
    box_bytes, line_bytes = split_chars(out)  # a reply carries one buffer
    text = result["text"]
    assert "needle-3" in text
    boxes = decode_boxes(box_bytes)
    lines = decode_u32(line_bytes)
    assert len(boxes) == len(text) == len(lines)
    start = text.index("needle-3")
    for x0, y0, x1, y1 in boxes[start : start + len("needle-3")]:
        assert 0 <= x0 <= x1 <= width
        assert 0 <= y0 <= y1 <= height
        # the footer is 40 pt above the bottom edge, so in top-left visible space it sits low
        assert y0 > height * 0.8
    assert len(result["line_rects"]) >= 3
    for rect in result["line_rects"]:
        assert 0 <= rect["y0"] <= rect["y1"] <= height


def test_text_layer_top_of_page_is_small_y(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    _, height = page_rect(handle, 0)
    reply, _, out = call_raw("text.layer", {"handle": handle, "page": 0})
    text = ok(reply)["text"]
    boxes = decode_boxes(split_chars(out)[0])
    start = text.index("heading")
    for _x0, _y0, _x1, y1 in boxes[start : start + 7]:
        assert y1 < height * 0.2  # the heading is at 72 pt from the top


def test_text_layer_separators_have_empty_boxes(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    reply, _, out = call_raw("text.layer", {"handle": handle, "page": 0})
    result = ok(reply)
    text = result["text"]
    assert LINE_SEP in text
    boxes = decode_boxes(split_chars(out)[0])
    for i, ch in enumerate(text):
        if ch == LINE_SEP:
            assert boxes[i] == (0.0, 0.0, 0.0, 0.0)


def test_text_layer_revision_is_the_engine_revision(web: None, fixture_pdf: Any) -> None:
    from pdfeditor.engine.registry import get_engine

    path = fixture_pdf("text_multipage")
    handle = open_bytes(fixture_pdf, "text_multipage")
    doc = get_engine().open(path)
    try:
        expected = doc.page(0).revision
    finally:
        doc.close()
    assert layer(handle, 0)["revision"] == expected


def test_text_layer_on_rotated_pages_stays_in_the_visible_rect(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "rotated_pages")
    sizes = ok(call("session.pages", {"handle": handle}))["pages"]
    for page, size in enumerate(sizes):
        reply, _, out = call_raw("text.layer", {"handle": handle, "page": page})
        boxes = decode_boxes(split_chars(out)[0])
        for x0, y0, x1, y1 in boxes:
            if x0 == y0 == x1 == y1 == 0.0:
                continue
            assert -0.5 <= x0 <= x1 <= size["width"] + 0.5, page
            assert -0.5 <= y0 <= y1 <= size["height"] + 0.5, page
        ok(reply)


# -- selection to text -------------------------------------------------------------------------


def pos(page: int, index: int) -> dict[str, int]:
    return {"page": page, "index": index}


def extract(handle: str, start: dict[str, int], end: dict[str, int], **kw: Any) -> dict[str, Any]:
    params = {"handle": handle, "start": start, "end": end, "cursor": None, "budget_ms": 1000}
    params.update(kw)
    return ok(call("text.extract", params))


def test_selection_of_one_word_gives_that_word(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    text = layer(handle, 0)["text"]
    start = text.index("heading")
    result = extract(handle, pos(0, start), pos(0, start + len("heading")))
    assert result == {"text": "heading", "cursor": None, "pages_done": 1, "total": 1, "done": True}


def test_selection_order_does_not_matter(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    text = layer(handle, 0)["text"]
    start = text.index("heading")
    forward = extract(handle, pos(0, start), pos(0, start + 7))
    backward = extract(handle, pos(0, start + 7), pos(0, start))
    assert forward == backward


def test_selection_across_pages_joins_with_a_line_separator(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    result = extract(handle, pos(0, 0), pos(2, 0))
    assert result["total"] == 3 and result["done"] is True
    pieces = result["text"].split(LINE_SEP)
    assert "Page 1 heading" in pieces[0].replace("\n", " ")
    assert "Page 2 heading" in result["text"].replace("\n", " ")


def test_empty_selection_gives_empty_text(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    assert extract(handle, pos(1, 4), pos(1, 4))["text"] == ""


def test_chunked_extract_equals_one_call(
    web: None, fixture_pdf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    full = extract(handle, pos(0, 0), pos(4, 0))
    monkeypatch.setattr(limits, "TEXT_PAGES_PER_SLICE", 2)
    pieces: list[str] = []
    cursor = None
    slices = 0
    while True:
        part = extract(handle, pos(0, 0), pos(4, 0), cursor=cursor)
        pieces.append(part["text"])
        slices += 1
        cursor = part["cursor"]
        if cursor is None:
            break
    assert slices == 3  # 5 pages, 2 per slice
    # A slice with no text is empty; the caller joins the non-empty slices.
    assert LINE_SEP.join(p for p in pieces if p) == full["text"]
    assert part["done"] is True and part["pages_done"] == 5


def test_chunked_whole_document_copy_equals_the_desktop_selection(
    web: None, fixture_pdf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # W5.4: a whole-document Copy joins its slices (largeCopy.ts) as the desktop's
    # TextSelection.text joins the pages, so the web copy and the desktop copy give the same string.
    handle = open_bytes(fixture_pdf, "text_multipage")
    session = REGISTRY.get(handle)
    cache = TextIndexCache(session)
    last = session.document.page_count - 1
    end = pos(last, len(cache.get(last)))
    desktop = TextSelection(TextPos(0, 0), TextPos(last, len(cache.get(last)))).text(cache)
    assert desktop
    monkeypatch.setattr(limits, "TEXT_PAGES_PER_SLICE", 1)
    pieces: list[str] = []
    cursor = None
    while True:
        part = extract(handle, pos(0, 0), end, cursor=cursor)
        pieces.append(part["text"])
        cursor = part["cursor"]
        if cursor is None:
            break
    assert LINE_SEP.join(p for p in pieces if p) == desktop


# -- search ------------------------------------------------------------------------------------


def test_search_finds_a_term_on_the_expected_page(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    slices = search_all(handle, "needle-3")
    assert len(slices) == 1
    result = slices[0]
    assert result["done"] is True and result["cursor"] is None
    assert result["pages_done"] == 5 and result["total"] == 5
    (hit,) = result["hits"]
    assert hit["page"] == 2
    assert hit["text"] == "needle-3"
    assert "[needle-3]" in hit["context"]
    assert len(hit["quads"]) >= 1
    quad = hit["quads"][0]
    assert set(quad) == {"ul", "ur", "ll", "lr"}


def test_search_hits_come_in_page_order(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    pages = [hit["page"] for hit in hits_of(search_all(handle, "heading"))]
    assert pages == [0, 1, 2, 3, 4]


def test_chunked_search_finds_the_same_hits_as_one_call(
    web: None, fixture_pdf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    full = hits_of(search_all(handle, "needle", start=3))
    monkeypatch.setattr(limits, "TEXT_PAGES_PER_SLICE", 2)
    slices = search_all(handle, "needle", start=3)
    assert len(slices) == 3
    assert [s["pages_done"] for s in slices] == [2, 4, 5]
    assert hits_of(slices) == full
    assert [hit["page"] for hit in full] == [3, 4, 0, 1, 2]  # wraps around from the start page


def test_search_cursor_round_trip(
    web: None, fixture_pdf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    monkeypatch.setattr(limits, "TEXT_PAGES_PER_SLICE", 2)
    first = ok(
        call(
            "text.search",
            {
                "handle": handle,
                "query": "needle",
                "options": {"case_sensitive": False, "whole_word": False, "regex": False},
                "start_page": 0,
                "cursor": None,
                "budget_ms": 1000,
            },
        )
    )
    assert first["done"] is False and first["cursor"] is not None
    assert first["pages_done"] == 2 and first["total"] == 5
    assert [h["page"] for h in first["hits"]] == [0, 1]
    second = ok(
        call(
            "text.search",
            {
                "handle": handle,
                "query": "needle",
                "options": {"case_sensitive": False, "whole_word": False, "regex": False},
                "start_page": 0,
                "cursor": first["cursor"],
                "budget_ms": 1000,
            },
        )
    )
    assert [h["page"] for h in second["hits"]] == [2, 3]
    # A cursor is tied to the start page it was made for.
    mismatch = error(
        call(
            "text.search",
            {
                "handle": handle,
                "query": "needle",
                "options": {"case_sensitive": False, "whole_word": False, "regex": False},
                "start_page": 4,
                "cursor": first["cursor"],
                "budget_ms": 1000,
            },
        )
    )
    assert mismatch["code"] == INVALID_ARGS
    assert mismatch["details"]["path"] == "params.start_page"


def test_search_cursor_from_another_page_count_is_stale(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    reply = call(
        "text.search",
        {
            "handle": handle,
            "query": "needle",
            "options": {"case_sensitive": False, "whole_word": False, "regex": False},
            "start_page": 0,
            "cursor": "v99:s0:o1",
            "budget_ms": 1000,
        },
    )
    assert error(reply)["code"] == STALE_CURSOR


def test_malformed_search_cursor_is_invalid_args(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    reply = call(
        "text.search",
        {
            "handle": handle,
            "query": "needle",
            "options": {"case_sensitive": False, "whole_word": False, "regex": False},
            "start_page": 0,
            "cursor": "garbage",
            "budget_ms": 1000,
        },
    )
    err = error(reply)
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.cursor"


def test_unknown_term_has_no_hits_and_is_done(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    slices = search_all(handle, "zqxjvk")
    assert len(slices) == 1
    assert slices[0]["hits"] == [] and slices[0]["done"] is True


def test_search_options_case_and_whole_word(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    assert len(hits_of(search_all(handle, "NEEDLE-3"))) == 1
    assert hits_of(search_all(handle, "NEEDLE-3", case_sensitive=True)) == []
    assert len(hits_of(search_all(handle, "heading", whole_word=True))) == 5
    assert hits_of(search_all(handle, "headin", whole_word=True)) == []
    assert len(hits_of(search_all(handle, "headin"))) == 5


def test_search_regex_option(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    found = hits_of(search_all(handle, r"needle-[24]", regex=True))
    assert [h["page"] for h in found] == [1, 3]
    assert [h["text"] for h in found] == ["needle-2", "needle-4"]


def test_search_multi_word_query_matches_across_line_breaks(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    assert len(hits_of(search_all(handle, "Page 1 heading"))) == 1


# -- errors ------------------------------------------------------------------------------------


def search_params(handle: str, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "handle": handle,
        "query": "needle",
        "options": {"case_sensitive": False, "whole_word": False, "regex": False},
        "start_page": 0,
        "cursor": None,
        "budget_ms": 1000,
    }
    params.update(overrides)
    return params


def test_unknown_handle_is_no_document(web: None) -> None:
    unknown = "0" * 32
    for method, params in [
        ("text.layer", {"handle": unknown, "page": 0}),
        (
            "text.extract",
            {
                "handle": unknown,
                "start": pos(0, 0),
                "end": pos(0, 0),
                "cursor": None,
                "budget_ms": 100,
            },
        ),
        ("text.search", search_params(unknown)),
    ]:
        assert error(call(method, params))["code"] == NO_DOCUMENT, method


def test_page_out_of_range_is_invalid_args(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    for page in (5, -1):
        err = error(call("text.layer", {"handle": handle, "page": page}))
        assert err["code"] == INVALID_ARGS
        assert err["details"]["path"] == "params.page"
        assert err["details"]["pages"] == 5


def test_extract_positions_are_checked(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    err = error(
        call(
            "text.extract",
            {
                "handle": handle,
                "start": pos(9, 0),
                "end": pos(0, 0),
                "cursor": None,
                "budget_ms": 100,
            },
        )
    )
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.start.page"
    err = error(
        call(
            "text.extract",
            {
                "handle": handle,
                "start": pos(0, 0),
                "end": pos(0, 99999),
                "cursor": None,
                "budget_ms": 100,
            },
        )
    )
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.end.index"
    err = error(
        call(
            "text.extract",
            {
                "handle": handle,
                "start": pos(0, -1),
                "end": pos(0, 0),
                "cursor": None,
                "budget_ms": 100,
            },
        )
    )
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.start.index"


def test_empty_query_is_invalid_args(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    for query in ("", "   "):
        err = error(call("text.search", search_params(handle, query=query)))
        assert err["code"] == INVALID_ARGS
        assert err["details"]["path"] == "params.query"


def test_invalid_regex_is_invalid_args(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    params = search_params(
        handle, query="(", options={"case_sensitive": False, "whole_word": False, "regex": True}
    )
    err = error(call("text.search", params))
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.query"


def test_over_limit_values_are_refused(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    long_query = "a" * (limits.MAX_QUERY_CHARS + 1)
    err = error(call("text.search", search_params(handle, query=long_query)))
    assert err["code"] == INVALID_ARGS
    assert err["details"]["path"] == "params.query"
    assert err["details"]["max_chars"] == limits.MAX_QUERY_CHARS
    for budget in (limits.MIN_BUDGET_MS - 1, limits.MAX_BUDGET_MS + 1):
        err = error(call("text.search", search_params(handle, budget_ms=budget)))
        assert err["code"] == INVALID_ARGS
        assert err["details"]["path"] == "params.budget_ms"
        err = error(
            call(
                "text.extract",
                {
                    "handle": handle,
                    "start": pos(0, 0),
                    "end": pos(0, 0),
                    "cursor": None,
                    "budget_ms": budget,
                },
            )
        )
        assert err["details"]["path"] == "params.budget_ms"


def test_start_page_out_of_range_is_invalid_args(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    err = error(call("text.search", search_params(handle, start_page=5)))
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.start_page"


def test_unknown_field_is_invalid_args_with_its_path(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    err = error(call("text.search", search_params(handle, extra=1)))
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.extra"
    err = error(call("text.layer", {"handle": handle, "page": "0"}))
    assert err["code"] == INVALID_ARGS and err["details"]["path"] == "params.page"


# -- determinism -------------------------------------------------------------------------------


def test_same_request_twice_gives_identical_replies(web: None, fixture_pdf: Any) -> None:
    handle = open_bytes(fixture_pdf, "text_multipage")
    requests: list[tuple[str, dict[str, Any]]] = [
        ("text.layer", {"handle": handle, "page": 1}),
        (
            "text.extract",
            {
                "handle": handle,
                "start": pos(0, 0),
                "end": pos(2, 5),
                "cursor": None,
                "budget_ms": 1000,
            },
        ),
        ("text.search", search_params(handle, query="heading")),
    ]
    for method, params in requests:
        _, first, first_out = call_raw(method, params)
        _, second, second_out = call_raw(method, params)
        assert first == second, method
        assert first_out == second_out, method
