"""Structured page text: blocks > lines > spans > chars, with positions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag

from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Quad, Rect


class FontFlags(IntFlag):
    SUPERSCRIPT = 1
    ITALIC = 2
    SERIF = 4
    MONOSPACED = 8
    BOLD = 16


@dataclass(frozen=True, slots=True)
class Char:
    c: str
    bbox: Rect
    origin: Point


@dataclass(frozen=True, slots=True)
class Span:
    text: str
    bbox: Rect
    font: str
    size: float
    color: Color
    flags: FontFlags
    origin: Point
    chars: tuple[Char, ...] = ()

    @property
    def is_bold(self) -> bool:
        return FontFlags.BOLD in self.flags or "bold" in self.font.lower()

    @property
    def is_italic(self) -> bool:
        name = self.font.lower()
        return FontFlags.ITALIC in self.flags or "italic" in name or "oblique" in name


@dataclass(frozen=True, slots=True)
class Line:
    spans: tuple[Span, ...]
    bbox: Rect
    direction: tuple[float, float] = (1.0, 0.0)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass(frozen=True, slots=True)
class Block:
    bbox: Rect
    lines: tuple[Line, ...] = ()
    is_image: bool = False

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True, slots=True)
class TextPage:
    page_index: int
    width: float
    height: float
    blocks: tuple[Block, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if not b.is_image and b.lines)

    def chars(self) -> list[Char]:
        """All chars in reading (block/line/span) order."""
        return [ch for b in self.blocks for line in b.lines for s in line.spans for ch in s.chars]


@dataclass(frozen=True, slots=True)
class SearchHit:
    page_index: int
    quads: tuple[Quad, ...]
    text: str = ""
    context: str = ""

    @property
    def rect(self) -> Rect:
        r = self.quads[0].rect
        for q in self.quads[1:]:
            r = r.union(q.rect)
        return r


@dataclass(frozen=True, slots=True)
class SearchOptions:
    case_sensitive: bool = False
    whole_word: bool = False
    regex: bool = False
    page_indices: tuple[int, ...] | None = None  # None = all pages


@dataclass(frozen=True, slots=True)
class TableData:
    """A table detected on a page: cell texts row by row (None = merged/empty cell)."""

    page_index: int
    bbox: Rect
    rows: tuple[tuple[str | None, ...], ...]

    @property
    def size(self) -> tuple[int, int]:
        return len(self.rows), max((len(r) for r in self.rows), default=0)
