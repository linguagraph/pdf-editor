"""Character-level text index for one page: hit testing, selection ranges, quads, copy text.

Everything here is headless and engine-neutral: it works on a :class:`TextPage`.
"""

from __future__ import annotations

import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

from pdfeditor.core.session import DocumentSession
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.text import TextPage

LINE_SEP = "\n"


@dataclass(frozen=True, slots=True)
class IndexedChar:
    c: str
    bbox: Rect
    line: int  # global line number on the page


class PageTextIndex:
    """Chars of a page in reading order, plus ``text`` where ``text[i] == chars[i].c``.

    Line breaks are represented by synthetic separator chars (``LINE_SEP``) whose bbox is empty,
    so offsets in ``text`` map 1:1 to ``chars`` and regex matches can span lines.
    """

    def __init__(self, page: TextPage) -> None:
        self.page_index = page.page_index
        chars: list[IndexedChar] = []
        line_rects: list[Rect] = []
        line_no = 0
        for block in page.blocks:
            if block.is_image:
                continue
            for line in block.lines:
                line_chars = [ch for span in line.spans for ch in span.chars]
                if not line_chars:
                    continue
                if chars:
                    chars.append(IndexedChar(LINE_SEP, Rect(0, 0, 0, 0), line_no - 1))
                for ch in line_chars:
                    chars.append(IndexedChar(ch.c, ch.bbox, line_no))
                line_rects.append(line.bbox)
                line_no += 1
        self.chars = chars
        self.line_rects = line_rects
        self.text = "".join(c.c for c in chars)

    def __len__(self) -> int:
        return len(self.chars)

    # -- hit testing ----------------------------------------------------------------------
    def hit_test(self, p: Point, tolerance: float = 4.0) -> int | None:
        """Caret index at ``p`` if ``p`` is on (or within ``tolerance`` of) a text line."""
        # Unweighted box distance: the tolerance applies equally in x and y (line gaps).
        near = [
            (_distance(r, p), i)
            for i, r in enumerate(self.line_rects)
            if _box_gap(r, p) <= tolerance
        ]
        if not near:
            return None
        return self._nearest_on_line(min(near)[1], p)

    def nearest(self, p: Point) -> int | None:
        """Like :meth:`hit_test` but never gives up: used while dragging a selection."""
        if not self.line_rects:
            return None
        line = min(
            range(len(self.line_rects)),
            key=lambda i: _distance(self.line_rects[i], p),
        )
        return self._nearest_on_line(line, p)

    def _nearest_on_line(self, line: int, p: Point) -> int:
        candidates = [i for i, c in enumerate(self.chars) if c.line == line and c.c != LINE_SEP]
        for i in candidates:
            b = self.chars[i].bbox
            if b.x0 <= p.x <= b.x1:
                # left half selects before the char, right half after it
                return i if p.x < (b.x0 + b.x1) / 2 else i + 1
        first, last = candidates[0], candidates[-1]
        if p.x < self.chars[first].bbox.x0:
            return first
        if p.x > self.chars[last].bbox.x1:
            return last + 1
        return min(candidates, key=lambda i: abs(self.chars[i].bbox.center.x - p.x))

    # -- ranges ---------------------------------------------------------------------------
    def word_range(self, i: int) -> tuple[int, int]:
        i = min(max(i, 0), len(self.chars) - 1)
        if not _is_word_char(self.text[i]):
            return i, i + 1
        start = i
        while start > 0 and _is_word_char(self.text[start - 1]):
            start -= 1
        end = i + 1
        while end < len(self.text) and _is_word_char(self.text[end]):
            end += 1
        return start, end

    def line_range(self, i: int) -> tuple[int, int]:
        i = min(max(i, 0), len(self.chars) - 1)
        line = self.chars[i].line
        idx = [k for k, c in enumerate(self.chars) if c.line == line and c.c != LINE_SEP]
        return idx[0], idx[-1] + 1

    def rects(self, start: int, end: int) -> list[Rect]:
        """One rectangle per line covering chars ``[start, end)``."""
        out: list[Rect] = []
        current: Rect | None = None
        current_line = -1
        for c in self.chars[max(0, start) : max(0, end)]:
            if c.c == LINE_SEP or c.bbox.is_empty:
                continue
            if c.line != current_line:
                if current is not None:
                    out.append(current)
                current, current_line = c.bbox, c.line
            else:
                assert current is not None
                current = current.union(c.bbox)
        if current is not None:
            out.append(current)
        return out

    def extract(self, start: int, end: int) -> str:
        return self.text[max(0, start) : max(0, end)]


