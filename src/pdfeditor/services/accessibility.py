"""Accessibility checker (a practical subset of the PDF/UA and WCAG checks Acrobat runs).

Headless: every rule yields a :class:`Finding` (passed, failed or a warning), with the page
and the tag (structure element) it refers to, so the UI can jump there and fix what's fixable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import Document
from pdfeditor.model.annotations import AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.structure import StructNode, walk_all
from pdfeditor.model.text import FontFlags

WHITE = Color(1, 1, 1)
LARGE_TEXT_PT = 18.0  # WCAG "large text": 18 pt, or 14 pt bold
LARGE_BOLD_PT = 14.0
MIN_CONTRAST = 4.5
MIN_CONTRAST_LARGE = 3.0


class Status(Enum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "needs review"


@dataclass(frozen=True)
class Finding:
    rule: str
    title: str
    status: Status
    detail: str = ""
    page_index: int | None = None
    ref: int | None = None  # structure element, when the finding is about one tag
    fixable: bool = False


# -- contrast -------------------------------------------------------------------------------------
def _luminance(c: Color) -> float:
    def channel(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = c.rgb()
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a: Color, b: Color) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _needed(size: float, bold: bool) -> float:
    large = size >= LARGE_TEXT_PT or (bold and size >= LARGE_BOLD_PT)
    return MIN_CONTRAST_LARGE if large else MIN_CONTRAST


# -- rules ----------------------------------------------------------------------------------------
def _document_rules(doc: Document) -> list[Finding]:
    out: list[Finding] = []
    info = doc.info()
    settings = doc.accessibility_settings()
    title = doc.metadata().title.strip()
    out.append(
        Finding("tagged", "Tagged PDF", Status.PASSED)
        if info.is_tagged
        else Finding(
            "tagged",
            "Tagged PDF",
            Status.FAILED,
            "The document has no tags, so screen readers can't tell headings, paragraphs, "
            "lists or figures apart, or read them in the right order.",
        )
    )
    out.append(
        Finding("language", "Document language", Status.PASSED, settings.language)
        if settings.language
        else Finding(
            "language",
            "Document language",
            Status.FAILED,
            "No language is set, so screen readers may pronounce the text wrongly.",
            fixable=True,
        )
    )
    out.append(
        Finding("title", "Title", Status.PASSED, title)
        if title
        else Finding("title", "Title", Status.FAILED, "The document has no title.", fixable=True)
    )
    out.append(
        Finding("display-title", "Title shown in the title bar", Status.PASSED)
        if settings.display_doc_title
        else Finding(
            "display-title",
            "Title shown in the title bar",
            Status.FAILED,
            "Viewers show the file name instead of the document title.",
            fixable=True,
        )
    )
    if any(doc.page(i).annotations() for i in range(doc.page_count)):
        out.append(
            Finding("tab-order", "Tab order follows the tags", Status.PASSED)
            if settings.tab_order_structure
            else Finding(
                "tab-order",
                "Tab order follows the tags",
                Status.FAILED,
                "Pages with comments or links don't use the structure (reading) order for "
                "keyboard navigation.",
                fixable=True,
            )
        )
    return out


def _tag_rules(roots: Sequence[StructNode]) -> list[Finding]:
    out: list[Finding] = []
    nodes = list(walk_all(list(roots)))
    figures = [n for n in nodes if n.standard_type in ("Figure", "Formula")]
    missing = [n for n in figures if not (n.alt.strip() or n.actual_text.strip())]
    for n in missing:
        out.append(
            Finding(
                "alt-text",
                f"{n.standard_type} without alternate text",
                Status.FAILED,
                "Screen readers can't describe it.",
                n.page_index,
                n.ref,
                fixable=True,
            )
        )
    if figures and not missing:
        out.append(Finding("alt-text", "Figures have alternate text", Status.PASSED))

    headings = [n for n in nodes if n.heading_level is not None]
    skipped = False
    previous = 0
    for n in headings:
        level = n.heading_level or 0
        if level > previous + 1:
            skipped = True
            out.append(
                Finding(
                    "headings",
                    f"Heading level skipped: {n.standard_type} after "
                    + (f"H{previous}" if previous else "the start"),
                    Status.WARNING,
                    "Headings should go down one level at a time so the outline makes sense.",
                    n.page_index,
                    n.ref,
                )
            )
        previous = level
    if headings and not skipped:
        out.append(Finding("headings", "Heading levels are nested properly", Status.PASSED))

    # reading order: the tags' order should not jump back to an earlier page
    leaves = [n for n in nodes if not n.children and n.page_index is not None]
    furthest = -1
    backwards = None
    for n in leaves:
        assert n.page_index is not None
        if n.page_index < furthest:
            backwards = n
            break
        furthest = max(furthest, n.page_index)
    if backwards is not None:
        out.append(
            Finding(
                "reading-order",
                "Reading order jumps back to an earlier page",
                Status.WARNING,
                f"A {backwards.standard_type} on page {(backwards.page_index or 0) + 1} "
                f"is read after content from page {furthest + 1}.",
                backwards.page_index,
                backwards.ref,
            )
        )
    elif leaves:
        out.append(Finding("reading-order", "Reading order follows the pages", Status.PASSED))
    return out


def _contrast_rules(
    doc: Document, pages: Sequence[int], token: CancelToken | None
) -> list[Finding]:
    out: list[Finding] = []
    worst_any = False
    for index in pages:
        if token is not None:
            token.check()
        low = 0
        worst = 21.0
        for block in doc.page(index).text_page(with_chars=False).blocks:
            for line in block.lines:
                for span in line.spans:
                    if not span.text.strip():
                        continue
                    bold = bool(span.flags & FontFlags.BOLD)
                    ratio = contrast_ratio(span.color, WHITE)
                    if ratio < _needed(span.size, bold):
                        low += 1
                        worst = min(worst, ratio)
        for a in doc.page(index).annotations():
            if a.type is AnnotationType.FREE_TEXT and a.text_color is not None:
                background = a.fill or WHITE
                ratio = contrast_ratio(a.text_color, background)
                if ratio < _needed(a.font_size, False):
                    low += 1
                    worst = min(worst, ratio)
        if low:
            worst_any = True
            out.append(
                Finding(
                    "contrast",
                    f"Low-contrast text on page {index + 1}",
                    Status.WARNING,
                    f"{low} text run(s) below the WCAG minimum (worst {worst:.1f}:1; "
                    "assumes a white page background).",
                    index,
                )
            )
    if not worst_any:
        out.append(Finding("contrast", "Text contrast", Status.PASSED))
    return out


def check(
    doc: Document, token: CancelToken | None = None, progress: ProgressFn = no_progress
) -> list[Finding]:
    """Run every rule; failed first, then warnings, then passed."""
    findings = _document_rules(doc)
    progress(1, 3)
    findings += _tag_rules(doc.structure_tree())
    progress(2, 3)
    findings += _contrast_rules(doc, range(doc.page_count), token)
    progress(3, 3)
    order = {Status.FAILED: 0, Status.WARNING: 1, Status.PASSED: 2}
    return sorted(findings, key=lambda f: order[f.status])
