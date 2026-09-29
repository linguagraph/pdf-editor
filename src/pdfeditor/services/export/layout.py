"""Page-faithful structure for Word export ("keep page layout").

Instead of reflowing text into one column, every page keeps its geometry: each text block is a
frame at its own place on the page, detected tables become real tables floating at their place
(grid, merged cells, borders and shading taken from the PDF), pictures are anchored where they
were, and whatever else is drawn with vector graphics (diagrams, rules, rotated text) becomes
one picture behind the text. The text stays editable; the page looks like the PDF.
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import re
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from fontTools.ttLib import TTFont
from PIL import Image

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import ColorMode, Document, Page, RenderRequest
from pdfeditor.engine.textlayout import editable_blocks, split_line
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.model.objects import ObjectType
from pdfeditor.model.text import Char, Line, Span, TableData
from pdfeditor.services.export.fonts import STANDARD_TWIN, metric_twin
from pdfeditor.services.export.structure import (
    Picture,
    Run,
    append_run,
    base_font_name,
    real_font_names,
    span_run,
)

HORIZONTAL = 1e-3
SAME_INDENT_PT = 2.0
# the extras on letters may differ this much (in font sizes) before the measured font is
# taken for a different one than the PDF shows
SPACING_SPREAD_EM = 0.05
MAX_SPACING_EM = 0.5  # more letter spacing than this is a measuring error
MIN_SPACING_PT = 0.02  # less isn't worth a Word attribute
LIMIT_CLEARANCE_PT = 2.5  # Word keeps text this far off a cell's edge (its border)
FULL_LINE = 0.9  # share of a cell's width a line must fill to be squeezed
GRID_SNAP_PT = 1.5  # cell edges closer than this are the same grid line
TABLE_DPI = 288  # borders are often hairlines: look at them closely
DARK = 150  # luma below this on a cell edge is a drawn border
WHITE = 245  # a cell lighter than this has no shading


@dataclass
class TextLine:
    runs: list[Run]
    indent: float  # points from the frame's left edge
    full: bool = False  # reaches the frame's right edge (justified text)


@dataclass
class Frame:
    """A text block at its place: ``rect`` is where its lines sit on the page."""

    rect: Rect
    lines: list[TextLine]
    pitch: float  # baseline-to-baseline distance (points)
    baseline: float  # of the first line
    justified: bool = False
    # every line fills its cell and one can't fit in Word: squeeze the cell's text to fit
    # (Word's "fit text" stretches short lines too, so it's only for full ones)
    tight: bool = False


@dataclass
class Border:
    width: float  # points
    color: str  # RRGGBB


@dataclass
class Cell:
    row: int
    col: int
    rows: int  # rows and columns spanned
    cols: int
    rect: Rect
    content: Frame | None = None
    direction: str = ""  # vertical text: "btLr" (bottom to top) or "tbRl" (top to bottom)
    shading: str | None = None  # RRGGBB
    borders: dict[str, Border | None] = field(default_factory=dict)  # top/left/bottom/right


@dataclass
class LayoutTable:
    xs: list[float]  # grid lines (points, page space)
    ys: list[float]
    cells: list[Cell]
    # False: not a table in the PDF, just a box for vertical text (no borders of its own)
    ruled: bool = True

    @property
    def rect(self) -> Rect:
        return Rect(self.xs[0], self.ys[0], self.xs[-1], self.ys[-1])


@dataclass
class LayoutPage:
    width: float
    height: float
    frames: list[Frame] = field(default_factory=list)
    tables: list[LayoutTable] = field(default_factory=list)
    pictures: list[tuple[Rect, Picture]] = field(default_factory=list)
    background: Picture | None = None  # vector art (and text that can't be placed), full page


def _render(page: Page, area: Rect | None, dpi: int, alpha: bool = False) -> Image.Image:
    mode, color = ("RGBA", ColorMode.RGBA) if alpha else ("RGB", ColorMode.RGB)
    shot = page.render(
        RenderRequest(matrix=Matrix.scale(dpi / 72), clip=area, color=color, annotations=False)
    )
    return Image.frombytes(mode, (shot.width, shot.height), shot.samples, "raw", mode, shot.stride)


def _png(img: Image.Image, width: float, height: float) -> Picture:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return Picture(buf.getvalue(), width, height)


def _line_runs(line: Line, fonts: dict[str, str]) -> list[Run]:
    runs: list[Run] = []
    for span in line.spans:
        append_run(runs, span_run(span, fonts))
    if runs:
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()
    return [r for r in runs if r.text]


Measure = Callable[[str, str, float], float]  # (text, base-14 font, size) -> points


class _Widths:
    """Advance widths from the page's embedded font programs, or, for fonts Word shows as
    Arial/Times New Roman/Courier New (see ``metric_twin``), from ``measure`` with the
    base-14 twin's metrics (None: can't be measured)."""

    def __init__(self, page: Page, measure: Measure | None = None) -> None:
        self._page = page
        self._measure = measure
        self._fonts: dict[str, tuple[dict[int, str], Any, int] | None] = {}

    def _font(self, name: str) -> tuple[dict[int, str], Any, int] | None:
        if name not in self._fonts:
            program = self._page.font_program(name)
            try:
                font = TTFont(io.BytesIO(program), lazy=True) if program else None
                cmap = font.getBestCmap() if font else None
                self._fonts[name] = (
                    (cmap, font["hmtx"], font["head"].unitsPerEm) if font and cmap else None
                )
            except Exception:
                self._fonts[name] = None
        return self._fonts[name]

    def advances(self, span: Span) -> list[float] | None:
        """Each character's natural advance (points), in the font Word will show."""
        font = self._font(span.font)
        if font is not None:
            cmap, hmtx, upm = font
            out = []
            for ch in span.text:
                glyph = cmap.get(ord(ch))
                if glyph is None:
                    return None
                out.append(hmtx[glyph][0] * span.size / upm)
            return out
        twin = metric_twin(base_font_name(span.font))
        if twin is None or self._measure is None:
            return None
        run = span_run(span, {})
        name = STANDARD_TWIN[twin][run.bold + 2 * run.italic]
        return [self._measure(ch, name, span.size) for ch in span.text]


