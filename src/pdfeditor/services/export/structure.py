"""Reading-order document structure for reflowing exports (Word, HTML, Markdown).

Pages are turned into a flat list of elements: headings and paragraphs made of styled runs,
tables (from :meth:`Page.find_tables`), pictures (the page rendered inside each image area)
and page breaks. Headings are recognized by font size relative to the body text.
"""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from PIL import Image

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import ColorMode, Document, RenderRequest
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.model.text import Block, FontFlags, Span, TableData

HEADING_RATIO = 1.15  # a block this much larger than body text is a heading
MAX_HEADING_LEVEL = 3
MIN_PICTURE_PT = 8.0


@dataclass
class Run:
    text: str
    size: float = 11.0
    bold: bool = False
    italic: bool = False
    mono: bool = False
    color: Color = field(default_factory=lambda: Color(0, 0, 0))
    font: str = ""

    def same_style(self, other: Run) -> bool:
        return (self.size, self.bold, self.italic, self.mono, self.color, self.font) == (
            other.size,
            other.bold,
            other.italic,
            other.mono,
            other.color,
            other.font,
        )


@dataclass
class Paragraph:
    runs: list[Run]
    level: int = 0  # 0 = body text, 1.. = heading level

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class Picture:
    png: bytes
    width: float  # points
    height: float


@dataclass
class Table:
    rows: list[list[str]]


@dataclass
class PageBreak:
    pass


Element = Paragraph | Picture | Table | PageBreak


@dataclass(frozen=True)
class StructureOptions:
    tables: bool = True
    pictures: bool = True
    picture_dpi: int = 150
    page_breaks: bool = True


def base_font_name(font: str) -> str:
    """``ABCDEF+Arial-BoldMT`` -> ``Arial``."""
    name = font.split("+", 1)[-1]
    for sep in ("-", ","):
        name = name.split(sep, 1)[0]
    return name.removesuffix("MT").removesuffix("PS") or font


def _run(span: Span) -> Run:
    flags = span.flags
    lower = span.font.lower()
    return Run(
        text=span.text,
        size=round(span.size * 2) / 2,
        bold=bool(flags & FontFlags.BOLD) or "bold" in lower or "black" in lower,
        italic=bool(flags & FontFlags.ITALIC) or "italic" in lower or "oblique" in lower,
        mono=bool(flags & FontFlags.MONOSPACED) or "courier" in lower or "mono" in lower,
        color=span.color,
        font=base_font_name(span.font),
    )


def _append(runs: list[Run], run: Run) -> None:
    if not run.text:
        return
    if runs and runs[-1].same_style(run):
        runs[-1].text += run.text
    else:
        runs.append(run)


def paragraph_runs(block: Block) -> list[Run]:
    """The block's lines joined into one paragraph (soft hyphens at line ends removed)."""
    runs: list[Run] = []
    for n, line in enumerate(block.lines):
        spans = [s for s in line.spans if s.text]
        if not spans:
            continue
        if n and runs:
            prev = runs[-1]
            first = spans[0].text.lstrip()
            if prev.text.endswith("-") and first[:1].islower():
                prev.text = prev.text[:-1]
            elif not prev.text.endswith(" "):
                prev.text += " "
        for span in spans:
            _append(runs, _run(span))
    if runs:
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()
    return [r for r in runs if r.text]


def body_size(blocks: Sequence[Block]) -> float:
    """The most common text size, weighted by characters."""
    sizes: Counter[float] = Counter()
    for b in blocks:
        for line in b.lines:
            for s in line.spans:
                sizes[round(s.size * 2) / 2] += len(s.text.strip())
    return sizes.most_common(1)[0][0] if sizes else 11.0


def heading_levels(blocks: Sequence[Block], body: float) -> dict[float, int]:
    larger = sorted(
        {
            round(max(s.size for ln in b.lines for s in ln.spans) * 2) / 2
            for b in blocks
            if b.lines and any(ln.spans for ln in b.lines)
        },
        reverse=True,
    )
    levels = [s for s in larger if s >= body * HEADING_RATIO]
    return {size: min(i + 1, MAX_HEADING_LEVEL) for i, size in enumerate(levels)}


def _inside(block: Block, areas: Sequence[Rect]) -> bool:
    cx, cy = (block.bbox.x0 + block.bbox.x1) / 2, (block.bbox.y0 + block.bbox.y1) / 2
    return any(a.x0 <= cx <= a.x1 and a.y0 <= cy <= a.y1 for a in areas)


def _picture(doc: Document, index: int, area: Rect, dpi: int) -> Picture:
    shot = doc.page(index).render(
        RenderRequest(
            matrix=Matrix.scale(dpi / 72), clip=area, color=ColorMode.RGB, annotations=False
        )
    )
    img = Image.frombytes("RGB", (shot.width, shot.height), shot.samples, "raw", "RGB", shot.stride)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return Picture(buf.getvalue(), area.width, area.height)


def _table(t: TableData) -> Table:
    return Table([[(c or "").replace("\n", " ").strip() for c in row] for row in t.rows])


def analyze(
    doc: Document,
    pages: Sequence[int],
    options: StructureOptions | None = None,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> list[Element]:
    options = options or StructureOptions()
    page_blocks = {i: doc.page(i).text_page(with_chars=False).blocks for i in pages}
    all_blocks = [b for blocks in page_blocks.values() for b in blocks if not b.is_image]
    body = body_size(all_blocks)
    levels = heading_levels(all_blocks, body)
    out: list[Element] = []
    for n, index in enumerate(pages):
        if token is not None:
            token.check()
        page = doc.page(index)
        tables = page.find_tables() if options.tables else []
        areas = [
            a
            for a in (page.image_areas() if options.pictures else [])
            if a.width >= MIN_PICTURE_PT and a.height >= MIN_PICTURE_PT
        ]
        # text blocks keep the engine's reading order; tables and pictures slot in by position
        extras: list[tuple[float, Element]] = [(t.bbox.y0, _table(t)) for t in tables]
        extras += [(a.y0, _picture(doc, index, a, options.picture_dpi)) for a in areas]
        extras.sort(key=lambda e: e[0])
        table_areas = [t.bbox for t in tables]
        if n and options.page_breaks:
            out.append(PageBreak())
        for block in page_blocks[index]:
            if block.is_image or _inside(block, table_areas):
                continue
            runs = paragraph_runs(block)
            if not runs:
                continue
            while extras and extras[0][0] <= block.bbox.y0:
                out.append(extras.pop(0)[1])
            size = max(r.size for r in runs)
            out.append(Paragraph(runs, levels.get(size, 0)))
        out.extend(e for _, e in extras)
        progress(n + 1, len(pages))
    return out
