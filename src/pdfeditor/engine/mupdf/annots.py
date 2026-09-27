"""Annotation writing for the MuPDF backend: create, update, delete, reorder, flatten.

Models arrive in visible page space. MuPDF's annotation constructors take *unrotated* page
coordinates, and raw PDF arrays (QuadPoints, InkList, ...) use PDF user space, so every
geometry goes through one of the conversions below.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

import pymupdf

from pdfeditor.engine.base import EngineError, UnsupportedFeature
from pdfeditor.engine.mupdf import convert as cv
from pdfeditor.model.annotations import (
    FLAG_HIDDEN,
    FLAG_LOCKED,
    IMAGE_STAMP,
    STANDARD_STAMPS,
    AnnotationModel,
    AnnotationType,
    ReviewState,
)
from pdfeditor.model.geometry import Point, Quad, Rect
from pdfeditor.model.redaction import GraphicsRedaction, ImageRedaction, RedactOptions

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.page import MuPage

_LINE_END_CODES = {
    "None": pymupdf.PDF_ANNOT_LE_NONE,
    "Square": pymupdf.PDF_ANNOT_LE_SQUARE,
    "Circle": pymupdf.PDF_ANNOT_LE_CIRCLE,
    "Diamond": pymupdf.PDF_ANNOT_LE_DIAMOND,
    "OpenArrow": pymupdf.PDF_ANNOT_LE_OPEN_ARROW,
    "ClosedArrow": pymupdf.PDF_ANNOT_LE_CLOSED_ARROW,
    "Butt": pymupdf.PDF_ANNOT_LE_BUTT,
    "ROpenArrow": pymupdf.PDF_ANNOT_LE_R_OPEN_ARROW,
    "RClosedArrow": pymupdf.PDF_ANNOT_LE_R_CLOSED_ARROW,
    "Slash": pymupdf.PDF_ANNOT_LE_SLASH,
}
_REF = re.compile(r"(\d+)\s+0\s+R")
# Types whose geometry is just their rectangle.
_RECT_TYPES = {
    AnnotationType.TEXT,
    AnnotationType.FREE_TEXT,
    AnnotationType.SQUARE,
    AnnotationType.CIRCLE,
    AnnotationType.STAMP,
    AnnotationType.FILE_ATTACHMENT,
    AnnotationType.CARET,
}
_FILL_TYPES = {
    AnnotationType.SQUARE,
    AnnotationType.CIRCLE,
    AnnotationType.POLYGON,
    AnnotationType.LINE,
    AnnotationType.POLYLINE,
}
WRITABLE_TYPES = frozenset(
    _RECT_TYPES
    | {
        AnnotationType.LINE,
        AnnotationType.POLYGON,
        AnnotationType.POLYLINE,
        AnnotationType.HIGHLIGHT,
        AnnotationType.UNDERLINE,
        AnnotationType.SQUIGGLY,
        AnnotationType.STRIKEOUT,
        AnnotationType.INK,
        AnnotationType.REDACT,
    }
)


class _Geo:
    """Coordinate conversions for one page."""

    def __init__(self, page: pymupdf.Page) -> None:
        self.vis_to_unrot = page.derotation_matrix
        self.vis_to_pdf = page.derotation_matrix * ~page.transformation_matrix

    def point(self, p: Point) -> pymupdf.Point:
        return pymupdf.Point(p.x, p.y) * self.vis_to_unrot

    def rect(self, r: Rect) -> pymupdf.Rect:
        return (pymupdf.Rect(r.x0, r.y0, r.x1, r.y1) * self.vis_to_unrot).normalize()

    def quad(self, q: Quad) -> pymupdf.Quad:
        return cv.to_fz_quad(q) * self.vis_to_unrot

    def pdf(self, p: Point) -> tuple[float, float]:
        q = pymupdf.Point(p.x, p.y) * self.vis_to_pdf
        return (q.x, q.y)


def pdf_num(v: float) -> str:
    """PDF number syntax: fixed-point only (``1e-06`` is not a valid PDF number)."""
    text = f"{v:.5f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _nums(values: Sequence[float]) -> str:
    return "[" + " ".join(pdf_num(v) for v in values) + "]"


def _pdf_date(dt: datetime | None) -> str:
    return cv.format_pdf_date(dt) if dt is not None else ""


# -- FreeText callouts ------------------------------------------------------------------------
def _pdf_rect(geo: _Geo, r: Rect) -> tuple[float, float, float, float]:
    """A visible-space rectangle in PDF user space (x0, y0, x1, y1), normalized."""
    a = geo.pdf(Point(r.x0, r.y0))
    b = geo.pdf(Point(r.x1, r.y1))
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _write_callout(doc: pymupdf.Document, geo: _Geo, xref: int, model: AnnotationModel) -> None:
    """Store a callout's geometry: /CL (leader line), /Rect (box plus line) and /RD.

    MuPDF's constructor derives /Rect and /RD itself, but only at creation; writing all three
    here keeps create and move/resize identical. /RD follows MuPDF's reading of it
    (left, bottom, right, top in PDF space), since MuPDF generates the appearance stream that
    every viewer draws.
    """
    box = pymupdf.Rect(_pdf_rect(geo, model.rect))
    points = [geo.pdf(p) for p in model.vertices]
    # Room for the line width and the arrow head around the line's points.
    pad = 4.0 + 3.0 * max(model.border_width, 1.0)
    outer = pymupdf.Rect(box)
    for x, y in points:
        outer |= pymupdf.Rect(x - pad, y - pad, x + pad, y + pad)
    rd = (box.x0 - outer.x0, box.y0 - outer.y0, outer.x1 - box.x1, outer.y1 - box.y1)
    ending = model.line_endings[0] if model.line_endings[0] in _LINE_END_CODES else "None"
    doc.xref_set_key(xref, "IT", "/FreeTextCallout")
    doc.xref_set_key(xref, "CL", _nums([c for p in points for c in p]))
    doc.xref_set_key(xref, "LE", f"/{ending}")
    doc.xref_set_key(xref, "Rect", _nums([outer.x0, outer.y0, outer.x1, outer.y1]))
    doc.xref_set_key(xref, "RD", _nums(rd))


def callout_box(doc: pymupdf.Document, annot: pymupdf.Annot) -> pymupdf.Rect | None:
    """A callout's text box in MuPDF's unrotated page space, or ``None`` if not a callout."""
    xref = annot.xref
    if doc.xref_get_key(xref, "CL")[0] != "array":
        return None
    r = pymupdf.Rect(annot.rect)
    rd = _array(doc, xref, "RD")
    if rd is None or len(rd) != 4:
        return r
    # MuPDF's page space runs top-down: PDF "top" (rd[3]) is the smaller y.
    box = pymupdf.Rect(r.x0 + rd[0], r.y0 + rd[3], r.x1 - rd[2], r.y1 - rd[1])
    return box if not box.is_empty else r


def callout_ending(doc: pymupdf.Document, xref: int) -> str:
    kind, value = doc.xref_get_key(xref, "LE")
    if kind == "name" and value.lstrip("/") in _LINE_END_CODES:
        return value.lstrip("/")
    return "None"


# -- image stamps -----------------------------------------------------------------------------
def _fit_aspect(r: Rect, aspect: float) -> Rect:
    """The largest rectangle with height/width ``aspect`` centered in ``r``."""
    r = r.normalized()
    w, h = r.width, r.height
    if h > w * aspect:
        h = w * aspect
    else:
        w = h / aspect
    c = r.center
    return Rect(c.x - w / 2, c.y - h / 2, c.x + w / 2, c.y + h / 2)


def _upright_image_stamp(doc: pymupdf.Document, page: pymupdf.Page, xref: int) -> None:
    """Counter-rotate an image stamp's appearance on a rotated page so it reads upright.

    MuPDF draws the image in unrotated page space and resets the appearance /Matrix whenever
    it regenerates it, so this runs after every update.
    """
    if not page.rotation % 360 or doc.xref_get_key(xref, "Name") != ("name", f"/{IMAGE_STAMP}"):
        return
    kind, ap = doc.xref_get_key(xref, "AP/N")
    if kind != "xref" or (match := _REF.match(ap)) is None:
        return
    m = pymupdf.Matrix(page.rotation)
    doc.xref_set_key(int(match.group(1)), "Matrix", _nums([m.a, m.b, m.c, m.d, 0, 0]))


def stamp_image(doc: pymupdf.Document, xref: int) -> bytes | None:
    """The image a stamp's appearance draws (PNG, or the original JPEG), if it draws one.

    Needed to re-create the stamp (undoing a delete, pasting a copy). Only images placed
    directly in the normal appearance are found; vector custom stamps return ``None``.
    """
    kind, ap = doc.xref_get_key(xref, "AP/N")
    if kind != "xref" or (match := _REF.match(ap)) is None:
        return None
    ap_xref = int(match.group(1))
    kind, value = doc.xref_get_key(ap_xref, "Resources/XObject")
    if kind == "xref" and (match := _REF.match(value)) is not None:
        value = doc.xref_object(int(match.group(1)))
    elif kind != "dict":
        return None
    for ref in _REF.findall(value):
        image_xref = int(ref)
        if doc.xref_get_key(image_xref, "Subtype") != ("name", "/Image"):
            continue
        try:
            info = doc.extract_image(image_xref)
            if info and not info.get("smask") and info.get("ext") in ("png", "jpeg", "jpg"):
                return bytes(info["image"])
            pix = pymupdf.Pixmap(doc, image_xref)
            if pix.colorspace is not None and pix.colorspace.n > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if info and info.get("smask") and not pix.alpha:
                pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, info["smask"]))
            return bytes(pix.tobytes("png"))
        except Exception:  # damaged image: keep the stamp readable
            return None
    return None


# -- lookup ---------------------------------------------------------------------------------
def find(page: MuPage, annot_id: int | None = None, name: str = "") -> pymupdf.Annot:
    """Load an annotation by /NM name, falling back to object id.

    The name comes first: MuPDF can reuse the object number of a deleted annotation, so after an
    undo/redo cycle an old id may point at a different annotation.
    """
    fz = page.fz
    if name:
        for annot in fz.annots():
            if annot.info.get("id") == name:
                return annot
    if annot_id is not None and annot_id in {entry[0] for entry in fz.annot_xrefs()}:
        return fz.load_annot(annot_id)
    raise EngineError(f"annotation not found (id={annot_id}, name={name!r})")


# -- create / update --------------------------------------------------------------------------
def add(page: MuPage, model: AnnotationModel) -> AnnotationModel:
    if model.type not in WRITABLE_TYPES:
        raise UnsupportedFeature(f"can't create {model.type.value} annotations")
    fz = page.fz
    geo = _Geo(fz)
    t = model.type
    if t is AnnotationType.TEXT:
        annot = fz.add_text_annot(
            geo.rect(model.rect).tl, model.contents, icon=model.icon or "Note"
        )
    elif t is AnnotationType.FREE_TEXT:
        annot = fz.add_freetext_annot(
            geo.rect(model.rect),
            model.contents,
            fontsize=model.font_size,
            fontname="helv",
            text_color=model.text_color.rgb() if model.text_color else (0, 0, 0),
            fill_color=model.fill.rgb() if model.fill else None,
            rotate=fz.rotation,  # keep the text upright in the visible page
        )
    elif t is AnnotationType.SQUARE:
        annot = fz.add_rect_annot(geo.rect(model.rect))
    elif t is AnnotationType.CIRCLE:
        annot = fz.add_circle_annot(geo.rect(model.rect))
    elif t is AnnotationType.LINE:
        a, b = model.vertices[:2]
        annot = fz.add_line_annot(geo.point(a), geo.point(b))
    elif t is AnnotationType.POLYGON:
        annot = fz.add_polygon_annot([geo.point(p) for p in model.vertices])
    elif t is AnnotationType.POLYLINE:
        annot = fz.add_polyline_annot([geo.point(p) for p in model.vertices])
    elif t.is_markup:
        quads = [geo.quad(q) for q in model.quads]
        adder = {
            AnnotationType.HIGHLIGHT: fz.add_highlight_annot,
            AnnotationType.UNDERLINE: fz.add_underline_annot,
            AnnotationType.SQUIGGLY: fz.add_squiggly_annot,
            AnnotationType.STRIKEOUT: fz.add_strikeout_annot,
        }[t]
        annot = adder(quads=quads)
    elif t is AnnotationType.INK:
        strokes = [[tuple(geo.point(p)) for p in stroke] for stroke in model.ink]
        annot = fz.add_ink_annot(strokes)
    elif t is AnnotationType.STAMP and model.image is not None:
        # MuPDF fits the image into the rect (keeping its aspect ratio) and draws it as the
        # appearance, so every viewer shows it; the model's rect then shrinks to the image.
        before = {entry[0] for entry in fz.annot_xrefs()}
        try:
            annot = fz.add_stamp_annot(geo.rect(model.rect), stamp=bytes(model.image))
        except Exception as exc:  # MuPDF raises its own error classes for bad images
            # MuPDF creates the annotation before decoding the image: drop the empty stamp.
            for leftover in fz.annots():
                if leftover.xref not in before:
                    fz.delete_annot(leftover)
                    break
            raise EngineError(f"can't use the stamp image: {exc}") from exc
        created = annot.rect
        if fz.rotation % 180 and created.width > 0 and created.height > 0:
            # MuPDF fitted the image in unrotated space; the appearance is turned upright in
            # _apply_properties, so fit the image's own aspect ratio in the visible page.
            annot.set_rect(geo.rect(_fit_aspect(model.rect, created.height / created.width)))
    elif t is AnnotationType.STAMP:
        index = STANDARD_STAMPS.index(model.icon) if model.icon in STANDARD_STAMPS else 0
        annot = fz.add_stamp_annot(geo.rect(model.rect), stamp=index)
    elif t is AnnotationType.REDACT:
        quads = [geo.quad(q) for q in model.quads]
        box = geo.rect(model.rect)
        if quads:
            box = quads[0].rect
            for q in quads[1:]:
                box |= q.rect
        annot = fz.add_redact_annot(
            box,
            text=model.overlay_text or None,
            fill=model.fill.rgb() if model.fill else (0, 0, 0),
            text_color=model.text_color.rgb() if model.text_color else (1, 1, 1),
            cross_out=True,
        )
        if len(quads) > 1:  # exact line areas of a multi-line text mark
            coords: list[float] = []
            for q in model.quads:
                for p in (q.ul, q.ur, q.ll, q.lr):
                    coords.extend(geo.pdf(p))
            page._doc.fz.xref_set_key(annot.xref, "QuadPoints", _nums(coords))
    elif t is AnnotationType.FILE_ATTACHMENT:
        if model.file_data is None:
            raise EngineError("file attachment annotation needs file_data")
        name = model.file_name or "attachment"
        annot = fz.add_file_annot(
            geo.rect(model.rect).tl,
            model.file_data,
            name,
            ufilename=name,
            desc=model.contents,
            icon=model.icon or "PushPin",
        )
    else:  # CARET and others in WRITABLE_TYPES without a constructor
        raise UnsupportedFeature(f"can't create {t.value} annotations")
    if annot is None:
        raise EngineError(f"MuPDF refused to create a {t.value} annotation")
    name = model.name or f"pdfeditor-{uuid.uuid4().hex}"
    page._doc.fz.xref_set_key(annot.xref, "NM", pymupdf.get_pdf_str(name))
    if model.is_callout:
        _write_callout(page._doc.fz, geo, annot.xref, model)
        annot = find(page, annot.xref)
    _apply_properties(page, annot, model)
    page._doc.mark_page_changed(page.index)
    return page._annot_model(find(page, annot.xref))


def update(page: MuPage, model: AnnotationModel) -> AnnotationModel:
    """Write ``model``'s geometry and properties onto the existing annotation it identifies."""
    annot = find(page, model.id, model.name)
    fz_doc = page._doc.fz
    geo = _Geo(page.fz)
    t = AnnotationType.from_pdf_name(annot.type[1])
    xref = annot.xref
    if t is AnnotationType.FREE_TEXT and model.is_callout:
        _write_callout(fz_doc, geo, xref, model)
        annot = find(page, xref)
    elif t in _RECT_TYPES:
        if t is AnnotationType.FREE_TEXT and fz_doc.xref_get_key(xref, "CL")[0] != "null":
            # The callout line was removed. MuPDF's set_rect would re-create /CL, so the plain
            # box is written directly.
            for key in ("CL", "IT", "LE", "RD"):
                fz_doc.xref_set_key(xref, key, "null")
            fz_doc.xref_set_key(xref, "Rect", _nums(_pdf_rect(geo, model.rect)))
            annot = find(page, xref)
        else:
            annot.set_rect(geo.rect(model.rect))
    elif t is AnnotationType.LINE and len(model.vertices) >= 2:
        fz_doc.xref_set_key(
            xref, "L", _nums([*geo.pdf(model.vertices[0]), *geo.pdf(model.vertices[1])])
        )
    elif t in (AnnotationType.POLYGON, AnnotationType.POLYLINE) and model.vertices:
        fz_doc.xref_set_key(
            xref, "Vertices", _nums([c for p in model.vertices for c in geo.pdf(p)])
        )
    elif t.is_markup and model.quads:
        coords: list[float] = []
        for q in model.quads:
            for p in (q.ul, q.ur, q.ll, q.lr):
                coords.extend(geo.pdf(p))
        fz_doc.xref_set_key(xref, "QuadPoints", _nums(coords))
    elif t is AnnotationType.INK and model.ink:
        strokes = [_nums([c for p in stroke for c in geo.pdf(p)]) for stroke in model.ink]
        fz_doc.xref_set_key(xref, "InkList", "[" + " ".join(strokes) + "]")
    if t not in _RECT_TYPES:
        # Vertex-based types: MuPDF recomputes /Rect from the vertices when regenerating.
        annot = find(page, xref)
    _apply_properties(page, annot, model, created=False)
    page._doc.mark_page_changed(page.index)
    return page._annot_model(find(page, xref))


