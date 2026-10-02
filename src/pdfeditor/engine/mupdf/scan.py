"""Scanned pages in the MuPDF backend: recognize them, and make their recognized text editable.

OCR alone leaves the scan untouched and lays invisible text over it (Tesseract's GlyphLessFont,
render mode 3): searchable, but editing that text puts new glyphs on top of the old picture of
them. Making it editable takes two steps, so the slow one can run in a background job:

* :func:`text_plan` (read-only): measure every recognized line on a render of the page (its
  baseline, ascender height and ink color, which Tesseract doesn't report precisely) and, for
  :attr:`ScanCleanup.ERASE`, paint the lines out of a copy of each full-page scan image with
  the paper color around them.
* :func:`apply_plan`: drop the invisible text under those lines, swap in the cleaned images (or
  stop drawing the scans), and typeset each line as real text in a standard font, scaled to
  the scanned width.

Figures and other images smaller than :data:`SCAN_COVERAGE` of the page are never changed, and
lines over them stay invisible: their picture is in the figure, which isn't erased. So do lines
that can't be placed convincingly (Tesseract sometimes runs several rows into one line).
"""

from __future__ import annotations

import io
import logging
import re
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pymupdf
from PIL import Image, ImageStat

from pdfeditor.engine.contentstream.parser import Name, Operation, parse, write
from pdfeditor.engine.mupdf.content import _remove_text, _write_ops
from pdfeditor.engine.mupdf.pagecopy import _clone_object, _clone_stream
from pdfeditor.model.color import BLACK, Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.scan import (
    SCAN_COVERAGE,
    ScanCleanup,
    ScanInfo,
    ScanLine,
    ScanTextPlan,
)

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.page import MuPage

log = logging.getLogger(__name__)

ZOOM = 3.0  # render scale for measuring lines (216 dpi)
ASCENT = 0.74  # height of capitals and ascenders in the standard sans font, per point of size
LINE_PAD = 0.15  # erase this share of the line height around a recognized line
# a line needing more horizontal scaling than this to match its scanned width is misread
MIN_SCALE, MAX_SCALE = 0.7, 1.4
FIGURE_OVERLAP = 0.3  # lines this much over a smaller image (a figure) stay invisible
MAX_SKEW = 0.09  # lines more than ~5 degrees off horizontal are left alone
MAX_DEPTH = 8  # form XObject nesting followed when looking for scan images
_TEXT_FLAGS = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
_XOBJECT_REF = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R")
_FONTS = {"sans": "helv", "cjk": "cjk"}
# Tesseract reads a round bullet before a capital as "@"
_BULLET = re.compile(r"^@(?=\s*[A-Z])")


# -- finding scans --------------------------------------------------------------------------------
def _scan_images(fz: pymupdf.Page) -> list[dict[str, Any]]:
    """Images (with their placement, unrotated space) that cover most of the page."""
    box = fz.cropbox
    page = pymupdf.Rect(0, 0, box.width, box.height)
    area = page.width * page.height
    if area <= 0:
        return []
    out = []
    for info in fz.get_image_info(xrefs=True):
        visible = pymupdf.Rect(info["bbox"]) & page
        if info.get("xref") and visible.width * visible.height >= SCAN_COVERAGE * area:
            out.append(info)
    return out


def _figures(fz: pymupdf.Page, scans: list[dict[str, Any]]) -> list[pymupdf.Rect]:
    """Where the page shows images that aren't scans (unrotated space)."""
    xrefs = {s["xref"] for s in scans}
    return [
        pymupdf.Rect(i["bbox"]) for i in fz.get_image_info(xrefs=True) if i.get("xref") not in xrefs
    ]


def _over_figure(box: pymupdf.Rect, figures: list[pymupdf.Rect]) -> bool:
    area = box.width * box.height
    return area > 0 and any(
        (r := box & f).width * r.height >= FIGURE_OVERLAP * area for f in figures
    )


def info(page: MuPage) -> ScanInfo:
    fz = page.fz
    box = fz.cropbox
    area = box.width * box.height
    coverage = 0.0
    if area > 0:
        for image in fz.get_image_info():
            r = pymupdf.Rect(image["bbox"]) & pymupdf.Rect(0, 0, box.width, box.height)
            coverage = max(coverage, r.width * r.height / area)
    visible = invisible = 0
    for block in fz.get_text("dict", flags=_TEXT_FLAGS)["blocks"]:
        for line in block.get("lines", ()):
            for span in line["spans"]:
                n = len("".join(span["text"].split()))
                if span.get("alpha", 255) == 0:
                    invisible += n
                else:
                    visible += n
    return ScanInfo(min(coverage, 1.0), visible, invisible)


