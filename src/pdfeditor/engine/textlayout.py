"""Editable paragraphs from extracted text blocks (engine-neutral).

Extraction engines group text by proximity, so a two-column list ("icon  label   icon  label")
often comes back as one block whose lines run across both columns, the gutter stored as a run
of spaces. Editing such a block would reflow both columns into one box. Here lines are cut at
wide gaps, and blocks laid out like a table (pieces side by side on one row) become one
paragraph per piece. Ordinary paragraphs pass through unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from pdfeditor.model.geometry import Rect
from pdfeditor.model.text import Block, Char, Line, Span

COLUMN_GAP_EM = 2.5  # a horizontal gap this many font sizes wide separates columns


def _union(rects: Sequence[Rect]) -> Rect:
    return Rect(
        min(r.x0 for r in rects),
        min(r.y0 for r in rects),
        max(r.x1 for r in rects),
        max(r.y1 for r in rects),
    )


def _span_of(span: Span, chars: Sequence[Char]) -> Span:
    return Span(
        text="".join(c.c for c in chars),
        bbox=_union([c.bbox for c in chars]),
        font=span.font,
        size=span.size,
        color=span.color,
        flags=span.flags,
        origin=chars[0].origin,
        chars=tuple(chars),
    )


def split_line(line: Line) -> list[Line]:
    """Cut a line where the gap between visible characters is wider than the column gap.

    Leading/trailing blanks of each piece are dropped; lines without per-char boxes or with
    rotated text are returned as they are.
    """
    if abs(line.direction[0] - 1) > 1e-3 or not all(s.chars for s in line.spans if s.text):
        return [line]
    pieces: list[list[tuple[Span, list[Char]]]] = [[]]
    last_x1: float | None = None
    for span in line.spans:
        for ch in span.chars:
            if ch.c.isspace():
                continue
            if last_x1 is not None and ch.bbox.x0 - last_x1 > COLUMN_GAP_EM * span.size:
                pieces.append([])
            last_x1 = ch.bbox.x1
            piece = pieces[-1]
            if piece and piece[-1][0] is span:
                piece[-1][1].append(ch)
            else:
                piece.append((span, [ch]))
    if len(pieces) == 1 and line.text == line.text.strip():
        return [line]
    out: list[Line] = []
    for piece in pieces:
        if not piece:
            continue
        # keep the blanks *between* visible characters of the piece (word spaces)
        spans = [_with_inner_blanks(span, chars) for span, chars in piece]
        out.append(Line(tuple(spans), _union([s.bbox for s in spans]), line.direction))
    return out


def _with_inner_blanks(span: Span, visible: list[Char]) -> Span:
    first, last = span.chars.index(visible[0]), span.chars.index(visible[-1])
    return _span_of(span, span.chars[first : last + 1])


def _rows_overlap(a: Rect, b: Rect) -> bool:
    return min(a.y1, b.y1) - max(a.y0, b.y0) > 0.5 * min(a.height, b.height)


def is_tabular(lines: Sequence[Line]) -> bool:
    """Pieces sit side by side on the same row: a list/table, not running text."""
    return any(
        _rows_overlap(a.bbox, b.bbox) and (a.bbox.x1 <= b.bbox.x0 or b.bbox.x1 <= a.bbox.x0)
        for i, a in enumerate(lines)
        for b in lines[i + 1 :]
    )


def editable_blocks(blocks: Sequence[Block]) -> list[Block]:
    """Blocks to offer for editing, in reading order (top to bottom, then left to right)."""
    out: list[Block] = []
    for block in blocks:
        if block.is_image or not block.text.strip():
            continue
        pieces = [piece for line in block.lines for piece in split_line(line)]
        if len(pieces) == len(block.lines) and not is_tabular(pieces):
            # running text: MuPDF's paragraph, with blank margins trimmed off its lines
            if pieces != list(block.lines):
                block = Block(_union([p.bbox for p in pieces]), tuple(pieces))
            out.append(block)
            continue
        out.extend(Block(p.bbox, (p,)) for p in pieces if p.text.strip())
    return sorted(out, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))
