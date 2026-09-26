"""Document text search: plain, whole-word, case-sensitive and regex; matches may span lines."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.model.geometry import Quad
from pdfeditor.model.text import SearchHit, SearchOptions
from pdfeditor.services.text import PageTextIndex, TextIndexCache

CONTEXT_CHARS = 40


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    options: SearchOptions = field(default_factory=SearchOptions)


def compile_query(query: SearchQuery) -> re.Pattern[str]:
    """Build the regex for a query. Raises ValueError for an empty query or invalid regex."""
    text = query.text
    if not text.strip():
        raise ValueError("empty search")
    opts = query.options
    # Outside regex mode, any run of whitespace matches any whitespace, including line breaks.
    words = (re.escape(part) for part in text.split())
    pattern = text if opts.regex else r"\s+".join(words)
    if opts.whole_word:
        pattern = rf"(?<!\w)(?:{pattern})(?!\w)"
    flags = 0 if opts.case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc


def search_index(index: PageTextIndex, pattern: re.Pattern[str]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    text = index.text
    for m in pattern.finditer(text):
        if m.end() == m.start():
            continue  # zero-width regex matches can't be highlighted
        rects = index.rects(m.start(), m.end())
        if not rects:
            continue
        before = text[max(0, m.start() - CONTEXT_CHARS) : m.start()]
        after = text[m.end() : m.end() + CONTEXT_CHARS]
        context = " ".join(f"{before}[{m.group(0)}]{after}".split())
        hits.append(
            SearchHit(
                page_index=index.page_index,
                quads=tuple(Quad.from_rect(r) for r in rects),
                text=m.group(0),
                context=context,
            )
        )
    return hits


def search_document(
    cache: TextIndexCache,
    query: SearchQuery,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
    on_page: Callable[[int, list[SearchHit]], None] | None = None,
    start_page: int = 0,
) -> list[SearchHit]:
    """Search pages in order starting at ``start_page`` (wrapping around).

    ``on_page`` is called after each page so results can stream into the UI. Each page takes the
    session lock separately, so rendering stays responsive during a long search.
    """
    pattern = compile_query(query)
    count = cache.session.page_count
    pages: Iterable[int]
    if query.options.page_indices is not None:
        pages = [p for p in query.options.page_indices if 0 <= p < count]
    else:
        pages = [(start_page + i) % count for i in range(count)] if count else []
    pages = list(pages)
    results: list[SearchHit] = []
    for done, page in enumerate(pages, 1):
        if token is not None:
            token.check()
        hits = search_index(cache.get(page), pattern)
        results.extend(hits)
        if on_page is not None:
            on_page(page, hits)
        progress(done, len(pages))
    return results