def _apply_properties(
    page: MuPage, annot: pymupdf.Annot, model: AnnotationModel, created: bool = True
) -> None:
    doc = page._doc.fz
    t = model.type
    info: dict[str, str] = {
        "content": model.contents,
        "title": model.author,
        "subject": model.subject,
    }
    if created or model.created is not None:
        info["creationDate"] = _pdf_date(model.created or datetime.now().astimezone())
    info["modDate"] = _pdf_date(model.modified or datetime.now().astimezone())
    annot.set_info(info)
    if t is AnnotationType.REDACT:
        annot.update()
        return
    if t is not AnnotationType.FREE_TEXT:
        fill = model.fill.rgb() if model.fill is not None and t in _FILL_TYPES else None
        stroke = model.color.rgb() if model.color is not None else None
        if stroke is not None or fill is not None:
            annot.set_colors(stroke=stroke, fill=fill)
    if t is AnnotationType.FREE_TEXT:
        # MuPDF draws FreeText borders only via /BS; no color means no border.
        annot.set_border(width=model.border_width if model.color else 0)
    elif (
        t not in (AnnotationType.TEXT, AnnotationType.FILE_ATTACHMENT, AnnotationType.STAMP)
        and not t.is_markup
    ):
        annot.set_border(width=model.border_width, dashes=list(model.dashes) or None)
    if t in (AnnotationType.LINE, AnnotationType.POLYLINE):
        start, end = model.line_endings
        annot.set_line_ends(_LINE_END_CODES.get(start, 0), _LINE_END_CODES.get(end, 0))
    if t is AnnotationType.TEXT and model.icon:
        annot.set_name(model.icon)
    annot.set_opacity(model.opacity if model.opacity < 1 else 1)
    flags = model.flags
    if model.locked:
        flags |= FLAG_LOCKED
    else:
        flags &= ~FLAG_LOCKED
    if model.in_reply_to is not None:
        flags |= FLAG_HIDDEN  # replies live in the comments panel, not as extra icons
    annot.set_flags(flags)
    if model.in_reply_to is not None:
        annot.set_irt_xref(model.in_reply_to)
    if model.state is not ReviewState.NONE:
        doc.xref_set_key(annot.xref, "State", pymupdf.get_pdf_str(model.state.value))
        doc.xref_set_key(annot.xref, "StateModel", pymupdf.get_pdf_str("Review"))
    else:
        doc.xref_set_key(annot.xref, "State", "null")
        doc.xref_set_key(annot.xref, "StateModel", "null")
    if t is AnnotationType.FREE_TEXT:
        annot.update(
            fontsize=model.font_size,
            text_color=model.text_color.rgb() if model.text_color else (0, 0, 0),
            fill_color=model.fill.rgb() if model.fill else None,
        )
    else:
        annot.update()
        if t is AnnotationType.STAMP:
            _upright_image_stamp(doc, page.fz, annot.xref)


