"""PyMuPDF (MuPDF) engine backend. The only package allowed to import ``pymupdf``."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from pdfeditor.engine.base import Capabilities, PasswordCallback
from pdfeditor.engine.mupdf.document import MuDocument, open_document
from pdfeditor.engine.mupdf.page import MuPage

__all__ = ["MuDocument", "MuPDFEngine", "MuPage", "create_engine"]

# Keep MuPDF's C-level warnings out of stderr; they're available via pymupdf.TOOLS.
pymupdf.TOOLS.mupdf_display_errors(False)


class MuPDFEngine:
    name = "mupdf"
    capabilities = Capabilities(
        render=True,
        text_extraction=True,
        search=True,
        incremental_save=True,
        annotations_read=True,
        annotations_write=True,
        annotations_flatten=True,
        page_ops=True,
        content_edit=True,
        redact=True,
        ocr=True,
        export=True,
        optimize=True,
        encrypt=True,
        outline_write=True,
        metadata_write=True,
        xmp=True,
        layers=True,
    )

    @property
    def version(self) -> str:
        return f"PyMuPDF {pymupdf.VersionBind} (MuPDF {pymupdf.VersionFitz})"

    def open(
        self, source: Path | bytes, password: str | PasswordCallback | None = None
    ) -> MuDocument:
        return open_document(source, password)

    def new_document(self) -> MuDocument:
        return MuDocument(pymupdf.open(), None, None)

    def text_width(self, text: str, font: str, size: float) -> float:
        from pdfeditor.engine.mupdf.pages import text_width

        return text_width(text, font, size)

    def standard_font_program(self, base_font: str) -> bytes | None:
        code = standard_font_code(base_font)
        return bytes(pymupdf.Font(code).buffer) if code else None


_FAMILIES = (
    (("courier", "couriernew", "mono"), ("cour", "cobo", "coit", "cobi")),
    (("times", "timesnewroman", "roman", "serif"), ("tiro", "tibo", "tiit", "tibi")),
    (("helvetica", "arial", "helv", "sans"), ("helv", "hebo", "heit", "hebi")),
)


def standard_font_code(base_font: str) -> str | None:
    """MuPDF's built-in font for a base-14 name or a common metric-compatible alias."""
    name = base_font.split("+", 1)[-1].lower().replace(" ", "")
    if name.startswith("symbol"):
        return "symb"
    if name.startswith("zapfdingbats") or name.startswith("dingbats"):
        return "zadb"
    style = name.replace("-", ",").split(",", 1)[1] if ("-" in name or "," in name) else ""
    bold = "bold" in style or "black" in style
    italic = "italic" in style or "oblique" in style
    family_name = name.replace("-", ",").split(",", 1)[0].removesuffix("mt").removesuffix("psmt")
    for aliases, codes in _FAMILIES:
        if family_name.removesuffix("ps") in aliases or family_name in aliases:
            return codes[(2 if italic else 0) + (1 if bold else 0)]
    return None


def create_engine() -> MuPDFEngine:
    return MuPDFEngine()
