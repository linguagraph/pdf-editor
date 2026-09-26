"""Page layout math for the document view (pure, in points)."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from pdfeditor.model.geometry import Matrix, Rect

PAGE_GAP = 12.0  # points between pages and around the layout


class LayoutMode(Enum):
    CONTINUOUS = "continuous"  # one column, scrolling
    SINGLE = "single"  # one page at a time
    TWO_UP = "two_up"  # two columns, scrolling
    TWO_UP_COVER = "two_up_cover"  # like TWO_UP but page 1 alone on the right


def rotated_size(width: float, height: float, rotation: int) -> tuple[float, float]:
    return (height, width) if rotation % 180 else (width, height)


def view_matrix(page_rect: Rect, rotation: int, scale: float) -> Matrix:
    """Map page space to device pixels: rotate by the view rotation, move to origin, scale."""
    rot = Matrix.rotate(rotation % 360)
    bbox = page_rect.transform(rot)
    return rot @ Matrix.translate(-bbox.x0, -bbox.y0) @ Matrix.scale(scale)


def compute_layout(
    sizes: Sequence[tuple[float, float]],
    mode: LayoutMode,
    rotation: int = 0,
    gap: float = PAGE_GAP,
) -> list[Rect]:
    """Scene rectangles for each page, given each page's unrotated (width, height).

    Pages are centered in their column; in two-up modes, each row is as tall as its tallest page.
    SINGLE mode stacks pages like CONTINUOUS — the view shows only the current one.
    """
    dims = [rotated_size(w, h, rotation) for w, h in sizes]
    if not dims:
        return []
    if mode in (LayoutMode.CONTINUOUS, LayoutMode.SINGLE):
        col_w = max(w for w, _ in dims)
        rects: list[Rect] = []
        y = gap
        for w, h in dims:
            x = gap + (col_w - w) / 2
            rects.append(Rect(x, y, x + w, y + h))
            y += h + gap
        return rects

    # Two columns. Rows are lists of page indices (None = empty slot).
    indices: list[int | None] = list(range(len(dims)))
    if mode is LayoutMode.TWO_UP_COVER:
        indices.insert(0, None)
    rows = [indices[i : i + 2] for i in range(0, len(indices), 2)]
    col_w = max(w for w, _ in dims)
    out: list[Rect | None] = [None] * len(dims)
    y = gap
    for row in rows:
        row_h = max(dims[i][1] for i in row if i is not None)
        for col, idx in enumerate(row):
            if idx is None:
                continue
            w, h = dims[idx]
            # the left page hugs the spine; the right one starts just after it
            x = gap + col_w - w if col == 0 else gap + col_w + gap
            top = y + (row_h - h) / 2
            out[idx] = Rect(x, top, x + w, top + h)
        y += row_h + gap
    return [r for r in out if r is not None]


def layout_bounds(rects: Sequence[Rect], gap: float = PAGE_GAP) -> Rect:
    if not rects:
        return Rect(0, 0, 0, 0)
    r = rects[0]
    for other in rects[1:]:
        r = r.union(other)
    return Rect(0, 0, r.x1 + gap, r.y1 + gap)


def page_at(rects: Sequence[Rect], y: float) -> int:
    """Index of the page whose vertical span contains (or is nearest to) scene ``y``."""
    if not rects:
        return -1
    best, best_dist = 0, float("inf")
    for i, r in enumerate(rects):
        if r.y0 <= y <= r.y1:
            return i
        dist = min(abs(r.y0 - y), abs(r.y1 - y))
        if dist < best_dist:
            best, best_dist = i, dist
    return best