# -- delete / order -----------------------------------------------------------------------------
def delete(page: MuPage, annot_id: int | None, name: str = "") -> None:
    annot = find(page, annot_id, name)
    page.fz.delete_annot(annot)
    page._doc.mark_page_changed(page.index)


def _annots_array(page: MuPage) -> tuple[int | None, list[int]]:
    """(xref of an indirect /Annots array or None, annotation xrefs in order)."""
    doc = page._doc.fz
    kind, value = doc.xref_get_key(page.fz.xref, "Annots")
    if kind == "xref":
        array_xref = int(_REF.match(value).group(1))  # type: ignore[union-attr]
        return array_xref, [int(m) for m in _REF.findall(doc.xref_object(array_xref))]
    if kind == "array":
        return None, [int(m) for m in _REF.findall(value)]
    return None, []


def order(page: MuPage) -> list[int]:
    return _annots_array(page)[1]


def set_order(page: MuPage, ids: Sequence[int]) -> None:
    """Reorder /Annots (later = drawn on top). ``ids`` must be a permutation of the current."""
    array_xref, current = _annots_array(page)
    if sorted(ids) != sorted(current):
        raise EngineError("new annotation order must contain exactly the current annotations")
    text = "[" + " ".join(f"{i} 0 R" for i in ids) + "]"
    doc = page._doc.fz
    if array_xref is not None:
        doc.update_object(array_xref, text)
    else:
        doc.xref_set_key(page.fz.xref, "Annots", text)
    page._doc.mark_page_changed(page.index)


