"""Render document pages onto a QPrinter (real printer, PDF file or print preview)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QImage, QPageLayout, QPainter
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
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
    copies: int = 1
    collate: bool = True  # 1,2,1,2 rather than 1,1,2,2


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


@dataclass(frozen=True, slots=True)
class Placement:
    rotation: int  # 0 or 90 degrees
    factor: float  # page size multiplier (1.0 = actual size)


def place_page(
    page_w: float, page_h: float, area_w_pt: float, area_h_pt: float, options: PrintOptions
) -> Placement:
    """How a page of ``page_w`` x ``page_h`` points sits on a printable area of
    ``area_w_pt`` x ``area_h_pt`` points. Shared by printing and the preview, so they agree."""
    paper_landscape = area_w_pt > area_h_pt
    rotation = 90 if options.auto_rotate and (page_w > page_h) != paper_landscape else 0
    rw, rh = rotated_size(page_w, page_h, rotation)
    fit = min(area_w_pt / rw, area_h_pt / rh)
    factor = {Scaling.FIT: fit, Scaling.ACTUAL: 1.0, Scaling.SHRINK: min(1.0, fit)}[options.scaling]
    return Placement(rotation, factor)


def sheet_sequence(count: int, copies: int = 1, collate: bool = True) -> list[int]:
    """Positions (into ``options.pages``) of the sheets to print, in order, for ``copies``
    copies: 0,1,0,1 collated, 0,0,1,1 otherwise."""
    copies = max(1, copies)
    if collate:
        return list(range(count)) * copies
    return [n for n in range(count) for _ in range(copies)]


def paint_sheet(
    painter: QPainter,
    session: DocumentSession,
    index: int,
    options: PrintOptions,
    area: QSizeF,
    resolution: int,
) -> None:
    """Paint document page ``index`` onto the printable ``area`` (device pixels at
    ``resolution`` dpi) whose top-left is the painter's origin.

    One routine serves the printer and the preview, so the preview shows what will print.
    The engine is touched only under the session lock and nothing engine-side is kept.
    """
    dpi = min(resolution, MAX_PRINT_DPI)
    device_per_render_px = resolution / dpi
    vector_ok = not (options.as_image or options.grayscale or not options.annotations)
    with session.lock:
        page = session.document.page(index)
        page_rect = page.rect
        w, h = page_rect.width, page_rect.height
        placement = place_page(
            w, h, area.width() / resolution * 72, area.height() / resolution * 72, options
        )
        svg = page.to_svg() if vector_ok else ""
        renderer = vector_svg(svg) if svg else None
        result = (
            page.render(
                RenderRequest(
                    matrix=view_matrix(page_rect, placement.rotation, dpi / 72 * placement.factor),
                    color=ColorMode.GRAY if options.grayscale else ColorMode.RGB,
                    annotations=options.annotations,
                )
            )
            if renderer is None
            else None
        )
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # pages larger than the printable area are cut off there, never spilling into margins
        painter.setClipRect(QRectF(0, 0, area.width(), area.height()))
        if renderer is not None:
            # device pixels for the page at this scaling, centred like the raster path
            k = resolution / 72 * placement.factor
            pw, ph = w * k, h * k
            painter.translate(QPointF(area.width() / 2, area.height() / 2))
            painter.rotate(placement.rotation)
            renderer.render(painter, QRectF(-pw / 2, -ph / 2, pw, ph))
        else:
            assert result is not None
            image = to_qimage(result)
            target_w = image.width() * device_per_render_px
            target_h = image.height() * device_per_render_px
            x = (area.width() - target_w) / 2
            y = (area.height() - target_h) / 2
            painter.drawImage(QRectF(x, y, target_w, target_h), image)
    finally:
        painter.restore()


def print_pages(
    session: DocumentSession,
    printer: QPrinter,
    options: PrintOptions,
    token: CancelToken | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Paint ``options.pages`` onto ``printer``; returns the number of sheets printed.

    Runs on the calling (GUI) thread: printer painting must not move to a worker thread on all
    platforms. Callers keep the UI alive through ``progress``.
    """
    if not options.pages:
        return 0
    copies = max(1, options.copies)
    if printer.supportsMultipleCopies():
        printer.setCopyCount(copies)
        printer.setCollateCopies(options.collate)
        sequence = list(range(len(options.pages)))
    else:  # PDF output has no copies: print them one after another
        sequence = sheet_sequence(len(options.pages), copies, options.collate)
    resolution = printer.resolution()
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("could not start printing")
    printed = 0
    try:
        rect = printer.pageLayout().paintRectPixels(resolution)
        area = QSizeF(rect.width(), rect.height())
        for n, position in enumerate(sequence):
            if token is not None and token.cancelled:
                break
            if n:
                printer.newPage()
            paint_sheet(painter, session, options.pages[position], options, area, resolution)
            printed += 1
            if progress is not None:
                progress(n + 1, len(sequence))
    finally:
        painter.end()
    return printed


def render_sheet_image(
    session: DocumentSession,
    index: int,
    options: PrintOptions,
    layout: QPageLayout,
    dpi: int,
) -> QImage:
    """The whole sheet of paper (white, margins included) as it will print, at ``dpi``.

    Safe on a worker thread: it needs only a copy of the page layout, never the printer.
    """
    scale = dpi / 72
    full = layout.fullRectPoints()
    image = QImage(
        max(1, round(full.width() * scale)),
        max(1, round(full.height() * scale)),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(Qt.GlobalColor.white)
    paint = layout.paintRectPoints()
    painter = QPainter(image)
    try:
        painter.translate(paint.left() * scale, paint.top() * scale)
        paint_sheet(
            painter,
            session,
            index,
            options,
            QSizeF(paint.width() * scale, paint.height() * scale),
            dpi,
        )
    finally:
        painter.end()
    return image


def available_printers() -> tuple[list[str], str | None]:
    """Installed printer names and the default one. The only place printers are queried, so
    tests replace this one function."""
    names = list(QPrinterInfo.availablePrinterNames())
    default = QPrinterInfo.defaultPrinterName() or None
    return names, default if default in names else None


def printer_for(name: str | None) -> QPrinter:
    """A printer by name; None means "Save as PDF" (the file name is set when printing)."""
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    if name is None:
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setResolution(150)
    else:
        printer.setPrinterName(name)
    return printer


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
