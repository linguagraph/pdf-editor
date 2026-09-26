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


def create_engine() -> MuPDFEngine:
    return MuPDFEngine()
