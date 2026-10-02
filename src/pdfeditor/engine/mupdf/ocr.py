"""OCR for the MuPDF backend (MuPDF's built-in Tesseract; only language data is needed).

Two steps so the slow part can run in a background job:

* :func:`text_layer` (read-only): render the page, OCR it into a searchable-PDF page, and strip
  the page image from it, leaving only Tesseract's invisible, exactly placed text.
* :func:`add_text_layer`: overlay that text-only page onto the original page.

Deskewing straightens only the image Tesseract sees; the recognized text is then rotated back
(a ``cm`` around the layer's content) so it lies over the glyphs of the page as scanned.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

from pdfeditor.engine.base import EngineError
from pdfeditor.engine.contentstream.parser import Name, parse, write
from pdfeditor.engine.deskew import estimate_skew, rotate_about_center
from pdfeditor.engine.mupdf.flatten import flatten_inserted_forms

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.page import MuPage


def text_layer(
    page: MuPage,
    language: str,
    dpi: int,
    tessdata: Path,
    preprocess: bool = False,
    deskew: bool = False,
) -> bytes:
    """A one-page PDF (visible page orientation) holding only the invisible OCR text."""
    pix = page.fz.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
    angle = 0.0
    if preprocess or deskew:
        pix, angle = _prepare(pix, preprocess, deskew)
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
        content = write(ops)
        if angle:
            content = _rotated(content, angle, op.rect)
        ocr.update_stream(xref, content)
        ocr.xref_set_key(op.xref, "Contents", f"{xref} 0 R")
        for entry in op.get_images(full=True):
            ocr.xref_set_key(op.xref, f"Resources/XObject/{entry[7]}", "null")
        return bytes(ocr.tobytes(garbage=4, deflate=True))
    finally:
        ocr.close()


def _prepare(pix: pymupdf.Pixmap, preprocess: bool, deskew: bool) -> tuple[pymupdf.Pixmap, float]:
    """The image for recognition only (the page is unchanged): optionally straightened, then
    grayscaled, lightly denoised and binarized. Returns it with the skew angle removed."""
    from PIL import Image, ImageFilter, ImageOps

    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    angle = estimate_skew(img) if deskew else 0.0
    if angle:
        img = rotate_about_center(img, -angle)
    if preprocess:
        gray = ImageOps.autocontrast(img.convert("L").filter(ImageFilter.MedianFilter(3)))
        img = gray.point(lambda v: 255 if v > 160 else 0).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return pymupdf.Pixmap(out.getvalue()), angle


def _rotated(content: bytes, angle: float, rect: pymupdf.Rect) -> bytes:
    """Wrap ``content`` so it turns ``angle`` degrees counterclockwise about the page center,
    undoing the deskew. PDF space is y-up, so a counterclockwise turn on screen is the
    standard mathematical rotation there."""
    rad = math.radians(angle)
    cos, sin = math.cos(rad), math.sin(rad)
    cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    e = cx - (cos * cx - sin * cy)
    f = cy - (sin * cx + cos * cy)
    cm = " ".join(f"{v:.6f}" for v in (cos, sin, -sin, cos, e, f))
    return b"q " + cm.encode() + b" cm " + content + b" Q"


def add_text_layer(page: MuPage, layer: bytes) -> None:
    src = pymupdf.open("pdf", layer)
    try:
        fz = page.fz
        # The layer is in visible orientation; counter-rotate it onto the unrotated page.
        target = (fz.rect * fz.derotation_matrix).normalize()
        before = set(fz.get_contents())
        fz.show_pdf_page(target, src, 0, rotate=fz.rotation, overlay=True, keep_proportion=False)
        # Inline the page-sized form show_pdf_page draws through: left as an object, the Edit
        # tool would let it be selected and dragged (moving the invisible text, nothing else).
        flatten_inserted_forms(fz, before)
    finally:
        src.close()
    page._doc.mark_page_changed(page.index)
