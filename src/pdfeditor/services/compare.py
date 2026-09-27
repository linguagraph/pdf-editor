"""Compare two documents: page alignment, word-level text diff and a visual (pixel) diff.

Headless; the UI shows the result side by side and can export it as a report PDF.

* Pages are aligned by text similarity (``difflib`` over page signatures), so inserted and
  deleted pages are found instead of shifting every later page into a "change".
* Aligned pages get a word diff; each change carries the word boxes on both sides.
* Pages whose text is identical (or that have no text, like scans) get a visual diff: both are
  rendered, compared with Pillow, and differing pixels are grouped into region boxes.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PIL import Image, ImageChops

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import ColorMode, Document, Engine, RenderRequest
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Point, Rect
from pdfeditor.model.pages import TextStamp
from pdfeditor.model.text import TextPage

PAGE_MATCH_RATIO = 0.5  # pages at least this similar are the "same page, edited"
VISUAL_DPI = 50
VISUAL_CELL = 4  # px per grid cell when grouping differing pixels
VISUAL_THRESHOLD = 40  # 0-255 grey difference that counts as changed
VISUAL_MIN_CELLS = 2  # smaller specks are rendering noise


class ChangeKind(Enum):
    INSERTED = "inserted"  # text only in the new document
    DELETED = "deleted"  # text only in the old document
    REPLACED = "replaced"
    VISUAL = "visual"  # pixels differ where the text is the same
    PAGE_INSERTED = "page inserted"
    PAGE_DELETED = "page deleted"


@dataclass
class Change:
    kind: ChangeKind
    page_a: int | None  # page in the old document (None: only in the new one)
    page_b: int | None
    rects_a: list[Rect] = field(default_factory=list)
    rects_b: list[Rect] = field(default_factory=list)
    text_a: str = ""
    text_b: str = ""

    @property
    def summary(self) -> str:
        def clip(s: str) -> str:
            s = " ".join(s.split())
            return s if len(s) <= 60 else s[:57] + "…"

        if self.kind is ChangeKind.INSERTED:
            return f"Inserted “{clip(self.text_b)}”"
        if self.kind is ChangeKind.DELETED:
            return f"Deleted “{clip(self.text_a)}”"
        if self.kind is ChangeKind.REPLACED:
            return f"“{clip(self.text_a)}” → “{clip(self.text_b)}”"
        if self.kind is ChangeKind.PAGE_INSERTED:
            return f"Page {(self.page_b or 0) + 1} inserted"
        if self.kind is ChangeKind.PAGE_DELETED:
            return f"Page {(self.page_a or 0) + 1} deleted"
        return "Appearance changed"


@dataclass
class CompareResult:
    pairs: list[tuple[int | None, int | None]]  # aligned pages (old, new)
    changes: list[Change]

    def for_page_a(self, index: int) -> list[Change]:
        return [c for c in self.changes if c.page_a == index]

    def for_page_b(self, index: int) -> list[Change]:
        return [c for c in self.changes if c.page_b == index]

    @property
    def identical(self) -> bool:
        return not self.changes


@dataclass(frozen=True)
class CompareOptions:
    text: bool = True
    visual: bool = True  # pixel diff where the text didn't change
    visual_dpi: int = VISUAL_DPI


# -- words ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Word:
    text: str
    rect: Rect
    line: int  # running line number, to merge boxes per line


def page_words(tp: TextPage) -> list[Word]:
    """Words in reading order with their boxes (from per-character boxes)."""
    words: list[Word] = []
    line_no = 0
    # visual reading order: an edited paragraph is re-typeset at the end of the content
    # stream, so the engine's block order alone would turn "changed" into "deleted + inserted"
    blocks = sorted(
        (b for b in tp.blocks if not b.is_image), key=lambda b: (round(b.bbox.y0), b.bbox.x0)
    )
    for block in blocks:
        for line in block.lines:
            line_no += 1
            chars = [ch for span in line.spans for ch in span.chars]
            current: list = []
            for ch in [*chars, None]:
                if ch is None or ch.c.isspace():
                    if current:
                        text = "".join(c.c for c in current)
                        box = Rect(
                            min(c.bbox.x0 for c in current),
                            min(c.bbox.y0 for c in current),
                            max(c.bbox.x1 for c in current),
                            max(c.bbox.y1 for c in current),
                        )
                        words.append(Word(text, box, line_no))
                        current = []
                else:
                    current.append(ch)
    return words


def _merge_rects(words: Sequence[Word]) -> list[Rect]:
    """One box per line for a run of words."""
    by_line: dict[int, Rect] = {}
    for w in words:
        r = by_line.get(w.line)
        by_line[w.line] = (
            w.rect
            if r is None
            else Rect(
                min(r.x0, w.rect.x0),
                min(r.y0, w.rect.y0),
                max(r.x1, w.rect.x1),
                max(r.y1, w.rect.y1),
            )
        )
    return list(by_line.values())


def diff_words(a: Sequence[Word], b: Sequence[Word], page_a: int, page_b: int) -> list[Change]:
    sm = difflib.SequenceMatcher(None, [w.text for w in a], [w.text for w in b], autojunk=False)
    out: list[Change] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        wa, wb = a[i1:i2], b[j1:j2]
        kind = {
            "insert": ChangeKind.INSERTED,
            "delete": ChangeKind.DELETED,
            "replace": ChangeKind.REPLACED,
        }[tag]
        out.append(
            Change(
                kind,
                page_a,
                page_b,
                _merge_rects(wa),
                _merge_rects(wb),
                " ".join(w.text for w in wa),
                " ".join(w.text for w in wb),
            )
        )
    return out


# -- page alignment -------------------------------------------------------------------------------
def _signature(words: Sequence[Word]) -> str:
    return " ".join(w.text for w in words)


def _similar(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a.split(), b.split(), autojunk=False).ratio()


def align_pages(sig_a: Sequence[str], sig_b: Sequence[str]) -> list[tuple[int | None, int | None]]:
    """Pair pages of the old and new document; unpaired pages were deleted or inserted."""
    pairs: list[tuple[int | None, int | None]] = []
    sm = difflib.SequenceMatcher(None, list(sig_a), list(sig_b), autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pairs.extend(zip(range(i1, i2), range(j1, j2), strict=True))
            continue
        # within a changed stretch: pair each old page with the next similar new page; new
        # pages skipped over were inserted, old pages without a partner were deleted
        j = j1
        for i in range(i1, i2):
            partner = next(
                (k for k in range(j, j2) if _similar(sig_a[i], sig_b[k]) >= PAGE_MATCH_RATIO), None
            )
            if partner is None:
                pairs.append((i, None))
                continue
            pairs.extend((None, k) for k in range(j, partner))
            pairs.append((i, partner))
            j = partner + 1
        pairs.extend((None, k) for k in range(j, j2))
    return pairs


# -- visual diff ----------------------------------------------------------------------------------
def _render_gray(doc: Document, index: int, dpi: int) -> Image.Image:
    shot = doc.page(index).render(
        RenderRequest(matrix=Matrix.scale(dpi / 72), color=ColorMode.GRAY, annotations=True)
    )
    return Image.frombytes("L", (shot.width, shot.height), shot.samples, "raw", "L", shot.stride)


def diff_regions(a: Image.Image, b: Image.Image, scale: float) -> list[Rect]:
    """Boxes (page points, ``scale`` = px per point) around pixels that differ."""
    w, h = max(a.width, b.width), max(a.height, b.height)
    canvas_a, canvas_b = Image.new("L", (w, h), 255), Image.new("L", (w, h), 255)
    canvas_a.paste(a, (0, 0))
    canvas_b.paste(b, (0, 0))
    diff = ImageChops.difference(canvas_a, canvas_b).point(
        lambda v: 255 if v > VISUAL_THRESHOLD else 0
    )
    if diff.getbbox() is None:
        return []
    cols, rows = -(-w // VISUAL_CELL), -(-h // VISUAL_CELL)
    # box-average each cell: any differing pixel makes the cell non-zero ("hot")
    grid = diff.resize((cols, rows), Image.Resampling.BOX)
    data = grid.tobytes()
    hot = {(i % cols, i // cols) for i, v in enumerate(data) if v}
    boxes: list[Rect] = []
    while hot:
        start = hot.pop()
        stack, cells = [start], [start]
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in hot:
                        hot.remove(n)
                        stack.append(n)
                        cells.append(n)
        if len(cells) < VISUAL_MIN_CELLS:
            continue
        x0, y0 = min(c[0] for c in cells), min(c[1] for c in cells)
        x1, y1 = max(c[0] for c in cells) + 1, max(c[1] for c in cells) + 1
        k = VISUAL_CELL / scale
        boxes.append(Rect(x0 * k, y0 * k, x1 * k, y1 * k))
    return sorted(boxes, key=lambda r: (r.y0, r.x0))


# -- compare --------------------------------------------------------------------------------------
def compare(
    old: Document,
    new: Document,
    options: CompareOptions | None = None,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> CompareResult:
    options = options or CompareOptions()
    words_a = [page_words(old.page(i).text_page()) for i in range(old.page_count)]
    words_b = [page_words(new.page(i).text_page()) for i in range(new.page_count)]
    pairs = align_pages([_signature(w) for w in words_a], [_signature(w) for w in words_b])
    changes: list[Change] = []
    for n, (ia, ib) in enumerate(pairs):
        if token is not None:
            token.check()
        if ia is None:
            changes.append(Change(ChangeKind.PAGE_INSERTED, None, ib))
        elif ib is None:
            changes.append(Change(ChangeKind.PAGE_DELETED, ia, None))
        else:
            text_changes = diff_words(words_a[ia], words_b[ib], ia, ib) if options.text else []
            changes.extend(text_changes)
            if options.visual and not text_changes:
                scale = options.visual_dpi / 72
                regions = diff_regions(
                    _render_gray(old, ia, options.visual_dpi),
                    _render_gray(new, ib, options.visual_dpi),
                    scale,
                )
                changes.extend(Change(ChangeKind.VISUAL, ia, ib, [r], [r]) for r in regions)
        progress(n + 1, len(pairs))
    return CompareResult(pairs, changes)


# -- report ---------------------------------------------------------------------------------------
_KIND_COLORS = {
    ChangeKind.INSERTED: Color(0.1, 0.65, 0.2),
    ChangeKind.DELETED: Color(0.85, 0.15, 0.15),
    ChangeKind.REPLACED: Color(0.95, 0.55, 0.0),
    ChangeKind.VISUAL: Color(0.2, 0.4, 0.9),
}
REPORT_LINES_PER_PAGE = 52


def change_color(kind: ChangeKind) -> Color:
    return _KIND_COLORS.get(kind, Color(0.5, 0.5, 0.5))


def write_report(
    engine: Engine,
    old: Document,
    new: Document,
    result: CompareResult,
    target: Path,
    old_name: str = "Old",
    new_name: str = "New",
) -> Path:
    """A PDF with a summary of changes, then each changed page of both documents with the
    changes outlined (red: deleted, green: inserted, orange: replaced, blue: appearance)."""
    doc = engine.new_document()
    try:
        counts = {k: sum(1 for c in result.changes if c.kind is k) for k in ChangeKind}
        lines = [
            ("Comparison report", 16.0),
            (f"Old: {old_name} ({old.page_count} pages)", 10.0),
            (f"New: {new_name} ({new.page_count} pages)", 10.0),
            ("", 10.0),
            (
                "No differences found."
                if result.identical
                else ", ".join(f"{n} {k.value}" for k, n in counts.items() if n),
                10.0,
            ),
            ("", 10.0),
        ]
        for c in result.changes:
            where = (
                (f"old p.{c.page_a + 1}" if c.page_a is not None else "")
                + (" / " if c.page_a is not None and c.page_b is not None else "")
                + (f"new p.{c.page_b + 1}" if c.page_b is not None else "")
            )
            lines.append((f"{where}: {c.summary}", 9.0))
        for start in range(0, len(lines), REPORT_LINES_PER_PAGE):
            at = doc.page_count
            doc.insert_blank_page(at, 595, 842)
            y = 56.0
            for text, size in lines[start : start + REPORT_LINES_PER_PAGE]:
                if text:
                    doc.page(at).stamp_text(TextStamp(text[:110], Point(48, y), font_size=size))
                y += size * 1.45
        for ia, ib in result.pairs:
            changes_a = result.for_page_a(ia) if ia is not None else []
            changes_b = result.for_page_b(ib) if ib is not None else []
            if not changes_a and not changes_b:
                continue
            if ia is not None:
                _report_page(doc, old, ia, f"{old_name} — page {ia + 1}", changes_a, old=True)
            if ib is not None:
                _report_page(doc, new, ib, f"{new_name} — page {ib + 1}", changes_b, old=False)
        return doc.save(target)
    finally:
        doc.close()


def _report_page(
    doc: Document, src: Document, index: int, label: str, changes: list[Change], old: bool
) -> None:
    at = doc.page_count
    doc.insert_pages(src, [index], at)
    page = doc.page(at)
    for c in changes:
        rects = c.rects_a if old else c.rects_b
        if c.kind in (ChangeKind.PAGE_DELETED, ChangeKind.PAGE_INSERTED):
            rects = [page.rect]
        for r in rects:
            page.add_annotation(
                AnnotationModel(
                    type=AnnotationType.SQUARE,
                    page_index=at,
                    rect=r.inflated(1.5),
                    color=change_color(c.kind),
                    border_width=1.5,
                    contents=c.summary,
                )
            )
    page.stamp_text(TextStamp(label, Point(8, 10), font_size=7, color=Color(0.35, 0.35, 0.35)))