def _fit_spacing(
    line: Line, runs: list[Run], widths: _Widths, justified: bool, limit: float | None = None
) -> bool:
    """Reproduce the PDF's letter and word spacing (Tc/Tw, tracking, condensed text).

    Each character's advance on the page is compared with its natural advance: the typical
    extra on letters is the letter spacing, the extra on spaces adds the word spacing, which
    gets runs of its own. Where the extras disagree, the measured font isn't what the PDF
    shows and nothing is changed. ``limit`` is a right edge the line must stay clear of (a
    table cell's); the spacing is tightened to keep it. Returns whether the line then fits
    (always, without a limit).
    """
    room = None if limit is None else limit - line.bbox.x0 - LIMIT_CLEARANCE_PT
    chars: list[tuple[Char, float]] = []
    for span in line.spans:
        advances = widths.advances(span) if span.chars else None
        if advances is None or len(advances) != len(span.chars):
            return room is None or line.bbox.width <= room
        chars += zip(span.chars, advances, strict=True)
    while chars and chars[0][0].c.isspace():
        chars.pop(0)
    while chars and chars[-1][0].c.isspace():
        chars.pop()
    size = max((s.size for s in line.spans), default=0.0)
    letters: list[float] = []
    spaces: list[float] = []
    for (ch, advance), (following, _) in itertools.pairwise(chars):
        extra = following.bbox.x0 - ch.bbox.x0 - advance
        (spaces if ch.c.isspace() else letters).append(extra)
    if not letters or not size:
        return room is None or line.bbox.width <= room
    tc = statistics.median(letters)
    spread = statistics.median(abs(e - tc) for e in letters)
    if spread > SPACING_SPREAD_EM * size or abs(tc) > MAX_SPACING_EM * size:
        return room is None or line.bbox.width <= room  # not the font the PDF shows
    # justified lines: Word stretches the spaces itself
    tw = statistics.median(spaces) - tc if spaces and not justified else 0.0
    natural = sum(advance for _, advance in chars)
    gaps = len(chars) - 1
    width = natural + tc * gaps + tw * len(spaces)
    if room is not None and width > room:
        tc -= (width - room) / gaps
        width = room
    if abs(tw) < MIN_SPACING_PT:
        for run in runs:
            run.spacing = tc if abs(tc) >= MIN_SPACING_PT else 0.0
    else:
        split: list[Run] = []
        for run in runs:
            for part in re.split(r"(\s+)", run.text):
                if part:
                    extra = tc + tw if part.isspace() else tc
                    split.append(dataclasses.replace(run, text=part, spacing=extra))
        runs[:] = split
    return room is None or width <= room + 0.01


