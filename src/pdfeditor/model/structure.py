"""Logical structure (tags) and document-level accessibility settings."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

# Standard structure types (PDF 32000-1 14.8.4), for role-map resolution and the tag editor.
STANDARD_TYPES = (
    "Document", "Part", "Art", "Sect", "Div", "BlockQuote", "Caption", "TOC", "TOCI", "Index",
    "NonStruct", "Private", "P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "L", "LI", "Lbl",
    "LBody", "Table", "TR", "TH", "TD", "THead", "TBody", "TFoot", "Span", "Quote", "Note",
    "Reference", "BibEntry", "Code", "Link", "Annot", "Ruby", "Warichu", "Figure", "Formula",
    "Form",
)  # fmt: skip


@dataclass
class StructNode:
    """One structure element. ``ref`` is the engine's object id (stable while the file is
    open); ``role`` is the standard type after the document's role map."""

    ref: int
    type: str
    role: str = ""
    title: str = ""
    alt: str = ""
    actual_text: str = ""
    lang: str = ""
    page_index: int | None = None  # first page with content of this element
    children: list[StructNode] = field(default_factory=list)

    @property
    def standard_type(self) -> str:
        return self.role or self.type

    def walk(self) -> Iterator[StructNode]:
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def heading_level(self) -> int | None:
        t = self.standard_type
        if len(t) == 2 and t[0] == "H" and t[1].isdigit():
            return int(t[1])
        return None


def walk_all(roots: list[StructNode]) -> Iterator[StructNode]:
    for r in roots:
        yield from r.walk()


@dataclass(frozen=True, slots=True)
class AccessibilitySettings:
    language: str = ""  # catalog /Lang, e.g. "en-US"
    display_doc_title: bool = False  # show the title, not the file name, in the title bar
    tab_order_structure: bool = False  # every page with annotations tabs in structure order