# -- flatten ------------------------------------------------------------------------------------
def _array(doc: pymupdf.Document, xref: int, key: str) -> list[float] | None:
    kind, value = doc.xref_get_key(xref, key)
    if kind != "array":
        return None
    return [float(v) for v in value.strip("[]").split()]


def flatten(page: MuPage, ids: Sequence[int] | None = None) -> int:
    """Draw annotations into the page content and remove them. Returns how many were flattened.

    Each annotation's normal appearance stream is placed as a form XObject using the mapping of
    PDF 32000 12.5.5 (the appearance BBox, transformed by its Matrix, is fitted to /Rect).
    Hidden annotations, popups, links and form widgets are left alone.
    """
    doc = page._doc.fz
    fz = page.fz
    skip = {AnnotationType.POPUP, AnnotationType.LINK, AnnotationType.WIDGET}
    targets = []
    for annot in fz.annots():
        t = AnnotationType.from_pdf_name(annot.type[1])
        if t in skip or annot.flags & FLAG_HIDDEN:
            continue
        if ids is None or annot.xref in ids:
            targets.append(annot.xref)
    if not targets:
        return 0

    content_ops: list[str] = []
    placed: list[int] = []
    for xref in targets:
        annot = fz.load_annot(xref)
        kind, ap = doc.xref_get_key(xref, "AP/N")
        if kind != "xref":
            annot.update()
            kind, ap = doc.xref_get_key(xref, "AP/N")
            if kind != "xref":
                continue
        ap_xref = int(_REF.match(ap).group(1))  # type: ignore[union-attr]
        rect = _array(doc, xref, "Rect")
        bbox = _array(doc, ap_xref, "BBox")
        if rect is None or bbox is None:
            continue
        m = _array(doc, ap_xref, "Matrix") or [1, 0, 0, 1, 0, 0]
        form_m = pymupdf.Matrix(*m)
        box = pymupdf.Rect(*bbox) * form_m  # transformed appearance box
        r = pymupdf.Rect(*rect).normalize()
        if box.is_empty or r.is_empty:
            continue
        sx, sy = r.width / box.width, r.height / box.height
        tx, ty = r.x0 - box.x0 * sx, r.y0 - box.y0 * sy
        doc.xref_set_key(ap_xref, "Type", "/XObject")
        doc.xref_set_key(ap_xref, "Subtype", "/Form")
        name = f"PdfEdFlat{ap_xref}"
        _add_xobject_resource(page, name, ap_xref)
        cm = " ".join(pdf_num(v) for v in (sx, 0, 0, sy, tx, ty))
        content_ops.append(f"q {cm} cm /{name} Do Q")
        placed.append(xref)

    if not placed:
        return 0
    # Isolate the existing content's graphics state, then draw the appearances on top.
    pre = doc.get_new_xref()
    doc.update_object(pre, "<<>>")
    doc.update_stream(pre, b"q\n")
    post = doc.get_new_xref()
    doc.update_object(post, "<<>>")
    doc.update_stream(post, ("Q\n" + "\n".join(content_ops) + "\n").encode())
    contents = [pre, *fz.get_contents(), post]
    doc.xref_set_key(fz.xref, "Contents", "[" + " ".join(f"{c} 0 R" for c in contents) + "]")
    for xref in placed:
        page.fz.delete_annot(find(page, xref))
    page._doc.mark_page_changed(page.index)
    return len(placed)