def _is_horizontal(line: Line) -> bool:
    return abs(line.direction[1]) < HORIZONTAL and line.direction[0] > 0


def _center_in(line: Line, rect: Rect) -> bool:
    cx, cy = (line.bbox.x0 + line.bbox.x1) / 2, (line.bbox.y0 + line.bbox.y1) / 2
    return rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1


def _frame(
    rows: Sequence[Sequence[Line]],
    fonts: dict[str, str],
    widths: _Widths,
    limit: float | None = None,
) -> Frame:
    """Text laid out in rows (each one or more pieces side by side, left to right); ``limit``
    is the right edge of the cell the text must fit in."""
    boxes = [
        Rect(r[0].bbox.x0, min(p.bbox.y0 for p in r), r[-1].bbox.x1, max(p.bbox.y1 for p in r))
        for r in rows
    ]
    x0 = min(b.x0 for b in boxes)
    rect = Rect(x0, min(b.y0 for b in boxes), max(b.x1 for b in boxes), max(b.y1 for b in boxes))
    baselines = [r[0].spans[0].origin.y for r in rows]
    steps = [b - a for a, b in itertools.pairwise(baselines) if b > a]
    pitch = statistics.median(steps) if steps else boxes[0].height
    # running text set flush on both sides: every line but the last fills the width
    justified = len(rows) > 2 and all(b.x1 >= rect.x1 - SAME_INDENT_PT for b in boxes[:-1])
    if justified and limit is None:
        # Word stretches justified lines to the frame's width, so the frame gets the text's
        # exact width and the lines are fitted into it
        limit = rect.x1 + LIMIT_CLEARANCE_PT
    lines: list[TextLine] = []
    tight = False
    for row, box in zip(rows, boxes, strict=True):
        runs: list[Run] = []
        for n, piece in enumerate(row):
            piece_runs = _line_runs(piece, fonts)
            if len(row) == 1:
                tight |= not _fit_spacing(piece, piece_runs, widths, justified, limit)
            elif limit is not None and box.x1 > limit - LIMIT_CLEARANCE_PT:
                tight = True
            if n and runs and piece_runs:
                runs[-1].text += " "  # a wide gap inside one cell or paragraph: a word space
            runs.extend(piece_runs)
        if runs:
            lines.append(TextLine(runs, box.x0 - x0, box.x1 >= rect.x1 - SAME_INDENT_PT))
    if limit is not None:
        tight = tight and all(b.width >= FULL_LINE * (limit - b.x0) for b in boxes)
    return Frame(rect, lines, pitch, baselines[0], justified, tight)


