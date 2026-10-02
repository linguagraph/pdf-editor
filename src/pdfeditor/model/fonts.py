"""Font identity: what a piece of text is set in, and what a font file on disk looks like.

Pure data, no I/O: ``services/fonts.py`` reads font files and the catalog; the engine resolves a
:class:`FontRef` to bytes to typeset with.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FontRefKind(Enum):
    STANDARD = "standard"  # one of the base-14 PDF fonts, by name (e.g. "Helvetica")
    DOCUMENT = "document"  # a font already embedded in the document, by its resource name
    FILE = "file"  # an installed or user-picked font file on disk


@dataclass(frozen=True, slots=True)
class FontRef:
    """What font to use, without saying how to render it.

    ``path``/``index`` are set only for ``FILE`` (a .ttc/.otc face is ``index`` within it).
    """

    kind: FontRefKind
    name: str
    path: str = ""
    index: int = 0

    @classmethod
    def standard(cls, name: str) -> FontRef:
        return cls(FontRefKind.STANDARD, name)

    @classmethod
    def document(cls, name: str) -> FontRef:
        return cls(FontRefKind.DOCUMENT, name)

    @classmethod
    def file(cls, path: str, index: int = 0, name: str = "") -> FontRef:
        return cls(FontRefKind.FILE, name or path, path, index)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "name": self.name, "path": self.path, "index": self.index}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FontRef | None:
        """``None`` for anything that isn't a well-formed ``FontRef`` (corrupt settings)."""
        try:
            kind = FontRefKind(data["kind"])
            name = data["name"]
            path = data.get("path", "")
            index = data.get("index", 0)
        except (KeyError, ValueError, TypeError):
            return None
        if not isinstance(name, str) or not isinstance(path, str) or not isinstance(index, int):
            return None
        return cls(kind, name, path, index)


@dataclass(frozen=True, slots=True)
class FontFace:
    """One face of a font file, as read from its ``name``/``OS/2``/``head``/``cmap`` tables."""

    family: str
    style: str  # subfamily name, e.g. "Bold Italic"
    weight: int  # OS/2 usWeightClass (400 = normal, 700 = bold)
    italic: bool
    path: str
    index: int = 0  # the face within a .ttc/.otc; 0 for a single-face file
    embeddable: bool = True
    reason: str = ""  # why not embeddable, e.g. "restricted by fsType"
    scripts: frozenset[str] = frozenset()  # coarse cmap coverage: "latin", "cyrillic", ...
    variable: bool = False  # has an `fvar` table (a variable font)
