"""Bookmarks (outline), destinations and links."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect


@dataclass(frozen=True, slots=True)
class Destination:
    page_index: int
    point: Point | None = None  # top-left of the target view, in page space
    zoom: float | None = None


@dataclass(slots=True)
class OutlineItem:
    title: str
    dest: Destination | None = None
    uri: str | None = None
    children: list[OutlineItem] = field(default_factory=list)
    is_open: bool = False
    color: Color | None = None
    bold: bool = False
    italic: bool = False

    def walk(self, level: int = 1) -> list[tuple[int, OutlineItem]]:
        """Depth-first ``(level, item)`` pairs including self."""
        out = [(level, self)]
        for child in self.children:
            out.extend(child.walk(level + 1))
        return out


def flatten(items: list[OutlineItem]) -> list[tuple[int, OutlineItem]]:
    out: list[tuple[int, OutlineItem]] = []
    for item in items:
        out.extend(item.walk())
    return out


class LinkKind(Enum):
    GOTO = "goto"  # internal page destination
    URI = "uri"
    GOTO_REMOTE = "gotor"  # another file
    LAUNCH = "launch"
    NAMED = "named"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Link:
    rect: Rect
    kind: LinkKind
    dest: Destination | None = None
    uri: str = ""
    file: str = ""
    named: str = ""