def _rows_of(pieces: Sequence[Line]) -> list[list[Line]]:
    """Pieces grouped into rows by baseline, top to bottom, each row left to right."""
    rows: list[list[Line]] = []
    for piece in sorted(pieces, key=lambda p: (p.spans[0].origin.y, p.bbox.x0)):
        base = piece.spans[0].origin.y
        if rows and abs(rows[-1][0].spans[0].origin.y - base) < piece.spans[0].size * 0.5:
            rows[-1].append(piece)
        else:
            rows.append([piece])
    return [sorted(r, key=lambda p: p.bbox.x0) for r in rows]


def _snap(values: Sequence[float]) -> list[float]:
    grid: list[float] = []
    for v in sorted(values):
        if grid and v - grid[-1] <= GRID_SNAP_PT:
            continue
        grid.append(v)
    return grid


def _nearest(grid: Sequence[float], v: float) -> int:
    return min(range(len(grid)), key=lambda i: abs(grid[i] - v))


def _cut(line: Line, cells: Sequence[Cell]) -> list[tuple[Cell, Line]]:
    """A horizontal line cut where it crosses from one cell into the next: extraction often
    runs neighbouring cells' text together (\"79,88 0,380 57,7\")."""
    parts: list[tuple[Cell, list[tuple[Span, list[Char]]]]] = []
    for span in line.spans:
        for ch in span.chars:
            cx, cy = (ch.bbox.x0 + ch.bbox.x1) / 2, (ch.bbox.y0 + ch.bbox.y1) / 2
            owner = next(
                (c for c in cells if c.rect.x0 <= cx <= c.rect.x1 and c.rect.y0 <= cy <= c.rect.y1),
                None,
            )
            if owner is None:
                continue
            if not parts or parts[-1][0] is not owner:
                parts.append((owner, []))
            spans = parts[-1][1]
            if spans and spans[-1][0] is span:
                spans[-1][1].append(ch)
            else:
                spans.append((span, [ch]))
    out: list[tuple[Cell, Line]] = []
    for owner, spans in parts:
        chars = [(s, c) for s, cs in spans for c in cs]
        while chars and chars[0][1].c.isspace():
            chars.pop(0)
        while chars and chars[-1][1].c.isspace():
            chars.pop()
        if not chars:
            continue
        pieces: list[Span] = []
        for span, group in itertools.groupby(chars, key=lambda sc: sc[0]):
            cs = [c for _, c in group]
            box = Rect(
                cs[0].bbox.x0,
                min(c.bbox.y0 for c in cs),
                cs[-1].bbox.x1,
                max(c.bbox.y1 for c in cs),
            )
            pieces.append(
                Span(
                    "".join(c.c for c in cs),
                    box,
                    span.font,
                    span.size,
                    span.color,
                    span.flags,
                    cs[0].origin,
                    tuple(cs),
                )
            )
        box = Rect(
            pieces[0].bbox.x0,
            min(p.bbox.y0 for p in pieces),
            pieces[-1].bbox.x1,
            max(p.bbox.y1 for p in pieces),
        )
        out.append((owner, Line(tuple(pieces), box, line.direction)))
    return out


def _crossed(area: Rect, pieces: Sequence[Line]) -> bool:
    """Whether a line of text runs across the area's left or right edge: text in a table
    stays in its cells, text flowing through a box means the box is inline decoration."""
    for p in pieces:
        if not _is_horizontal(p) or p.bbox.y1 <= area.y0 or p.bbox.y0 >= area.y1:
            continue
        for edge in (area.x0, area.x1):
            if p.bbox.x0 < edge - GRID_SNAP_PT and p.bbox.x1 > edge + GRID_SNAP_PT:
                return True
    return False