# -- planning (read-only) -------------------------------------------------------------------------
@dataclass
class _Line:
    text: str
    box: Rect  # visible space
    origin: Point
    size: float  # as Tesseract reported it


def _invisible_lines(fz: pymupdf.Page, to_visible: pymupdf.Matrix) -> list[list[_Line]]:
    """The invisible (recognized) lines of ``fz`` by block, mapped to visible space; lines
    that aren't roughly horizontal there are skipped."""
    lin = pymupdf.Matrix(to_visible.a, to_visible.b, to_visible.c, to_visible.d, 0, 0)
    blocks: list[list[_Line]] = []
    for block in fz.get_text("dict", flags=_TEXT_FLAGS)["blocks"]:
        lines: list[_Line] = []
        for line in block.get("lines", ()):
            spans = [s for s in line["spans"] if s.get("alpha", 255) == 0]
            text = "".join(s["text"] for s in spans).strip()
            if not text:
                continue
            direction = pymupdf.Point(line["dir"]) * lin
            if direction.x <= 0 or abs(direction.y) > MAX_SKEW * direction.x:
                continue
            bbox = pymupdf.Rect(spans[0]["bbox"])
            for s in spans[1:]:
                bbox |= pymupdf.Rect(s["bbox"])
            r = bbox * to_visible
            o = pymupdf.Point(spans[0]["origin"]) * to_visible
            size = max(float(s["size"]) for s in spans) * abs(to_visible.d or to_visible.b)
            lines.append(_Line(text, Rect(r.x0, r.y0, r.x1, r.y1), Point(o.x, o.y), size))
        if lines:
            blocks.append(lines)
    return blocks


def _percentile(histogram: list[int], share: float) -> int:
    total = sum(histogram)
    wanted, seen = total * share, 0
    for value, count in enumerate(histogram):
        seen += count
        if seen >= wanted:
            return value
    return 255


@dataclass
class _Ink:
    top: float  # visible space
    baseline: float
    color: Color


def _measure(render: Image.Image, gray: Image.Image, box: Rect) -> _Ink | None:
    """Where the ink of a line is: the top of its capitals/ascenders, its baseline (the last
    row most letters reach; descenders are sparse) and its color."""
    h = box.height
    left, top = max(0, int(box.x0 * ZOOM)), max(0, int((box.y0 - 0.25 * h) * ZOOM))
    right = min(gray.width, int(box.x1 * ZOOM) + 1)
    bottom = min(gray.height, int((box.y1 + 0.1 * h) * ZOOM) + 1)
    if right - left < 2 or bottom - top < 4:
        return None
    crop = gray.crop((left, top, right, bottom))
    hist = crop.histogram()
    dark, paper = _percentile(hist, 0.02), _percentile(hist, 0.5)
    if paper - dark < 40:
        return None  # no clear ink in this box
    threshold = (dark + paper) / 2
    mask = crop.point([255 if v < threshold else 0 for v in range(256)])
    rows = list(mask.resize((1, mask.height), Image.Resampling.BOX).tobytes())
    peak = max(rows)
    if peak == 0:
        return None
    first = next(r for r, v in enumerate(rows) if v >= 0.05 * peak)
    last = max(r for r, v in enumerate(rows) if v >= 0.3 * peak) + 1
    if last - first < 2:
        return None
    mean = ImageStat.Stat(render.crop((left, top, right, bottom)), mask).mean
    color = Color(mean[0] / 255, mean[1] / 255, mean[2] / 255) if len(mean) >= 3 else BLACK
    return _Ink((top + first) / ZOOM, (top + last) / ZOOM, color)


def _font(name: str) -> pymupdf.Font:
    return pymupdf.Font(_FONTS[name])


def _font_for(text: str, fonts: dict[str, pymupdf.Font]) -> str:
    chars = {ord(c) for c in text if not c.isspace()}
    for name in ("sans", "cjk"):
        font = fonts.setdefault(name, _font(name))
        if all(font.has_glyph(c) for c in chars):
            return name
    return "sans"  # some characters can't be shown; MuPDF falls back per glyph


