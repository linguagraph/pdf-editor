"""Search-and-redact presets, mark creation and post-redaction verification (headless)."""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field

from pdfeditor.engine.base import Document, UnsupportedFeature
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.color import BLACK, WHITE, Color
from pdfeditor.model.geometry import Quad, Rect
from pdfeditor.model.objects import ObjectType
from pdfeditor.model.text import SearchHit, SearchOptions
from pdfeditor.services.search import SearchQuery, search_document
from pdfeditor.services.text import TextIndexCache

# Deliberately broad: a redaction search should over-find, then the user reviews each match.
PRESETS: dict[str, str] = {
    "Email addresses": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "Phone numbers": r"(?<!\w)\+?\d[\d ()./-]{6,}\d(?!\w)",
    "Credit card numbers": r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)",
    "IBAN": r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]){10,30}(?![A-Z0-9])",
    "US Social Security numbers": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "Dates": r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})(?!\d)",
}


def luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def find_sensitive(
    cache: TextIndexCache, patterns: Sequence[str], pages: Sequence[int] | None = None
) -> list[SearchHit]:
    """Matches of the given regexes (preset names or raw patterns); card numbers Luhn-checked."""
    hits: list[SearchHit] = []
    for pattern in patterns:
        regex = PRESETS.get(pattern, pattern)
        options = SearchOptions(
            case_sensitive=True, regex=True, page_indices=tuple(pages) if pages else None
        )
        found = search_document(cache, SearchQuery(regex, options))
        if pattern == "Credit card numbers":
            found = [h for h in found if luhn_ok(h.text)]
        hits.extend(found)
    # the same text can match several presets: keep one mark per area
    unique: dict[tuple[int, tuple[float, ...]], SearchHit] = {}
    for h in hits:
        unique.setdefault((h.page_index, tuple(round(v, 1) for v in h.rect.as_tuple())), h)
    return sorted(unique.values(), key=lambda h: (h.page_index, h.rect.y0, h.rect.x0))


@dataclass(frozen=True)
class MarkStyle:
    fill: Color = BLACK
    overlay_text: str = ""
    text_color: Color = WHITE


DEFAULT_STYLE = MarkStyle()


def mark_for_area(
    page: int, rect: Rect, style: MarkStyle = DEFAULT_STYLE, author: str = ""
) -> AnnotationModel:
    return AnnotationModel(
        AnnotationType.REDACT,
        page,
        rect,
        fill=style.fill,
        overlay_text=style.overlay_text,
        text_color=style.text_color,
        author=author,
        subject="Redaction",
    )


def mark_for_hit(
    hit: SearchHit, style: MarkStyle = DEFAULT_STYLE, author: str = ""
) -> AnnotationModel:
    model = mark_for_area(hit.page_index, hit.rect, style, author)
    model.quads = tuple(Quad.from_rect(q.rect) for q in hit.quads)
    model.contents = hit.text
    return model


def mark_areas(mark: AnnotationModel) -> list[Rect]:
    return [q.rect for q in mark.quads] if mark.quads else [mark.rect]


@dataclass
class VerificationReport:
    areas: int = 0
    images_checked: int = 0
    leaks: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.leaks


def verify(
    doc: Document, areas: dict[int, list[Rect]], overlay_texts: Sequence[str] = ()
) -> VerificationReport:
    """Check that nothing readable is left under redacted areas (visible page space).

    Text: no extractable character may have its centre inside an area, apart from the marks'
    own overlay labels (``overlay_texts``, e.g. "REDACTED"). Images: pixels of any image still
    overlapping an area must be blanked (a single uniform color).
    """
    labels = sorted({t.replace(" ", "") for t in overlay_texts if t.strip()}, key=len, reverse=True)
    report = VerificationReport()
    for index, rects in areas.items():
        page = doc.page(index)
        chars = page.text_page(with_chars=True).chars()
        for rect in rects:
            report.areas += 1
            inner = rect.inflated(-0.5)
            leaked = "".join(
                c.c for c in chars if inner.contains(c.bbox.center) and not c.c.isspace()
            )
            for label in labels:
                leaked = leaked.replace(label, "")
            if leaked:
                report.leaks.append(
                    f"page {index + 1}: text still readable under a mark: {leaked[:40]!r}"
                )
        try:
            objects = page.content_objects()
        except UnsupportedFeature:
            objects = []
            report.unverified.append(f"page {index + 1}: images couldn't be inspected")
        for obj in objects:
            if obj.type is not ObjectType.IMAGE:
                continue
            overlapping = [r for r in rects if r.intersects(obj.bbox)]
            if not overlapping:
                continue
            try:
                data, _ext = page.image_data(obj.key)
            except Exception:
                report.unverified.append(
                    f"page {index + 1}: an inline or unreadable image overlaps a mark"
                )
                continue
            report.images_checked += 1
            for rect in overlapping:
                if not _blanked(data, obj.bbox, rect):
                    report.leaks.append(
                        f"page {index + 1}: image pixels under a mark were not removed"
                    )
    return report


def _blanked(data: bytes, placement: Rect, area: Rect) -> bool:
    """True if the part of the image under ``area`` is one flat color (axis-aligned placement)."""
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    overlap = area.intersection(placement)
    if overlap.is_empty:
        return True
    sx, sy = img.width / placement.width, img.height / placement.height
    box = (
        int((overlap.x0 - placement.x0) * sx) + 1,
        int((overlap.y0 - placement.y0) * sy) + 1,
        int((overlap.x1 - placement.x0) * sx) - 1,
        int((overlap.y1 - placement.y0) * sy) - 1,
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return True
    colors = img.crop(box).getcolors(maxcolors=2)
    return colors is not None and len(colors) == 1