def _table(
    table: TableData, pieces: Sequence[Line], fonts: dict[str, str], widths: _Widths
) -> LayoutTable | None:
    """The table's grid and cells with their text; None if the engine gave no cell boxes."""
    boxes = list(dict.fromkeys(c for row in table.cells for c in row if c is not None))
    if not boxes:
        return None
    xs = _snap([b.x0 for b in boxes] + [b.x1 for b in boxes])
    ys = _snap([b.y0 for b in boxes] + [b.y1 for b in boxes])
    cells: list[Cell] = []
    for box in boxes:
        c0, c1 = _nearest(xs, box.x0), _nearest(xs, box.x1)
        r0, r1 = _nearest(ys, box.y0), _nearest(ys, box.y1)
        if c1 > c0 and r1 > r0:
            rect = Rect(xs[c0], ys[r0], xs[c1], ys[r1])
            cells.append(Cell(r0, c0, r1 - r0, c1 - c0, rect))
    if len(cells) < 2 or _crossed(Rect(xs[0], ys[0], xs[-1], ys[-1]), pieces):
        return None  # a box around a few words (inline code, a button), not a table
    flat: dict[int, list[Line]] = {}
    for piece in pieces:
        if _is_horizontal(piece) and piece.spans[0].chars:
            for owner, part in _cut(piece, cells):
                flat.setdefault(id(owner), []).append(part)
    for cell in cells:
        inside = [p for p in pieces if not _is_horizontal(p) and _center_in(p, cell.rect)]
        if id(cell) in flat:
            cell.content = _frame(_rows_of(flat[id(cell)]), fonts, widths, cell.rect.x1)
        elif inside and (vertical := _vertical(inside, cell.rect, fonts)) is not None:
            cell.direction, cell.content = vertical
    return LayoutTable(xs, ys, cells)


def _vertical(
    pieces: Sequence[Line], rect: Rect, fonts: dict[str, str]
) -> tuple[str, Frame] | None:
    """Text running up or down the page, as Word's vertical text direction and lines."""
    up = [p for p in pieces if abs(p.direction[0]) < HORIZONTAL and p.direction[1] < 0]
    down = [p for p in pieces if abs(p.direction[0]) < HORIZONTAL and p.direction[1] > 0]
    if not up and not down:
        return None  # upside down or slanted: Word can't write it, it stays in the picture
    # bottom-to-top lines follow each other left to right; top-to-bottom ones right to left
    direction, chosen = ("btLr", up) if len(up) >= len(down) else ("tbRl", down)
    ordered = sorted(chosen, key=lambda p: p.bbox.x0, reverse=direction == "tbRl")
    lines = [TextLine(runs, 0) for p in ordered if (runs := _line_runs(p, fonts))]
    if not lines:
        return None
    size = max(r.size for ln in lines for r in ln.runs)
    return direction, Frame(rect, lines, size * 1.2, rect.y0 + size)


def _vertical_boxes(pieces: Sequence[Line], fonts: dict[str, str]) -> list[LayoutTable]:
    """Vertical text outside tables, each run of it in a borderless one-cell table (Word only
    writes vertically in table cells and text boxes)."""
    boxes: list[LayoutTable] = []
    columns: list[list[Line]] = []
    for piece in sorted(pieces, key=lambda p: p.bbox.x0):
        near = next(
            (
                c for c in columns
                if piece.bbox.x0 - c[-1].bbox.x1 < piece.spans[0].size
                and min(piece.bbox.y1, c[-1].bbox.y1) > max(piece.bbox.y0, c[-1].bbox.y0)
            ),
            None,
        )  # fmt: skip
        if near is None:
            columns.append([piece])
        else:
            near.append(piece)
    for column in columns:
        rect = Rect(
            min(p.bbox.x0 for p in column) - 1,
            min(p.bbox.y0 for p in column) - 1,
            max(p.bbox.x1 for p in column) + 1,
            max(p.bbox.y1 for p in column) + 1,
        )
        vertical = _vertical(column, rect, fonts)
        if vertical is not None:
            cell = Cell(0, 0, 1, 1, rect, vertical[1], vertical[0])
            boxes.append(LayoutTable([rect.x0, rect.x1], [rect.y0, rect.y1], [cell], ruled=False))
    return boxes


