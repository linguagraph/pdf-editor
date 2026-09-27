"""Engine-neutral annotation model."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Point, Quad, Rect


class AnnotationType(Enum):
    TEXT = "Text"  # sticky note
    FREE_TEXT = "FreeText"
    LINE = "Line"
    SQUARE = "Square"
    CIRCLE = "Circle"
    POLYGON = "Polygon"
    POLYLINE = "PolyLine"
    HIGHLIGHT = "Highlight"
    UNDERLINE = "Underline"
    SQUIGGLY = "Squiggly"
    STRIKEOUT = "StrikeOut"
    STAMP = "Stamp"
    CARET = "Caret"
    INK = "Ink"
    POPUP = "Popup"
    FILE_ATTACHMENT = "FileAttachment"
    REDACT = "Redact"
    LINK = "Link"
    WIDGET = "Widget"
    OTHER = "Other"

    @classmethod
    def from_pdf_name(cls, name: str) -> AnnotationType:
        for member in cls:
            if member.value == name:
                return member
        return cls.OTHER

    @property
    def is_markup(self) -> bool:
        return self in _TEXT_MARKUP

    @property
    def is_comment(self) -> bool:
        """Shown in the comments panel (excludes links, widgets, popups)."""
        return self not in {AnnotationType.LINK, AnnotationType.WIDGET, AnnotationType.POPUP}


_TEXT_MARKUP = {
    AnnotationType.HIGHLIGHT,
    AnnotationType.UNDERLINE,
    AnnotationType.SQUIGGLY,
    AnnotationType.STRIKEOUT,
}


class ReviewState(Enum):
    NONE = "None"
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    CANCELLED = "Cancelled"
    COMPLETED = "Completed"


@dataclass(slots=True)
class AnnotationModel:
    """A snapshot of one annotation's editable properties.

    ``id`` is the engine's stable identifier (the PDF object number for MuPDF), or ``None`` for an
    annotation that hasn't been created in the document yet.
    """

    type: AnnotationType
    page_index: int
    rect: Rect
    id: int | None = None
    name: str = ""  # /NM unique name
    contents: str = ""
    author: str = ""
    subject: str = ""
    created: datetime | None = None
    modified: datetime | None = None
    color: Color | None = None  # stroke
    fill: Color | None = None  # interior
    opacity: float = 1.0
    border_width: float = 1.0
    dashes: tuple[float, ...] = ()
    quads: tuple[Quad, ...] = ()  # text markup
    ink: tuple[tuple[Point, ...], ...] = ()  # ink strokes
    vertices: tuple[Point, ...] = ()  # line (2 pts) / polygon / polyline
    line_endings: tuple[str, str] = ("None", "None")
    font_size: float = 11.0
    text_color: Color | None = None  # FreeText
    icon: str = ""  # Text/Stamp/FileAttachment icon name
    in_reply_to: int | None = None
    state: ReviewState = ReviewState.NONE
    flags: int = 0
    locked: bool = False
    file_name: str = ""  # FileAttachment: attached file's name
    file_data: bytes | None = None  # FileAttachment: contents (only needed to create one)
    overlay_text: str = ""  # Redact: text shown in the box after the redaction is applied
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_reply(self) -> bool:
        return self.in_reply_to is not None


# Standard stamp names (PDF 32000 12.5.6.12), in the order MuPDF numbers them.
STANDARD_STAMPS = (
    "Approved",
    "AsIs",
    "Confidential",
    "Departmental",
    "Experimental",
    "Expired",
    "Final",
    "ForComment",
    "ForPublicRelease",
    "NotApproved",
    "NotForPublicRelease",
    "Sold",
    "TopSecret",
    "Draft",
)

NOTE_ICONS = ("Note", "Comment", "Key", "Help", "NewParagraph", "Paragraph", "Insert")

# Annotation flags (PDF 32000 12.5.3)
FLAG_HIDDEN = 2
FLAG_PRINT = 4
FLAG_NO_ZOOM = 8
FLAG_NO_ROTATE = 16
FLAG_LOCKED = 128


def transformed(model: AnnotationModel, m: Matrix) -> AnnotationModel:
    """A copy of ``model`` with every geometry field mapped through ``m`` (move/resize)."""
    out = copy.deepcopy(model)
    out.rect = model.rect.transform(m)
    out.quads = tuple(q.transform(m) for q in model.quads)
    out.ink = tuple(tuple(p.transform(m) for p in stroke) for stroke in model.ink)
    out.vertices = tuple(p.transform(m) for p in model.vertices)
    return out
