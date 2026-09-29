"""Reading-order document structure for reflowing exports (Word, HTML, Markdown).

Pages are turned into a flat list of elements: headings and paragraphs made of styled runs,
tables (from :meth:`Page.find_tables`), pictures (the page rendered inside each image area)
and page breaks. Headings are recognized by font size relative to the body text.
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from fontTools.ttLib import TTFont
from PIL import Image

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import ColorMode, Document, Page, RenderRequest
from pdfeditor.engine.textlayout import body_size, heading_levels
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.model.text import Block, FontFlags, Span, TableData

MIN_PICTURE_PT = 8.0
MARGIN_BAND = 0.1  # top/bottom share of the page where running headers and footers live
_PAGE_NUMBER = re.compile(r"^[\s\-\u2013\u2014]*\d{1,4}[\s\-\u2013\u2014]*$")


@dataclass
class Run:
    text: str
    size: float = 11.0
    bold: bool = False
    italic: bool = False
    mono: bool = False
    color: Color = field(default_factory=lambda: Color(0, 0, 0))
    font: str = ""
    spacing: float = 0.0  # extra points after each character (condensed/expanded text)

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
    # Word only: keep every page's layout (positioned text, anchored pictures) instead of
    # reflowing; ``vector_background`` also keeps vector art (needs capabilities.content_edit)
    keep_layout: bool = False
    vector_background: bool = True


def base_font_name(font: str) -> str:
    """``ABCDEF+Arial-BoldMT`` -> ``Arial``."""
    name = font.split("+")[-1]
    for sep in ("-", ","):
        name = name.split(sep, 1)[0]
    return name.removesuffix("MT").removesuffix("PS") or font


_PLAIN_STYLES = {"regular", "normal", "roman", "book", "plain"}


def embedded_font_name(program: bytes) -> str | None:
    """``"Segoe UI-Bold"`` from a TrueType/OpenType program's name table (legacy family, the
    name Word and other apps look fonts up by); None if the program can't be read."""
    try:
        names = TTFont(io.BytesIO(program), lazy=True)["name"]
        family = names.getDebugName(1)
        style = names.getDebugName(2) or ""
    except Exception:
        return None
    if not family:
        return None
    return family if style.lower() in _PLAIN_STYLES else f"{family}-{style}"


def real_font_names(page: Page, blocks: Sequence[Block]) -> dict[str, str]:
    """Span font name -> the embedded program's own name. The PDF's name is often not one an
    application knows: a resource name ("CIDFont+F1" from Microsoft Print to PDF, "T1_0"), or
    a PostScript name with the family's spaces turned into hyphens ("Noto-Sans-Bold")."""
    names: dict[str, str] = {}
    for font in {s.font for b in blocks for ln in b.lines for s in ln.spans}:
        program = page.font_program(font)
        real = embedded_font_name(program) if program else None
        if real:
            names[font] = real
    return names


def span_run(span: Span, fonts: dict[str, str]) -> Run:
    flags = span.flags
    font = fonts.get(span.font, span.font)
    lower = font.lower()
    return Run(
        text=span.text,
        size=round(span.size * 2) / 2,
        bold=bool(flags & FontFlags.BOLD) or "bold" in lower or "black" in lower,
        italic=bool(flags & FontFlags.ITALIC) or "italic" in lower or "oblique" in lower,
        mono=bool(flags & FontFlags.MONOSPACED) or "courier" in lower or "mono" in lower,
        color=span.color,
        font=base_font_name(font),
    )


def append_run(runs: list[Run], run: Run) -> None:
    if not run.text:
        return
    if runs and runs[-1].same_style(run):
        runs[-1].text += run.text
    else:
        runs.append(run)


def paragraph_runs(block: Block, fonts: dict[str, str] | None = None) -> list[Run]:
    """The block's lines joined into one paragraph (soft hyphens at line ends removed).
    ``fonts`` renames span fonts (see :func:`real_font_names`)."""
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
            append_run(runs, span_run(span, fonts or {}))
    if runs:
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()
    return [r for r in runs if r.text]


def is_page_number(block: Block, page_height: float) -> bool:
    """A bare page number in the top or bottom margin: noise once the text reflows, and since
    footers often come first in content order, it would pull every picture ahead of the text."""
    text = "".join(s.text for ln in block.lines for s in ln.spans)
    in_margin = block.bbox.y1 <= page_height * MARGIN_BAND or block.bbox.y0 >= page_height * (
        1 - MARGIN_BAND
    )
    return in_margin and bool(_PAGE_NUMBER.match(text))


def main_size(runs: Sequence[Run]) -> float:
    """The size most of the paragraph's characters have: a larger bullet or spacer doesn't
    make body text a heading."""
    weights: dict[float, int] = {}
    for r in runs:
        weights[r.size] = weights.get(r.size, 0) + len(r.text.strip())
    return max(weights, key=lambda size: (weights[size], -size))  # a tie stays body text


def _anchor(area: Rect, blocks: Sequence[Block]) -> int | None:
    """Index of the block a table or picture goes before: the nearest one starting below its
    top, preferring its own column. Going by position rather than by the first lower block in
    reading order keeps one stray glyph or footer from pulling every picture to the top."""
    below = [i for i, b in enumerate(blocks) if b.bbox.y0 >= area.y0 - 1]
    same_column = [i for i in below if blocks[i].bbox.x0 < area.x1 and area.x0 < blocks[i].bbox.x1]
    pool = same_column or below
    return min(pool, key=lambda i: (blocks[i].bbox.y0, i)) if pool else None


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
        height = page.rect.height
        fonts = real_font_names(page, page_blocks[index])
        table_areas = [t.bbox for t in tables]
        paragraphs: list[tuple[Block, list[Run]]] = []
        for block in page_blocks[index]:
            if block.is_image or _inside(block, table_areas) or is_page_number(block, height):
                continue
            if runs := paragraph_runs(block, fonts):
                paragraphs.append((block, runs))
        # text keeps the engine's reading order; tables and pictures slot in by position
        extras: list[tuple[Rect, Element]] = [(t.bbox, _table(t)) for t in tables]
        extras += [(a, _picture(doc, index, a, options.picture_dpi)) for a in areas]
        extras.sort(key=lambda e: e[0].y0)
        before: dict[int | None, list[Element]] = {}
        for area, element in extras:
            before.setdefault(_anchor(area, [b for b, _ in paragraphs]), []).append(element)
        if n and options.page_breaks:
            out.append(PageBreak())
        for i, (_, runs) in enumerate(paragraphs):
            out.extend(before.get(i, []))
            out.append(Paragraph(runs, levels.get(main_size(runs), 0)))
        out.extend(before.get(None, []))
        progress(n + 1, len(pages))
    return out
