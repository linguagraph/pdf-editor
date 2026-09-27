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
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Quad, Rect
from pdfeditor.model.metadata import (
    DocumentInfo,
    EmbeddedFile,
    FontInfo,
    ImageInfo,
    LayerInfo,
    Metadata,
    SpaceUsage,
)
from pdfeditor.model.objects import FontChoice, PageObject, ShapeSpec, TextStyle
from pdfeditor.model.outline import Link, OutlineItem
from pdfeditor.model.pages import ImageStamp, PageLabelRule, TextStamp
from pdfeditor.model.redaction import RedactOptions, SanitizeOptions
from pdfeditor.model.text import TableData, TextPage


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
    annotations_flatten: bool = False
    outline_write: bool = False
    metadata_write: bool = False
    xmp: bool = False
    layers: bool = False
    page_ops: bool = False  # insert/delete/reorder/rotate/crop/labels/stamps
    content_edit: bool = False
    redact: bool = False
    ocr: bool = False
    export: bool = False  # tables, image areas, image/font extraction
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


@dataclass(frozen=True, slots=True)
class OptimizeOptions:
    """In-place size reductions (``capabilities.optimize``); unused objects, duplicates and
    compression are handled when saving (``SaveOptions(garbage=4, object_streams=True)``)."""

    image_dpi: int | None = 150  # None: keep image resolution
    downsample_above: float = 1.3  # only images shown above image_dpi * this are downsampled
    jpeg_quality: int = 75  # photos are recompressed as JPEG at this quality
    grayscale: bool = False
    subset_fonts: bool = True
    remove_thumbnails: bool = True
    remove_metadata: bool = False  # document info and XMP


@runtime_checkable
class Page(Protocol):
    """One page. Every coordinate a Page returns (text, search hits, links, annotations) is in
    the *visible* page space: the rotated, CropBox-relative space that ``rect`` describes and
    ``render`` draws, with the origin at the top-left.
    """

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
        """Changes on every mutation of this page; used to invalidate render caches.

        Must be pure bookkeeping (no engine calls): the GUI thread reads it without the session
        lock while a worker may be rendering.
        """
        ...

    def render(self, request: RenderRequest) -> RenderResult: ...

    def text_page(self, with_chars: bool = True) -> TextPage: ...

    def search(self, needle: str, case_sensitive: bool = False) -> list[Quad]:
        """Plain-text search on this page; regex/whole-word are done by the search service."""
        ...

    def links(self) -> list[Link]: ...

    def annotations(self) -> list[AnnotationModel]: ...

    @property
    def pdf_matrix(self) -> Matrix:
        """Maps visible page space to PDF user space (points, origin bottom-left, unrotated).

        Needed for exchange formats such as XFDF that use raw PDF coordinates.
        """
        ...

    # Optional (``capabilities.annotations_write``). Annotations are identified by ``id``
    # (engine object id) with ``name`` (/NM) as a stable fallback that survives delete + re-add.
    def add_annotation(self, model: AnnotationModel) -> AnnotationModel:
        """Create an annotation; returns it as stored (with ``id`` and ``name`` filled in)."""
        ...

    def update_annotation(self, model: AnnotationModel) -> AnnotationModel:
        """Overwrite geometry and properties of the annotation ``model`` identifies."""
        ...

    def delete_annotation(self, annot_id: int | None, name: str = "") -> None: ...

    def annotation_order(self) -> list[int]:
        """Annotation ids in drawing order (last = on top)."""
        ...

    def set_annotation_order(self, ids: Sequence[int]) -> None: ...

    def flatten_annotations(self, ids: Sequence[int] | None = None) -> int:
        """Burn annotations (all, or ``ids``) into the page content; returns how many."""
        ...

    # Optional (``capabilities.page_ops``); visible-space geometry as everywhere else.
    def set_rotation(self, degrees: int) -> None:
        """Set the page's own /Rotate (persistent, unlike view rotation)."""
        ...

    def set_crop(self, rect: Rect) -> None:
        """Crop the page to ``rect`` (current visible space)."""
        ...

    def stamp_text(self, stamp: TextStamp) -> None: ...

    def stamp_image(self, stamp: ImageStamp) -> None: ...

    def fill_background(self, color: Color, opacity: float = 1.0) -> None: ...

    # Optional (``capabilities.content_edit``). Object keys are valid for one page revision.
    def content_objects(self) -> list[PageObject]:
        """Editable content: text blocks, images, form XObjects and vector paths."""
        ...

    def delete_objects(self, keys: Sequence[str]) -> None: ...

    def transform_objects(self, keys: Sequence[str], matrix: Matrix) -> None:
        """Move/resize objects by ``matrix`` (visible page space). Text blocks are re-typeset
        at their new place with their own style."""
        ...

    def replace_text(
        self, key: str, text: str, style: TextStyle | None = None
    ) -> FontChoice | None:
        """Replace a text block's content (empty text deletes it). Returns the font used."""
        ...

    def add_text(self, rect: Rect, text: str, style: TextStyle) -> FontChoice: ...

    def add_shape(self, spec: ShapeSpec) -> None: ...

    def image_data(self, key: str) -> tuple[bytes, str]:
        """Encoded image bytes and file extension ("png", "jpeg", ...)."""
        ...

    def replace_image(self, key: str, data: bytes) -> None: ...

    def font_program(self, name: str) -> bytes | None:
        """The embedded font program behind a text style's font name (for an editing preview
        that looks like the page); None if the font isn't embedded."""
        ...

    # Optional (``capabilities.ocr``). Two steps so recognition can run in a background job.
    def ocr_text_layer(
        self, language: str, dpi: int, tessdata: Path, preprocess: bool = False
    ) -> bytes:
        """Recognize the page and return an invisible-text layer (read-only; slow)."""
        ...

    def add_text_layer(self, layer: bytes) -> None:
        """Overlay a layer from :meth:`ocr_text_layer` so the page becomes searchable."""
        ...

    # Optional (``capabilities.export``).
    def find_tables(self) -> list[TableData]:
        """Tables detected from ruling lines and text alignment (visible page space)."""
        ...

    def image_areas(self) -> list[Rect]:
        """Where images are shown on the page, in drawing order (visible page space)."""
        ...

    # Optional (``capabilities.redact``). Marks are REDACT annotations (add_annotation).
    def apply_redactions(self, ids: Sequence[int] | None, options: RedactOptions) -> int:
        """Permanently remove content under redaction marks (all, or ``ids``). Returns the
        number of marks applied; the marks themselves are replaced by their fill boxes."""
        ...


