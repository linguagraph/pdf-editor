"""Engine-neutral interfaces every PDF backend implements.

Everything above the engine layer depends only on these Protocols and on ``pdfeditor.model``.
Backends advertise optional features through :class:`Capabilities`; callers must check a flag
before using the matching optional method.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pdfeditor.model.annotations import AnnotationModel
from pdfeditor.model.geometry import Matrix, Quad, Rect
from pdfeditor.model.metadata import DocumentInfo, EmbeddedFile, FontInfo, Metadata
from pdfeditor.model.outline import Link, OutlineItem
from pdfeditor.model.text import TextPage


class EngineError(Exception):
    """Base class for engine failures."""


class OpenError(EngineError):
    """The file couldn't be opened (not a PDF, unrecoverable damage, missing file)."""


class PasswordRequired(OpenError):
    """The document is encrypted and no (or a wrong) password was supplied."""


class SaveError(EngineError):
    """Saving failed; the original file is untouched."""


class UnsupportedFeature(EngineError):
    """The active engine doesn't implement this optional capability."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    render: bool = True
    text_extraction: bool = True
    search: bool = True
    incremental_save: bool = False
    annotations_read: bool = False
    annotations_write: bool = False
    outline_write: bool = False
    metadata_write: bool = False
    xmp: bool = False
    page_ops: bool = False
    content_edit: bool = False
    redact: bool = False
    ocr: bool = False
    encrypt: bool = False
    optimize: bool = False


@dataclass(frozen=True, slots=True)
class PageBoxes:
    media: Rect
    crop: Rect
    trim: Rect | None = None
    bleed: Rect | None = None
    art: Rect | None = None


class ColorMode(Enum):
    RGB = "rgb"
    RGBA = "rgba"
    GRAY = "gray"


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """``matrix`` maps page space (incl. rotation) to pixels; ``clip`` is in page space."""

    matrix: Matrix = field(default_factory=Matrix.identity)
    clip: Rect | None = None
    color: ColorMode = ColorMode.RGBA
    annotations: bool = True


@dataclass(frozen=True, slots=True)
class RenderResult:
    width: int
    height: int
    stride: int
    color: ColorMode
    samples: bytes

    @property
    def channels(self) -> int:
        return {ColorMode.RGB: 3, ColorMode.RGBA: 4, ColorMode.GRAY: 1}[self.color]


@dataclass(frozen=True, slots=True)
class SaveOptions:
    incremental: bool = False
    garbage: int = 3  # 0 = keep unused objects ... 4 = also merge duplicate streams
    deflate: bool = True
    object_streams: bool = False
    clean_content: bool = False


@runtime_checkable
class Page(Protocol):
    @property
    def index(self) -> int: ...

    @property
    def rect(self) -> Rect:
        """Visible page rectangle (CropBox with /Rotate applied), origin at (0, 0)."""
        ...

    @property
    def rotation(self) -> int: ...

    @property
    def boxes(self) -> PageBoxes: ...

    @property
    def revision(self) -> int:
        """Increments on every mutation of this page; used to invalidate render caches."""
        ...

    def render(self, request: RenderRequest) -> RenderResult: ...

    def text_page(self, with_chars: bool = True) -> TextPage: ...

    def search(self, needle: str, case_sensitive: bool = False) -> list[Quad]:
        """Plain-text search on this page; regex/whole-word are done by the search service."""
        ...

    def links(self) -> list[Link]: ...

    def annotations(self) -> list[AnnotationModel]: ...


@runtime_checkable
class Document(Protocol):
    @property
    def path(self) -> Path | None: ...

    @property
    def page_count(self) -> int: ...

    @property
    def is_dirty(self) -> bool: ...

    def page(self, index: int) -> Page: ...

    def info(self) -> DocumentInfo: ...

    def metadata(self) -> Metadata: ...

    def set_metadata(self, meta: Metadata) -> None: ...

    def xmp(self) -> str: ...

    def set_xmp(self, xml: str) -> None: ...

    def outline(self) -> list[OutlineItem]: ...

    def set_outline(self, items: Sequence[OutlineItem]) -> None: ...

    def page_label(self, index: int) -> str: ...

    def embedded_files(self) -> list[EmbeddedFile]: ...

    def fonts(self) -> list[FontInfo]: ...

    def save(self, path: Path | None = None, options: SaveOptions | None = None) -> Path:
        """Save safely (temp file + verify + atomic replace). ``path=None`` saves in place.

        After a successful save the document is bound to the saved path.
        """
        ...

    def to_bytes(self, options: SaveOptions | None = None) -> bytes: ...

    def close(self) -> None: ...


PasswordCallback = Callable[[int], str | None]
"""Called with the attempt number (1, 2, ...); returns a password or ``None`` to give up."""


@runtime_checkable
class Engine(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> Capabilities: ...

    def open(
        self, source: Path | bytes, password: str | PasswordCallback | None = None
    ) -> Document: ...

    def new_document(self) -> Document: ...