def _luma(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _hex(rgb: tuple[int, int, int]) -> str:
    return "".join(f"{v:02X}" for v in rgb)


def _dominant(img: Image.Image) -> tuple[int, int, int]:
    colors = img.getcolors(img.width * img.height) or [(1, (255, 255, 255))]
    rgb = max(colors, key=lambda c: c[0])[1]
    return rgb if isinstance(rgb, tuple) else (rgb, rgb, rgb)


def _edge(
    img: Image.Image, scale: float, a: tuple[float, float], b: tuple[float, float]
) -> Border | None:
    """A drawn line from a to b (image pixels), found by scanning across it at a few places."""
    probe = 2.5 * scale  # how far across the edge to look
    horizontal = a[1] == b[1]
    hits: list[tuple[float, tuple[int, int, int]]] = []
    samples = 9
    for k in range(samples):
        t = 0.15 + 0.7 * k / (samples - 1)
        x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        across = [
            (round(x), round(y + d)) if horizontal else (round(x + d), round(y))
            for d in range(-round(probe), round(probe) + 1)
        ]
        pixels = [
            img.getpixel(p) for p in across if 0 <= p[0] < img.width and 0 <= p[1] < img.height
        ]
        dark = [px for px in pixels if isinstance(px, tuple) and _luma(px) < DARK]  # type: ignore[arg-type]
        if dark:
            hits.append((len(dark) / scale, min(dark, key=_luma)))  # type: ignore[arg-type]
    if len(hits) < samples * 0.7:
        return None
    width = statistics.median(w for w, _ in hits)
    color = statistics.median_low([_hex(c) for _, c in hits])
    return Border(max(0.25, min(width, 6.0)), color)


def _dress(table: LayoutTable, art: Image.Image, scale: float) -> None:
    """Borders and shading as drawn: ``art`` is the table area rendered without its text."""
    ox, oy = table.xs[0] - 3, table.ys[0] - 3  # the render's top-left (page points)

    def at(x: float, y: float) -> tuple[float, float]:
        return (x - ox) * scale, (y - oy) * scale

    for cell in table.cells:
        r = cell.rect
        if r.width > 5 and r.height > 5:
            inner = art.crop((*at(r.x0 + 2, r.y0 + 2), *at(r.x1 - 2, r.y1 - 2)))
            fill = _dominant(inner.convert("RGB"))
            cell.shading = _hex(fill) if _luma(fill) < WHITE else None
        cell.borders = {
            "top": _edge(art, scale, at(r.x0, r.y0), at(r.x1, r.y0)),
            "bottom": _edge(art, scale, at(r.x0, r.y1), at(r.x1, r.y1)),
            "left": _edge(art, scale, at(r.x0, r.y0), at(r.x0, r.y1)),
            "right": _edge(art, scale, at(r.x1, r.y0), at(r.x1, r.y1)),
        }


def _page_text(
    page: Page, found: Sequence[TableData], measure: Measure | None
) -> tuple[list[Frame], list[LayoutTable], list[Rect]]:
    """Frames and tables for the page's text, and where text stays that Word can't write
    (upside down, slanted): that text is left in the background picture."""
    blocks = page.text_page(with_chars=True).blocks
    fonts = real_font_names(page, blocks)
    widths = _Widths(page, measure)
    pieces = [p for b in blocks if not b.is_image for ln in b.lines for p in split_line(ln)]
    pieces = [p for p in pieces if p.text.strip() and p.spans]
    tables = [t for t in (_table(td, pieces, fonts, widths) for td in found) if t is not None]
    in_cells = [c.rect for t in tables for c in t.cells]
    loose = [
        p for p in pieces if not _is_horizontal(p) and not any(_center_in(p, r) for r in in_cells)
    ]
    tables += _vertical_boxes(loose, fonts)
    frames: list[Frame] = []
    for block in editable_blocks(blocks):
        lines = [
            ln
            for ln in block.lines
            if _is_horizontal(ln)
            and ln.text.strip()
            and not any(_center_in(ln, r) for r in in_cells)
        ]
        if lines:
            frames.append(_frame([[ln] for ln in lines], fonts, widths))
    written = [c.rect for t in tables for c in t.cells if c.direction]
    unplaced = [
        p.bbox
        for p in pieces
        if not _is_horizontal(p) and not any(_center_in(p, r) for r in written)
    ]
    return frames, tables, unplaced


def _holds(box: Rect, piece: Rect) -> bool:
    cx, cy = (piece.x0 + piece.x1) / 2, (piece.y0 + piece.y1) / 2
    return box.x0 <= cx <= box.x1 and box.y0 <= cy <= box.y1


def _is_blank(img: Image.Image) -> bool:
    if img.mode == "RGBA":  # judge what shows on white paper
        img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img)
    darkest, _ = img.convert("L").getextrema()
    return float(darkest) >= 250  # type: ignore[arg-type]


