"""Editable page content (not annotations): text blocks, images, form XObjects, paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pdfeditor.model.color import BLACK, Color
from pdfeditor.model.fonts import FontRef
from pdfeditor.model.geometry import Point, Rect


class ObjectType(Enum):
    TEXT = "text"
    IMAGE = "image"
    FORM = "form"  # a form XObject drawn as a unit (e.g. a logo made of paths)
    PATH = "path"


class Align(Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


@dataclass(frozen=True, slots=True)
class TextStyle:
    font: str = "Helvetica"  # font name as found in the document (or a standard family)
    size: float = 11.0
    color: Color = BLACK
    bold: bool = False
    italic: bool = False
    align: Align = Align.LEFT
    line_height: float = 1.2  # multiple of the font size
    # extra space after every glyph / after every word space, in points (PDF Tc / Tw)
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    # which font to typeset with, if not the one named in ``font`` (set by the font picker)
    font_ref: FontRef | None = None


@dataclass(frozen=True, slots=True)
class PageObject:
    """One editable thing on a page. ``key`` is valid for the current page revision only."""

    type: ObjectType
    key: str
    bbox: Rect  # visible page space
    text: str = ""  # TEXT
    style: TextStyle | None = None  # TEXT: dominant style of the block
    editable: bool = True
    reason: str = ""  # why it isn't editable (rotated text, Type3 font, ...)
    image_size: tuple[int, int] = (0, 0)  # IMAGE: pixels
    stroke: bool = False  # PATH
    fill: bool = False  # PATH


@dataclass(frozen=True, slots=True)
class FontChoice:
    """How edited text was set: the document's own font, a standard font, or a substitute."""

    name: str
    embedded_reused: bool = False
    substituted: bool = False  # the original font couldn't render the new text
    missing: str = ""  # characters the requested font (font_ref) couldn't show, if substituted


class ShapeKind(Enum):
    RECTANGLE = "rectangle"
    ELLIPSE = "ellipse"
    LINE = "line"


@dataclass(frozen=True, slots=True)
class ShapeSpec:
    kind: ShapeKind
    rect: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))  # RECTANGLE / ELLIPSE
    start: Point = field(default_factory=lambda: Point(0, 0))  # LINE
    end: Point = field(default_factory=lambda: Point(0, 0))  # LINE
    stroke: Color | None = BLACK
    fill: Color | None = None
    width: float = 1.0


def family_of(font_name: str) -> str:
    """Standard family ("sans", "serif" or "mono") closest to a PDF font name."""
    name = font_name.lower()
    if any(k in name for k in ("courier", "mono", "consol", "typewriter")):
        return "mono"
    serif = ("times", "serif", "roman", "georgia", "garamond", "cambria", "minion", "book")
    if any(k in name for k in serif):
        return "sans" if "sans" in name else "serif"
    return "sans"