@runtime_checkable
class Document(Protocol):
    @property
    def path(self) -> Path | None: ...

    @property
    def page_count(self) -> int: ...

    @property
    def is_dirty(self) -> bool: ...

    def page(self, index: int) -> Page:
        """Page handle. Like ``page_count`` and ``Page.revision``, this must not call into the
        engine, so it's safe without the session lock; every other method needs the lock."""
        ...

    def info(self) -> DocumentInfo: ...

    def metadata(self) -> Metadata: ...

    def set_metadata(self, meta: Metadata) -> None: ...

    def xmp(self) -> str: ...

    def set_xmp(self, xml: str) -> None: ...

    def outline(self) -> list[OutlineItem]: ...

    def set_outline(self, items: Sequence[OutlineItem]) -> None: ...

    def page_label(self, index: int) -> str: ...

    def embedded_files(self) -> list[EmbeddedFile]: ...

    def extract_embedded_file(self, name: str) -> bytes: ...

    def layers(self) -> list[LayerInfo]:
        """Optional-content groups in UI order (empty if the document has none)."""
        ...

    def set_layer_visible(self, layer_id: int, visible: bool) -> None:
        """Change view visibility of a layer (does not make the document dirty)."""
        ...

    def fonts(self) -> list[FontInfo]: ...

    # Optional (``capabilities.export``).
    def extract_font(self, ref: int) -> tuple[str, bytes]:
        """An embedded font program as (file name with extension, bytes)."""
        ...

    def images(self) -> list[ImageInfo]: ...

    def extract_image(self, ref: int) -> tuple[bytes, str]:
        """Encoded image bytes and file extension ("png", "jpeg", ...)."""
        ...

    def save(self, path: Path | None = None, options: SaveOptions | None = None) -> Path:
        """Save safely (temp file + verify + atomic replace). ``path=None`` saves in place.

        After a successful save the document is bound to the saved path.
        """
        ...

    def to_bytes(self, options: SaveOptions | None = None) -> bytes: ...

    def copy(self) -> Document:
        """An independent in-memory copy of the current state (same password, no path)."""
        ...

    # Optional (``capabilities.optimize``).
    def space_usage(self) -> SpaceUsage:
        """Where the bytes go, by category (for the current in-memory objects)."""
        ...

    def optimize(self, options: OptimizeOptions) -> None:
        """Downsample/recompress images, subset fonts, drop thumbnails/metadata (in place)."""
        ...

    # Optional (``capabilities.page_ops``). Bookmarks and links follow moved pages; bookmarks to
    # removed pages are dropped; page-label rules are kept by page index.
    def select_pages(self, order: Sequence[int]) -> None:
        """Keep exactly ``order`` (reorder, delete, or duplicate by repeating an index)."""
        ...

    def insert_blank_page(self, at: int, width: float, height: float) -> None: ...

    def insert_pages(self, source: Document, pages: Sequence[int], at: int) -> None:
        """Copy pages (with annotations and links) from another open document."""
        ...

    def insert_image_page(self, at: int, image: bytes) -> None: ...

    def page_label_rules(self) -> list[PageLabelRule]: ...

    def set_page_label_rules(self, rules: Sequence[PageLabelRule]) -> None: ...

    def sanitize(self, options: SanitizeOptions) -> list[str]:
        """Remove hidden or sensitive information; returns what was removed, for the report.
        Save with ``SaveOptions(garbage=4)`` afterwards so nothing stays in unused objects."""
        ...

    def can_save_incrementally(self) -> bool:
        """True if ``save(options=SaveOptions(incremental=True))`` to ``path`` can work."""
        ...

    def load_state(self, data: bytes) -> None:
        """Replace the whole document with a snapshot made by :meth:`to_bytes` (used by undo).

        Keeps ``path`` and the password. Afterwards the document may no longer be backed by its
        file, so incremental save may become unavailable.
        """
        ...

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

    def text_width(self, text: str, font: str, size: float) -> float:
        """Width in points of ``text`` in a base-14 ``font`` (for aligning stamps)."""
        ...
