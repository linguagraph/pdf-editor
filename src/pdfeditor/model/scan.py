"""Scanned pages: what a page is made of, and how its recognized text becomes editable."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pdfeditor.model.color import BLACK, Color
from pdfeditor.model.geometry import Point, Rect

# A page is a scan when one image covers at least this share of it.
SCAN_COVERAGE = 0.5


class ScanCleanup(Enum):
    """What OCR does with the scanned picture of the text it recognized."""

    KEEP = "keep"  # searchable: invisible text over the untouched scan
    ERASE = "erase"  # editable: visible text, the recognized lines painted out of the scan
    REMOVE = "remove"  # text only: visible text, the full-page scan images deleted


@dataclass(frozen=True, slots=True)
class ScanInfo:
    coverage: float  # share of the page covered by its largest image (0..1)
    visible_chars: int  # non-space characters drawn visibly
    invisible_chars: int  # non-space characters in invisible text (an OCR layer)

    @property
    def is_scan(self) -> bool:
        return self.coverage >= SCAN_COVERAGE

    @property
    def needs_ocr(self) -> bool:
        """A scan nothing has been recognized on yet."""
        return self.is_scan and self.visible_chars == 0 and self.invisible_chars == 0

    @property
    def has_hidden_ocr(self) -> bool:
        """A scan whose only text is an invisible OCR layer (searchable, not yet editable)."""
        return self.is_scan and self.visible_chars == 0 and self.invisible_chars > 0


@dataclass(frozen=True, slots=True)
class ScanLine:
    """One recognized line, typeset to cover its picture in the scan (visible page space)."""

    text: str
    origin: Point  # start of the baseline
    size: float  # font size in points
    scale: float = 1.0  # horizontal scaling that makes the line as wide as in the scan
    color: Color = BLACK
    box: Rect | None = None  # the line's area in the scan (what ERASE paints out)
    font: str = "sans"  # "sans", or "cjk" for scripts the sans font can't show


@dataclass(frozen=True, slots=True)
class ScanTextPlan:
    """How to make a scanned page's recognized text visible and editable: worked out in the
    background (slow: rendering, image analysis), then applied in one quick, undoable step.

    Applying it replaces the invisible text under each line; recognized lines it leaves out
    (over a figure, or too garbled to place) stay invisible and searchable."""

    lines: tuple[ScanLine, ...]
    # scan image (engine id) -> the cleaned image, or None to delete it from the page
    images: tuple[tuple[str, bytes | None], ...] = ()