def _plan_lines(
    fz: pymupdf.Page, blocks: list[list[_Line]], figures: list[pymupdf.Rect]
) -> list[ScanLine]:
    pix = fz.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), colorspace=pymupdf.csRGB, alpha=False)
    render = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    gray = render.convert("L")
    fonts: dict[str, pymupdf.Font] = {}
    to_unrotated = ~fz.rotation_matrix
    out: list[ScanLine] = []
    for block in blocks:
        inks = [_measure(render, gray, line.box) for line in block]
        heights = [i.baseline - i.top for i in inks if i is not None]
        # one paragraph, one size: the median is robust against a line without ascenders
        block_size = statistics.median(heights) / ASCENT if heights else None
        for line, ink in zip(block, inks, strict=True):
            if _over_figure(pymupdf.Rect(*line.box.as_tuple()) * to_unrotated, figures):
                continue
            size = block_size or line.size * 0.85
            baseline = ink.baseline if ink is not None else line.origin.y
            text = line.text
            font = _font_for(text, fonts)
            bullet = next((b for b in "\u25cf\u2022" if fonts[font].has_glyph(ord(b))), "")
            if bullet:
                text = _BULLET.sub(bullet, text)
            natural = fonts[font].text_length(text, fontsize=size)
            scale = line.box.width / natural if natural > 0 else 0.0
            if not MIN_SCALE <= scale <= MAX_SCALE:
                log.debug("leaving a recognized line invisible (scale %.2f): %r", scale, text)
                continue
            out.append(
                ScanLine(
                    text,
                    Point(line.box.x0, baseline),
                    round(size, 2),
                    scale,
                    ink.color if ink is not None else BLACK,
                    line.box,
                    font,
                )
            )
    return out


def _erased_image(
    doc: pymupdf.Document, image: dict[str, Any], boxes: list[pymupdf.Rect]
) -> bytes | None:
    """The scan image with ``boxes`` (unrotated page space) painted over in the paper color
    around each; None when it can't be done safely or no box touches the image."""
    xref = int(image["xref"])
    if image.get("has-mask") or doc.xref_get_key(xref, "ImageMask")[1] == "true":
        log.info("not erasing text from image %d: it has a mask", xref)
        return None
    to_unit = ~pymupdf.Matrix(image["transform"])
    pix = pymupdf.Pixmap(doc, xref)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    mode = "L" if pix.n == 1 else "RGB"
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    w, h = img.size
    touched = False
    for box in boxes:
        pad = max(1.0, LINE_PAD * min(box.width, box.height))
        unit = pymupdf.Rect(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad) * to_unit
        x0, x1 = sorted((unit.x0 * w, unit.x1 * w))
        y0, y1 = sorted((unit.y0 * h, unit.y1 * h))
        area = (max(0, int(x0)), max(0, int(y0)), min(w, int(x1) + 1), min(h, int(y1) + 1))
        if area[2] - area[0] < 1 or area[3] - area[1] < 1:
            continue
        region = img.crop(area)
        lum = region.convert("L")
        middle = _percentile(lum.histogram(), 0.5)
        light = lum.point([255 if v >= middle else 0 for v in range(256)])
        paper = ImageStat.Stat(region, light).median
        img.paste(paper[0] if mode == "L" else tuple(paper), area)
        touched = True
    if not touched:
        return None
    out = io.BytesIO()
    kind = doc.xref_get_key(xref, "Filter")[1]
    if "DCT" in kind or "JPX" in kind:
        img.save(out, format="JPEG", quality=90)
    elif image.get("bpc") == 1:
        img.convert("L").point(lambda v: 255 if v >= 128 else 0).convert("1").save(out, "PNG")
    else:
        img.save(out, format="PNG")
    return out.getvalue()