def _add_xobject_resource(page: MuPage, name: str, xobject: int) -> None:
    doc = page._doc.fz
    kind, value = doc.xref_get_key(page.fz.xref, "Resources")
    if kind == "xref":
        res_xref = int(_REF.match(value).group(1))  # type: ignore[union-attr]
        sub_kind, sub = doc.xref_get_key(res_xref, "XObject")
        if sub_kind == "xref":
            doc.xref_set_key(int(_REF.match(sub).group(1)), name, f"{xobject} 0 R")  # type: ignore[union-attr]
        else:
            doc.xref_set_key(res_xref, f"XObject/{name}", f"{xobject} 0 R")
    else:
        sub_kind, sub = doc.xref_get_key(page.fz.xref, "Resources/XObject")
        if sub_kind == "xref":
            doc.xref_set_key(int(_REF.match(sub).group(1)), name, f"{xobject} 0 R")  # type: ignore[union-attr]
        else:
            doc.xref_set_key(page.fz.xref, f"Resources/XObject/{name}", f"{xobject} 0 R")


# -- redaction --------------------------------------------------------------------------------
_IMAGE_MODES = {
    ImageRedaction.NONE: pymupdf.PDF_REDACT_IMAGE_NONE,
    ImageRedaction.PIXELS: pymupdf.PDF_REDACT_IMAGE_PIXELS,
    ImageRedaction.REMOVE: pymupdf.PDF_REDACT_IMAGE_REMOVE,
}
_GRAPHICS_MODES = {
    GraphicsRedaction.NONE: pymupdf.PDF_REDACT_LINE_ART_NONE,
    GraphicsRedaction.COVERED: pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
    GraphicsRedaction.TOUCHED: pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
}


