"""Document-level metadata, properties and embedded files."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(slots=True)
class Metadata:
    """The Info dictionary fields users can edit. Dates are raw PDF date strings."""

    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""
    creator: str = ""
    producer: str = ""
    creation_date: str = ""
    mod_date: str = ""


class EncryptionMethod(Enum):
    NONE = "none"
    RC4_40 = "rc4-40"
    RC4_128 = "rc4-128"
    AES_128 = "aes-128"
    AES_256 = "aes-256"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Permissions:
    print: bool = True
    modify: bool = True
    copy: bool = True
    annotate: bool = True
    fill_forms: bool = True
    accessibility: bool = True
    assemble: bool = True
    print_high_quality: bool = True


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    page_count: int
    pdf_version: str
    encryption: EncryptionMethod = EncryptionMethod.NONE
    permissions: Permissions = field(default_factory=Permissions)
    is_repaired: bool = False
    has_forms: bool = False
    has_signatures: bool = False
    has_javascript: bool = False
    is_tagged: bool = False
    has_xmp: bool = False
    file_size: int | None = None


@dataclass(frozen=True, slots=True)
class EmbeddedFile:
    name: str
    filename: str
    size: int
    description: str = ""


@dataclass(frozen=True, slots=True)
class FontInfo:
    name: str  # base font name without subset prefix
    type: str  # Type1, TrueType, Type0, Type3, ...
    encoding: str
    embedded: bool
    subset: bool
    ref: int  # engine object id


@dataclass(frozen=True, slots=True)
class LayerInfo:
    """An optional-content group (layer) as presented in the layers panel."""

    id: int  # engine-specific handle
    name: str
    visible: bool
    depth: int = 0
    locked: bool = False


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """An image resource of the document (each distinct image once)."""

    ref: int  # engine object id
    width: int
    height: int
    colorspace: str
    pages: tuple[int, ...]  # pages that show it


SPACE_CATEGORIES = (
    "Images",
    "Fonts",
    "Page content",
    "Comments and forms",
    "Structure (tags)",
    "Bookmarks and links",
    "Embedded files",
    "Metadata and thumbnails",
    "Other objects",
    "File overhead",
)


@dataclass(frozen=True, slots=True)
class SpaceUsage:
    """Bytes per category (stored, i.e. compressed, sizes) for an audit report."""

    total: int
    categories: dict[str, int]

    def share(self, category: str) -> float:
        return self.categories.get(category, 0) / self.total if self.total else 0.0
