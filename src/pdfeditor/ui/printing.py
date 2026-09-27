"""Render document pages onto a QPrinter (real printer, PDF file or print preview)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QByteArray, QPointF, QRectF
from PySide6.QtGui import QPageLayout, QPainter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtSvg import QSvgRenderer

from pdfeditor.core.jobs import CancelToken
from pdfeditor.core.layout import rotated_size, view_matrix
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import ColorMode, RenderRequest
from pdfeditor.ui.view.renderer import to_qimage

# Pages print as vectors (the engine's SVG drawn with QtSvg), so text and line art stay sharp at
# any printer resolution. Grayscale output, printing without comments, and pages using SVG
# features QtSvg lacks (masks, blend modes, patterns, filters) print as images instead:
# 300 dpi keeps an A4 page around 26 MB while staying crisp for text.
MAX_PRINT_DPI = 300
_UNSUPPORTED_SVG = re.compile(r"<mask\b|mix-blend-mode|<pattern\b|<filter\b")
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_GLYPH_SCALE = 1000.0


class Scaling(Enum):
    FIT = "fit"  # scale every page to the printable area
    ACTUAL = "actual"  # 100%; oversized pages are clipped
    SHRINK = "shrink"  # actual size, but shrink pages that don't fit


@dataclass(frozen=True, slots=True)
class PrintOptions:
    pages: tuple[int, ...]
    scaling: Scaling = Scaling.SHRINK
    auto_rotate: bool = True  # turn landscape pages to match portrait paper (and vice versa)
    annotations: bool = True
    grayscale: bool = False
    as_image: bool = False  # force the raster path (Acrobat's "Print as image")


def qt_friendly_svg(svg: str) -> str:
    """MuPDF defines glyph outlines in 1-unit em space and scales each use by the font size;
    QtSvg loses the detail at that scale and draws blobs. Enlarge the outlines 1000x and shrink
    the uses to match (same geometry)."""

    def outline(m: re.Match[str]) -> str:
        d = _NUMBER.sub(lambda n: f"{float(n.group()) * _GLYPH_SCALE:.3f}", m.group(2))
        return f'{m.group(1)}d="{d}"'

    def use(m: re.Match[str]) -> str:
        a, b, c, d, e, f = (float(v) for v in m.group(2).split(","))
        k = _GLYPH_SCALE
        values = ",".join(f"{v:.6g}" for v in (a / k, b / k, c / k, d / k, e, f))
        return f'{m.group(1)}transform="matrix({values})"'

    svg = re.sub(r'(<path id="font_[^"]*" )d="([^"]*)"', outline, svg)
    return re.sub(r'(<use [^>]*xlink:href="#font_[^"]*" )transform="matrix\(([^)]*)\)"', use, svg)


def vector_svg(svg: str) -> QSvgRenderer | None:
    """A renderer for the page, or None when it needs the raster path."""
    if _UNSUPPORTED_SVG.search(svg):
        return None
    renderer = QSvgRenderer(QByteArray(qt_friendly_svg(svg).encode("utf-8")))
    return renderer if renderer.isValid() else None


def print_pages(
    session: DocumentSession,
    printer: QPrinter,
    options: PrintOptions,
    token: CancelToken | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Paint ``options.pages`` onto ``printer``; returns the number of pages printed.

    Runs on the calling (GUI) thread: printer painting must not move to a worker thread on all
    platforms. Callers keep the UI alive through ``progress``.
    """
    if not options.pages:
        return 0
    dpi = min(printer.resolution(), MAX_PRINT_DPI)
    device_per_render_px = printer.resolution() / dpi
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("could not start printing")
    printed = 0
    vector_ok = not (options.as_image or options.grayscale or not options.annotations)
    try:
        area = printer.pageLayout().paintRectPixels(printer.resolution())
        paper_landscape = area.width() > area.height()
        for n, index in enumerate(options.pages):
            if token is not None and token.cancelled:
                break
            if n:
                printer.newPage()
            with session.lock:
                page = session.document.page(index)
                page_rect = page.rect
                w, h = page_rect.width, page_rect.height
                rotation = 90 if options.auto_rotate and (w > h) != paper_landscape else 0
                rw, rh = rotated_size(w, h, rotation)
                fit = min(
                    area.width() / (rw / 72 * printer.resolution()),
                    area.height() / (rh / 72 * printer.resolution()),
                )
                factor = {
                    Scaling.FIT: fit,
                    Scaling.ACTUAL: 1.0,
                    Scaling.SHRINK: min(1.0, fit),
                }[options.scaling]
                scale = dpi / 72 * factor  # render pixels per point
                svg = page.to_svg() if vector_ok else ""
                renderer = vector_svg(svg) if svg else None
                result = (
                    page.render(
                        RenderRequest(
                            matrix=view_matrix(page_rect, rotation, scale),
                            color=ColorMode.GRAY if options.grayscale else ColorMode.RGB,
                            annotations=options.annotations,
                        )
                    )
                    if renderer is None
                    else None
                )
            if renderer is not None:
                # device pixels for the page at this scaling, centred like the raster path
                k = printer.resolution() / 72 * factor
                pw, ph = w * k, h * k
                painter.save()
                painter.translate(QPointF(area.width() / 2, area.height() / 2))
                painter.rotate(rotation)
                renderer.render(painter, QRectF(-pw / 2, -ph / 2, pw, ph))
                painter.restore()
                printed += 1
                if progress is not None:
                    progress(n + 1, len(options.pages))
                continue
            assert result is not None
            image = to_qimage(result)
            target_w = image.width() * device_per_render_px
            target_h = image.height() * device_per_render_px
            x = (area.width() - target_w) / 2
            y = (area.height() - target_h) / 2
            painter.drawImage(QRectF(x, y, target_w, target_h), image)
            printed += 1
            if progress is not None:
                progress(n + 1, len(options.pages))
    finally:
        painter.end()
    return printed


def pdf_printer(path: str, landscape: bool = False) -> QPrinter:
    """A QPrinter that writes a PDF file (tests, and systems without a PDF printer)."""
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    printer.setResolution(150)
    orientation = (
        QPageLayout.Orientation.Landscape if landscape else QPageLayout.Orientation.Portrait
    )
    printer.setPageOrientation(orientation)
    return printer