def text_plan(page: MuPage, layer: bytes | None, cleanup: ScanCleanup) -> ScanTextPlan | None:
    fz = page.fz
    scans = _scan_images(fz)
    if not scans:
        return None
    rotation = fz.rotation_matrix
    if layer is not None:
        src = pymupdf.open("pdf", layer)
        try:
            lp = src[0]
            to_visible = pymupdf.Matrix(
                fz.rect.width / lp.rect.width, fz.rect.height / lp.rect.height
            )
            blocks = _invisible_lines(lp, to_visible)
        finally:
            src.close()
    else:
        blocks = _invisible_lines(fz, rotation)
    lines = _plan_lines(fz, blocks, _figures(fz, scans))
    if not lines:
        return None
    images: list[tuple[str, bytes | None]] = []
    if cleanup is ScanCleanup.REMOVE:
        images = [(str(s["xref"]), None) for s in scans]
    elif cleanup is ScanCleanup.ERASE:
        to_unrotated = ~rotation
        boxes = [pymupdf.Rect(*line.box.as_tuple()) * to_unrotated for line in lines if line.box]
        seen: set[int] = set()
        for s in scans:
            if s["xref"] in seen:
                continue
            seen.add(s["xref"])
            data = _erased_image(page._doc.fz, s, boxes)
            if data is not None:
                images.append((str(s["xref"]), data))
        if not images:
            return None  # visible text over glyphs still in the scan would show twice
    return ScanTextPlan(tuple(lines), tuple(images))


# -- applying -------------------------------------------------------------------------------------
def _new_image(doc: pymupdf.Document, data: bytes) -> int:
    img = Image.open(io.BytesIO(data))
    xref = int(doc.get_new_xref())
    gray = img.mode in ("L", "1")
    bpc = 1 if img.mode == "1" else 8
    doc.update_object(
        xref,
        f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} "
        f"/ColorSpace /{'DeviceGray' if gray else 'DeviceRGB'} /BitsPerComponent {bpc} >>",
    )
    if img.format == "JPEG":
        doc.update_stream(xref, data, compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
    else:
        raw = img.tobytes() if img.mode in ("1", "L", "RGB") else img.convert("RGB").tobytes()
        doc.update_stream(xref, raw, compress=True)
    return xref


def _xobjects(doc: pymupdf.Document, holder: int) -> tuple[int, str, dict[str, int]]:
    """Where ``holder``'s (a page's or form's) XObject names live — the object to set keys on
    and the key prefix there — and the names with the objects they refer to."""
    kind, value = doc.xref_get_key(holder, "Resources")
    if kind == "xref":
        owner, prefix = int(value.split()[0]), ""
    elif kind == "dict":
        owner, prefix = holder, "Resources/"
    else:
        return holder, "", {}
    kind, value = doc.xref_get_key(owner, prefix + "XObject")
    if kind == "xref":
        owner, prefix = int(value.split()[0]), ""
        value = doc.xref_object(owner, compressed=True)
    elif kind == "dict":
        prefix += "XObject/"
    else:
        return owner, prefix, {}
    return owner, prefix, {m.group(1): int(m.group(2)) for m in _XOBJECT_REF.finditer(value)}


def _is_form(doc: pymupdf.Document, xref: int) -> bool:
    return bool(doc.xref_get_key(xref, "Subtype")[1] == "/Form")


def _make_private(doc: pymupdf.Document, form: int) -> None:
    """Give a freshly copied form its own Resources and XObject dictionaries."""
    kind, value = doc.xref_get_key(form, "Resources")
    if kind == "xref":
        res = _clone_object(doc, int(value.split()[0]))
        doc.xref_set_key(form, "Resources", f"{res} 0 R")
        owner, prefix = res, ""
    elif kind == "dict":
        owner, prefix = form, "Resources/"
    else:
        return
    kind, value = doc.xref_get_key(owner, prefix + "XObject")
    if kind == "xref":
        doc.xref_set_key(
            owner, prefix + "XObject", f"{_clone_object(doc, int(value.split()[0]))} 0 R"
        )


def _drop_draws(ops: list[Operation], names: set[str]) -> list[Operation]:
    return [
        o
        for o in ops
        if not (
            o.operator == "Do"
            and o.operands
            and isinstance(o.operands[0], Name)
            and str(o.operands[0]) in names
        )
    ]


def _swap_images(page: MuPage, changes: dict[int, int | None]) -> None:
    """Draw new image objects instead of the old ones (``None``: stop drawing them) on this
    page only. Forms on the way to an image are copied first, since other pages may use them;
    the page's own resources are already private (``unshare_page``)."""
    doc = page._doc.fz
    memo: dict[int, bool] = {}

    def holds(form: int, depth: int) -> bool:
        if form not in memo:
            memo[form] = False  # breaks cycles
            _o, _p, names = _xobjects(doc, form)
            memo[form] = any(
                x in changes or (depth < MAX_DEPTH and _is_form(doc, x) and holds(x, depth + 1))
                for x in names.values()
            )
        return memo[form]

    def visit(holder: int, depth: int) -> set[str]:
        """Rewrite ``holder``'s entries; returns the names it must no longer draw."""
        owner, prefix, names = _xobjects(doc, holder)
        removed: set[str] = set()
        for name, xref in names.items():
            if xref in changes:
                new = changes[xref]
                value = "null" if new is None else f"{new} 0 R"
                doc.xref_set_key(owner, prefix + name, value)
                if new is None:
                    removed.add(name)
            elif depth < MAX_DEPTH and _is_form(doc, xref) and holds(xref, depth + 1):
                copy = _clone_stream(doc, xref)
                _make_private(doc, copy)
                gone = visit(copy, depth + 1)
                if gone:
                    doc.update_stream(copy, write(_drop_draws(parse(doc.xref_stream(copy)), gone)))
                doc.xref_set_key(owner, prefix + name, f"{copy} 0 R")
        return removed

    gone = visit(page.fz.xref, 0)
    if gone:
        _write_ops(page, _drop_draws(parse(page.fz.read_contents()), gone))


def _merge_streams(page: MuPage, before: set[int]) -> None:
    """Join the content streams added since ``before`` into one, with one name per font
    (TextWriter adds a stream and a font name for every line it writes)."""
    fz, doc = page.fz, page._doc.fz
    contents = fz.get_contents()
    new = [x for x in contents if x not in before]
    if len(new) < 2:
        return
    ops = parse(b"\n".join(doc.xref_stream(x) or b"" for x in new))
    canonical: dict[int, str] = {}
    rename: dict[str, str] = {}
    for font_xref, _ext, _type, _base, name, *_rest in fz.get_fonts(full=True):
        first = canonical.setdefault(int(font_xref), str(name))
        if first != name:
            rename[str(name)] = first
    for op in ops:
        if op.operator == "Tf" and op.operands and isinstance(op.operands[0], Name):
            target = rename.get(str(op.operands[0]))
            if target is not None:
                op.operands[0] = Name(target)
    merged = int(doc.get_new_xref())
    doc.update_object(merged, "<<>>")
    doc.update_stream(merged, write(ops))
    kept = [x for x in contents if x in before] + [merged]
    doc.xref_set_key(fz.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in kept) + "]")
    if rename:
        kind, value = doc.xref_get_key(fz.xref, "Resources")
        owner, prefix = (int(value.split()[0]), "") if kind == "xref" else (fz.xref, "Resources/")
        kind, value = doc.xref_get_key(owner, prefix + "Font")
        if kind == "xref":
            owner, prefix = int(value.split()[0]), ""
        else:
            prefix += "Font/"
        for name in rename:
            doc.xref_set_key(owner, prefix + name, "null")