def _box_gap(r: Rect, p: Point) -> float:
    return max(r.x0 - p.x, 0.0, p.x - r.x1, r.y0 - p.y, p.y - r.y1)


def _distance(r: Rect, p: Point) -> float:
    """0 inside ``r``; otherwise weighted so the vertically closest line wins."""
    dx = max(r.x0 - p.x, 0.0, p.x - r.x1)
    dy = max(r.y0 - p.y, 0.0, p.y - r.y1)
    return dy * 4 + dx


def _is_word_char(c: str) -> bool:
    return c.isalnum() or c == "_" or unicodedata.category(c) == "Mn"


class TextIndexCache:
    """Per-session cache of page indexes, keyed by page revision. Thread-safe.

    Least-recently-used pages are dropped beyond ``max_pages``: a search or copy across a
    huge document would otherwise keep every page's character boxes in memory.
    """

    def __init__(self, session: DocumentSession, max_pages: int = 300) -> None:
        self.session = session
        self.max_pages = max_pages
        self._cache: OrderedDict[int, tuple[int, PageTextIndex]] = OrderedDict()
        self._mutex = threading.Lock()

    def get(self, page_index: int) -> PageTextIndex:
        page = self.session.document.page(page_index)
        rev = page.revision
        with self._mutex:
            hit = self._cache.get(page_index)
            if hit is not None and hit[0] == rev:
                self._cache.move_to_end(page_index)
                return hit[1]
        with self.session.lock:
            index = PageTextIndex(page.text_page(with_chars=True))
        with self._mutex:
            self._cache[page_index] = (rev, index)
            self._cache.move_to_end(page_index)
            while len(self._cache) > self.max_pages:
                self._cache.popitem(last=False)
        return index

    def __len__(self) -> int:
        return len(self._cache)

    def clear(self) -> None:
        with self._mutex:
            self._cache.clear()


@dataclass(frozen=True, slots=True, order=True)
class TextPos:
    """A caret position: before char ``index`` on ``page``."""

    page: int
    index: int


@dataclass(frozen=True, slots=True)
class TextSelection:
    anchor: TextPos
    focus: TextPos

    @property
    def start(self) -> TextPos:
        return min(self.anchor, self.focus)

    @property
    def end(self) -> TextPos:
        return max(self.anchor, self.focus)

    @property
    def is_empty(self) -> bool:
        return self.anchor == self.focus

    def page_spans(self, cache: TextIndexCache) -> list[tuple[int, int, int]]:
        """``(page, start, end)`` char ranges covered on each page."""
        start, end = self.start, self.end
        out = []
        for page in range(start.page, end.page + 1):
            s = start.index if page == start.page else 0
            e = end.index if page == end.page else len(cache.get(page))
            if e > s:
                out.append((page, s, e))
        return out

    def covers(self, page: int) -> bool:
        return not self.is_empty and self.start.page <= page <= self.end.page

    def page_rects(self, cache: TextIndexCache, page: int) -> list[Rect]:
        """Highlight rectangles on one page (only that page's text is extracted)."""
        if not self.covers(page):
            return []
        start, end = self.start, self.end
        s = start.index if page == start.page else 0
        e = end.index if page == end.page else len(cache.get(page))
        return cache.get(page).rects(s, e) if e > s else []

    def text(self, cache: TextIndexCache) -> str:
        return LINE_SEP.join(cache.get(p).extract(s, e) for p, s, e in self.page_spans(cache))

    def rects(self, cache: TextIndexCache) -> dict[int, list[Rect]]:
        return {p: cache.get(p).rects(s, e) for p, s, e in self.page_spans(cache)}