def stash_redactions(page: MuPage, keep: set[int] | None = None) -> list[AnnotationModel]:
    """Remove pending redaction marks (all, or all except ``keep``) and return them.

    MuPDF applies *every* redaction mark on a page at once; stashing lets callers apply a
    subset (or their own temporary marks) and then :func:`restore_redactions`.
    """
    stashed: list[AnnotationModel] = []
    for annot in list(page.fz.annots(types=[pymupdf.PDF_ANNOT_REDACT])):
        if keep is not None and annot.xref in keep:
            continue
        stashed.append(page._annot_model(annot))
        page.fz.delete_annot(annot)
    return stashed


def restore_redactions(page: MuPage, models: Sequence[AnnotationModel]) -> None:
    for model in models:
        add(page, model)


def apply_redactions(page: MuPage, ids: Sequence[int] | None, options: RedactOptions) -> int:
    """Apply redaction marks (all, or ``ids``); returns how many were applied."""
    stashed = stash_redactions(page, set(ids)) if ids is not None else []
    count = len(list(page.fz.annots(types=[pymupdf.PDF_ANNOT_REDACT])))
    try:
        if count:
            page.fz.apply_redactions(
                images=_IMAGE_MODES[options.images],
                graphics=_GRAPHICS_MODES[options.graphics],
                text=pymupdf.PDF_REDACT_TEXT_REMOVE
                if options.text
                else pymupdf.PDF_REDACT_TEXT_NONE,
            )
    finally:
        restore_redactions(page, stashed)
        page._doc.mark_page_changed(page.index)
    return count