def _write_lines(page: MuPage, lines: tuple[ScanLine, ...]) -> None:
    fz = page.fz
    rotation = fz.rotation_matrix
    to_unrotated = ~rotation
    turn = pymupdf.Matrix(rotation.a, rotation.b, rotation.c, rotation.d, 0, 0)
    fonts: dict[str, pymupdf.Font] = {}
    before = set(fz.get_contents())
    for line in lines:
        font = fonts.setdefault(line.font, _font(line.font))
        origin = pymupdf.Point(line.origin.x, line.origin.y) * to_unrotated
        writer = pymupdf.TextWriter(fz.rect)
        writer.append(origin, line.text, font=font, fontsize=line.size)
        writer.write_text(
            fz,
            color=(line.color.r, line.color.g, line.color.b),
            morph=(origin, pymupdf.Matrix(line.scale, 0, 0, 1, 0, 0) * turn),
        )
    _merge_streams(page, before)


def apply_plan(page: MuPage, plan: ScanTextPlan) -> None:
    doc = page._doc.fz
    _remove_text(page, [line.box for line in plan.lines if line.box is not None])
    changes: dict[int, int | None] = {}
    for key, data in plan.images:
        changes[int(key)] = None if data is None else _new_image(doc, data)
    if changes:
        _swap_images(page, changes)
    _write_lines(page, plan.lines)
    page._doc.mark_page_changed(page.index)
