"""OCR for the MuPDF backend (MuPDF's built-in Tesseract; only language data is needed).

Two steps so the slow part can run in a background job:

* :func:`text_layer` (read-only): render the page, OCR it into a searchable-PDF page, and strip
  the page image from it, leaving only Tesseract's invisible, exactly placed text.
* :func:`add_text_layer`: overlay that text-only page onto the original page.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

from pdfeditor.engine.base import EngineError
from pdfeditor.engine.contentstream.parser import Name, parse, write

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.page import MuPage


def text_layer(
    page: MuPage, language: str, dpi: int, tessdata: Path, preprocess: bool = False
) -> bytes:
    """A one-page PDF (visible page orientation) holding only the invisible OCR text."""
    pix = page.fz.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
    if preprocess:
        pix = _preprocess(pix)
    pix.set_dpi(dpi, dpi)
    try:
        data = pix.pdfocr_tobytes(language=language, tessdata=str(tessdata))
    except Exception as exc:
        raise EngineError(f"OCR failed ({language}): {exc}") from exc
    ocr = pymupdf.open("pdf", data)
    try:
        op = ocr[0]
        images = {str(entry[7]) for entry in op.get_images(full=True)}
        ops = [
            o
            for o in parse(op.read_contents())
            if not (
                o.operator == "Do"
                and o.operands
                and isinstance(o.operands[0], Name)
                and str(o.operands[0]) in images
            )
            and o.operator != "BI"
        ]
        xref = ocr.get_new_xref()
        ocr.update_object(xref, "<<>>")
        ocr.update_stream(xref, write(ops))
        ocr.xref_set_key(op.xref, "Contents", f"{xref} 0 R")
        for entry in op.get_images(full=True):
            ocr.xref_set_key(op.xref, f"Resources/XObject/{entry[7]}", "null")
        return bytes(ocr.tobytes(garbage=4, deflate=True))
    finally:
        ocr.close()


def _preprocess(pix: pymupdf.Pixmap) -> pymupdf.Pixmap:
    """Grayscale, light denoise and binarize (for recognition only; the page is unchanged)."""
    from PIL import Image, ImageFilter, ImageOps

    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    img = ImageOps.autocontrast(img.filter(ImageFilter.MedianFilter(3)))
    img = img.point(lambda v: 255 if v > 160 else 0)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return pymupdf.Pixmap(out.getvalue())


def add_text_layer(page: MuPage, layer: bytes) -> None:
    src = pymupdf.open("pdf", layer)
    try:
        fz = page.fz
        # The layer is in visible orientation; counter-rotate it onto the unrotated page.
        target = (fz.rect * fz.derotation_matrix).normalize()
        fz.show_pdf_page(target, src, 0, rotate=fz.rotation, overlay=True, keep_proportion=False)
    finally:
        src.close()
    page._doc.mark_page_changed(page.index)