def _inside(rect: Rect, area: Rect, margin: float = 3.0) -> bool:
    return (
        rect.x0 >= area.x0 - margin
        and rect.y0 >= area.y0 - margin
        and rect.x1 <= area.x1 + margin
        and rect.y1 <= area.y1 + margin
    )


def analyze_layout(
    doc: Document,
    pages: Sequence[int],
    picture_dpi: int = 150,
    pictures: bool = True,
    background: bool = True,
    tables: bool = True,
    measure: Measure | None = None,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> list[LayoutPage]:
    """Pages with positioned text frames, tables, pictures and a vector-art background.

    Pictures, table borders and the background are rendered from a copy of the document with
    the placed text (and then the images and table rules) removed, so nothing shows twice. That
    needs ``capabilities.content_edit``; without it (``background=False``) pages get frames,
    borderless tables and pictures only. ``measure`` (``Engine.text_width``) sizes text in
    standard fonts the PDF doesn't embed.
    """
    scratch = doc.copy() if background else None
    out: list[LayoutPage] = []
    try:
        for n, index in enumerate(pages):
            if token is not None:
                token.check()
            page = doc.page(index)
            rect = page.rect
            frames, placed, unplaced = _page_text(
                page, page.find_tables() if tables else [], measure
            )
            result = LayoutPage(rect.width, rect.height, frames, placed)
            areas = [a for a in page.image_areas() if a.width >= 1 and a.height >= 1]
            source = page
            if scratch is not None:
                source = scratch.page(index)
                source.delete_objects(
                    [
                        o.key
                        for o in source.content_objects()
                        # all text now in frames and cells, whatever the engine can edit;
                        # only text Word can't write stays in the picture
                        if o.type is ObjectType.TEXT
                        and not any(_holds(o.bbox, r) for r in unplaced)
                    ]
                )
            if pictures:
                result.pictures = [
                    (a, _png(_render(source, a, picture_dpi), a.width, a.height)) for a in areas
                ]
            if scratch is not None:
                source = scratch.page(index)
                for table in (t for t in placed if t.ruled):
                    area = Rect(
                        table.xs[0] - 3, table.ys[0] - 3, table.xs[-1] + 3, table.ys[-1] + 3
                    )
                    _dress(table, _render(source, area, TABLE_DPI), TABLE_DPI / 72)
                drop = [
                    o.key
                    for o in source.content_objects()
                    if o.type is ObjectType.IMAGE
                    or (
                        o.type is ObjectType.PATH
                        and any(_inside(o.bbox, t.rect) for t in placed if t.ruled)
                    )
                ]
                if drop:
                    source.delete_objects(drop)
                # transparent where nothing is drawn, so it can't hide the pictures above it
                art = _render(scratch.page(index), None, picture_dpi, alpha=True)
                if not _is_blank(art):
                    result.background = _png(art, rect.width, rect.height)
            out.append(result)
            progress(n + 1, len(pages))
    finally:
        if scratch is not None:
            scratch.close()
    return out
