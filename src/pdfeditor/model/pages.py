"""Page-level editing models: labels and content stamps."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pdfeditor.model.color import BLACK, Color
from pdfeditor.model.geometry import Point, Rect


class LabelStyle(Enum):
    DECIMAL = "D"  # 1, 2, 3
    ROMAN_UPPER = "R"  # I, II, III
    ROMAN_LOWER = "r"  # i, ii, iii
    ALPHA_UPPER = "A"  # A, B, C
    ALPHA_LOWER = "a"  # a, b, c
    NONE = ""  # prefix only


@dataclass(frozen=True, slots=True)
class PageLabelRule:
    """Pages from ``start`` (0-based) until the next rule are labelled ``prefix`` + number."""

    start: int
    style: LabelStyle = LabelStyle.DECIMAL
    prefix: str = ""
    first: int = 1


FONTS = ("helv", "hebo", "tiro", "tibo", "cour", "cobo")  # PDF base-14 families (+ bold)


@dataclass(frozen=True, slots=True)
class TextStamp:
    """One line of text drawn into the page content (header, footer, watermark, Bates)."""

    text: str
    origin: Point  # baseline start, visible page space
    font_size: float = 10.0
    font: str = "helv"
    color: Color = BLACK
    opacity: float = 1.0
    angle: float = 0.0  # visible, counter-clockwise degrees
    on_top: bool = True


@dataclass(frozen=True, slots=True)
class ImageStamp:
    image: bytes
    rect: Rect  # visible page space
    opacity: float = 1.0
    on_top: bool = True
    keep_proportion: bool = True
