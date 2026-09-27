"""PyMuPDF implementation of :class:`pdfeditor.engine.base.Page`."""

from __future__ import annotations

import contextlib
import io
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pymupdf

from pdfeditor.engine.base import ColorMode, PageBoxes, RenderRequest, RenderResult
from pdfeditor.engine.mupdf import annots, content, ocr, pages
from pdfeditor.engine.mupdf import convert as cv
from pdfeditor.model.annotations import AnnotationModel, AnnotationType, ReviewState
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Point, Quad, Rect
from pdfeditor.model.objects import FontChoice, PageObject, ShapeSpec, TextStyle
from pdfeditor.model.outline import Destination, Link, LinkKind
from pdfeditor.model.pages import ImageStamp, TextStamp
from pdfeditor.model.redaction import RedactOptions
from pdfeditor.model.text import Block, Char, FontFlags, Line, Span, TableData, TextPage

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.document import MuDocument

_COLORSPACES = {
    ColorMode.RGB: (pymupdf.csRGB, False),
    ColorMode.RGBA: (pymupdf.csRGB, True),
    ColorMode.GRAY: (pymupdf.csGRAY, False),
}

_LINK_KINDS = {
    pymupdf.LINK_GOTO: LinkKind.GOTO,
    pymupdf.LINK_URI: LinkKind.URI,
    pymupdf.LINK_GOTOR: LinkKind.GOTO_REMOTE,
    pymupdf.LINK_LAUNCH: LinkKind.LAUNCH,
    pymupdf.LINK_NAMED: LinkKind.NAMED,
}


class MuPage:
    def __init__(self, doc: MuDocument, index: int) -> None:
        self._doc = doc
        self._index = index
        self._fz_page: pymupdf.Page | None = None
        # (annotations flag) -> (revision, display list)
        self._display_lists: dict[bool, tuple[int, pymupdf.DisplayList]] = {}

    # -- internal ---------------------------------------------------------------------------
    @property
    def fz(self) -> pymupdf.Page:
        if self._fz_page is None:
            self._fz_page = self._doc.fz.load_page(self._index)
        return self._fz_page

    def invalidate(self) -> None:
        """Drop cached MuPDF objects after the page (or the whole document) changed."""
        self._fz_page = None
        self._display_lists.clear()

    # MuPDF reports text, search hits, links and annotations in *unrotated* page space, while
    # rendering and ``rect`` use the visible (rotated) space. The Page contract says everything is
    # in visible space, so outputs pass through these helpers.
    def _vis_matrix(self) -> pymupdf.Matrix | None:
        page = self.fz
        return page.rotation_matrix if page.rotation else None

    def _vrect(self, r: Any, m: pymupdf.Matrix | None) -> Rect:
        return cv.rect(pymupdf.Rect(r) * m) if m is not None else cv.rect(r)

    def _vpoint(self, p: Any, m: pymupdf.Matrix | None) -> Point:
        return cv.point(pymupdf.Point(p) * m) if m is not None else cv.point(p)

    def _vquad(self, q: pymupdf.Quad, m: pymupdf.Matrix | None) -> Quad:
        return cv.quad(q * m) if m is not None else cv.quad(q)

    def _display_list(self, annots: bool) -> pymupdf.DisplayList:
        rev = self.revision
        cached = self._display_lists.get(annots)
        if cached is None or cached[0] != rev:
            cached = (rev, self.fz.get_displaylist(annots=annots))
            self._display_lists[annots] = cached
        return cached[1]

    # -- Page protocol ----------------------------------------------------------------------
    @property
    def index(self) -> int:
        return self._index

    @property
    def rect(self) -> Rect:
        return cv.rect(self.fz.rect)

    @property
    def rotation(self) -> int:
        return int(self.fz.rotation)

    @property
    def revision(self) -> int:
        return self._doc.page_revision(self._index)

    @property
    def boxes(self) -> PageBoxes:
        page = self.fz

        def optional(key: str, value: Any) -> Rect | None:
            return (
                cv.rect(value) if self._doc.fz.xref_get_key(page.xref, key)[0] != "null" else None
            )

        return PageBoxes(
            media=cv.rect(page.mediabox),
            crop=cv.rect(page.cropbox),
            trim=optional("TrimBox", page.trimbox),
            bleed=optional("BleedBox", page.bleedbox),
            art=optional("ArtBox", page.artbox),
        )

    def render(self, request: RenderRequest) -> RenderResult:
        cs, alpha = _COLORSPACES[request.color]
        clip = cv.to_fz_rect(request.clip) if request.clip is not None else None
        pix = self._display_list(request.annotations).get_pixmap(
            matrix=cv.to_fz_matrix(request.matrix), colorspace=cs, alpha=alpha, clip=clip
        )
        return RenderResult(
            width=pix.width,
            height=pix.height,
            stride=pix.stride,
            color=request.color,
            samples=bytes(pix.samples_mv),
        )

    def text_page(self, with_chars: bool = True) -> TextPage:
        mode = "rawdict" if with_chars else "dict"
        data = self.fz.get_text(mode, flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
        m = self._vis_matrix()
        blocks: list[Block] = []
        for b in data["blocks"]:
            if b.get("type", 0) == 1:
                blocks.append(Block(bbox=self._vrect(b["bbox"], m), is_image=True))
                continue
            lines: list[Line] = []
            for ln in b.get("lines", []):
                spans: list[Span] = []
                for s in ln.get("spans", []):
                    chars = tuple(
                        Char(ch["c"], self._vrect(ch["bbox"], m), self._vpoint(ch["origin"], m))
                        for ch in s.get("chars", ())
                    )
                    text = s["text"] if "text" in s else "".join(ch.c for ch in chars)
                    spans.append(
                        Span(
                            text=text,
                            bbox=self._vrect(s["bbox"], m),
                            font=s["font"],
                            size=float(s["size"]),
                            color=Color.from_int(int(s["color"])),
                            flags=FontFlags(int(s["flags"]) & 0x1F),
                            origin=self._vpoint(s["origin"], m),
                            chars=chars,
                        )
                    )
                dx, dy = ln["dir"]
                if m is not None:
                    dx, dy = m.a * dx + m.c * dy, m.b * dx + m.d * dy
                lines.append(
                    Line(spans=tuple(spans), bbox=self._vrect(ln["bbox"], m), direction=(dx, dy))
                )
            blocks.append(Block(bbox=self._vrect(b["bbox"], m), lines=tuple(lines)))
        visible = self.fz.rect
        return TextPage(
            page_index=self._index,
            width=float(visible.width),
            height=float(visible.height),
            blocks=tuple(blocks),
        )

    def search(self, needle: str, case_sensitive: bool = False) -> list[Quad]:
        if not needle:
            return []
        quads = self.fz.search_for(needle, quads=True)
        if case_sensitive:
            # MuPDF matches case-insensitively; keep single-line hits whose text matches exactly.
            # Full regex / multi-line case-sensitive search lives in the search service.
            quads = [q for q in quads if needle in self.fz.get_textbox(q.rect)]
        m = self._vis_matrix()
        return [self._vquad(q, m) for q in quads]

    def links(self) -> list[Link]:
        m = self._vis_matrix()
        out: list[Link] = []
        for ln in self.fz.get_links():
            kind = _LINK_KINDS.get(ln.get("kind", 0), LinkKind.OTHER)
            dest = None
            if kind is LinkKind.GOTO and ln.get("page", -1) >= 0:
                to = ln.get("to")
                target = int(ln["page"])
                dest = Destination(
                    page_index=target,
                    # MuPDF already reports link targets in the target page's visible space.
                    point=cv.point(to) if to is not None else None,
                    zoom=float(ln["zoom"]) if ln.get("zoom") else None,
                )
            out.append(
                Link(
                    rect=self._vrect(ln["from"], m),
                    kind=kind,
                    dest=dest,
                    uri=ln.get("uri", "") or "",
                    file=ln.get("file", "") or "",
                    named=str(ln.get("name", "") or ln.get("nameddest", "") or ""),
                )
            )
        return out

    @property
    def pdf_matrix(self) -> Matrix:
        fz = self.fz
        m = fz.derotation_matrix * ~fz.transformation_matrix
        return Matrix(m.a, m.b, m.c, m.d, m.e, m.f)

    def add_annotation(self, model: AnnotationModel) -> AnnotationModel:
        return annots.add(self, model)

    def update_annotation(self, model: AnnotationModel) -> AnnotationModel:
        return annots.update(self, model)

    def delete_annotation(self, annot_id: int | None, name: str = "") -> None:
        annots.delete(self, annot_id, name)

    def annotation_order(self) -> list[int]:
        return annots.order(self)

    def set_annotation_order(self, ids: Sequence[int]) -> None:
        annots.set_order(self, ids)

    def flatten_annotations(self, ids: Sequence[int] | None = None) -> int:
        return annots.flatten(self, ids)

    def set_rotation(self, degrees: int) -> None:
        pages.set_rotation(self, degrees)

    def set_crop(self, rect: Rect) -> None:
        pages.set_crop(self, rect)

    def stamp_text(self, stamp: TextStamp) -> None:
        pages.stamp_text(self, stamp)

    def stamp_image(self, stamp: ImageStamp) -> None:
        pages.stamp_image(self, stamp)

    def fill_background(self, color: Color, opacity: float = 1.0) -> None:
        pages.fill_background(self, color.rgb(), opacity)

    def content_objects(self) -> list[PageObject]:
        return content.list_objects(self)

    def delete_objects(self, keys: Sequence[str]) -> None:
        content.delete_objects(self, keys)

    def transform_objects(self, keys: Sequence[str], matrix: Matrix) -> None:
        content.transform_objects(self, keys, matrix)

    def replace_text(
        self, key: str, text: str, style: TextStyle | None = None
    ) -> FontChoice | None:
        return content.replace_text(self, key, text, style)

    def add_text(self, rect: Rect, text: str, style: TextStyle) -> FontChoice:
        return content.insert_text(self, rect, text, style)

    def add_shape(self, spec: ShapeSpec) -> None:
        content.add_shape(self, spec)

    def image_data(self, key: str) -> tuple[bytes, str]:
        return content.image_data(self, key)

    def replace_image(self, key: str, data: bytes) -> None:
        content.replace_image(self, key, data)

    def ocr_text_layer(
        self, language: str, dpi: int, tessdata: Path, preprocess: bool = False
    ) -> bytes:
        return ocr.text_layer(self, language, dpi, tessdata, preprocess)

    def add_text_layer(self, layer: bytes) -> None:
        ocr.add_text_layer(self, layer)

    def find_tables(self) -> list[TableData]:
        # MuPDF prints a hint about an optional layout package; keep the console clean.
        with contextlib.redirect_stdout(io.StringIO()):
            found = self.fz.find_tables()
        # find_tables already reports visible (rotated) coordinates
        return [
            TableData(self.index, cv.rect(t.bbox), tuple(tuple(row) for row in t.extract()))
            for t in found.tables
        ]

    def image_areas(self) -> list[Rect]:
        m = self._vis_matrix()
        return [
            self._vrect(info["bbox"], m)
            for info in self.fz.get_image_info()
            if pymupdf.Rect(info["bbox"]).is_valid and not pymupdf.Rect(info["bbox"]).is_empty
        ]

    def apply_redactions(self, ids: Sequence[int] | None, options: RedactOptions) -> int:
        return annots.apply_redactions(self, ids, options)

    def annotations(self) -> list[AnnotationModel]:
        return [self._annot_model(a) for a in self.fz.annots()]

    def _annot_model(self, annot: pymupdf.Annot) -> AnnotationModel:
        atype = AnnotationType.from_pdf_name(annot.type[1])
        info = annot.info
        colors = annot.colors or {}
        border = annot.border or {}
        m = self._vis_matrix()
        raw = annot.vertices or []
        vertices: list[Point] = []
        quads: tuple[Quad, ...] = ()
        ink: tuple[tuple[Point, ...], ...] = ()
        if atype is AnnotationType.INK:
            ink = tuple(tuple(self._vpoint(p, m) for p in stroke) for stroke in raw)
        elif atype.is_markup:
            pts = [self._vpoint(v, m) for v in raw]
            quads = tuple(Quad(*pts[i : i + 4]) for i in range(0, len(pts) - 3, 4))
        else:
            vertices = [self._vpoint(v, m) for v in raw]
        state_key = self._doc.fz.xref_get_key(annot.xref, "State")
        state = ReviewState.NONE
        if state_key[0] in ("name", "string"):  # spec says text string; some writers use names
            try:
                state = ReviewState(state_key[1].lstrip("/"))
            except ValueError:
                state = ReviewState.NONE
        ends = annot.line_ends or (0, 0)
        text_color = None
        if atype is AnnotationType.FREE_TEXT:
            text_color = _freetext_color(self._doc.fz.xref_get_key(annot.xref, "DA")[1])
        file_name, file_data = "", None
        if atype is AnnotationType.FILE_ATTACHMENT:
            try:
                file_name = str(annot.file_info.get("filename") or "")
                file_data = bytes(annot.get_file())
            except Exception:  # damaged attachment: keep the annotation readable
                file_name, file_data = "", None
        return AnnotationModel(
            type=atype,
            page_index=self._index,
            rect=self._vrect(annot.rect, m),
            id=annot.xref,
            name=info.get("id", ""),
            contents=info.get("content", ""),
            author=info.get("title", ""),
            subject=info.get("subject", ""),
            created=cv.parse_pdf_date(info.get("creationDate")),
            modified=cv.parse_pdf_date(info.get("modDate")),
            color=cv.color(colors.get("stroke")),
            fill=cv.color(colors.get("fill")),
            opacity=float(annot.opacity)
            if annot.opacity is not None and annot.opacity >= 0
            else 1.0,
            border_width=_border_width(border.get("width")),
            dashes=tuple(float(d) for d in border.get("dashes", ()) or ()),
            quads=quads,
            ink=ink,
            vertices=tuple(vertices),
            line_endings=(_line_end_name(ends[0]), _line_end_name(ends[1])),
            text_color=text_color,
            icon=info.get("name", ""),
            in_reply_to=annot.irt_xref or None,
            state=state,
            flags=int(annot.flags),
            locked=bool(annot.flags & pymupdf.PDF_ANNOT_IS_LOCKED),
            file_name=file_name,
            file_data=file_data,
            overlay_text=_overlay_text(self._doc.fz, annot.xref)
            if atype is AnnotationType.REDACT
            else "",
        )


_LINE_END_NAMES = {
    pymupdf.PDF_ANNOT_LE_NONE: "None",
    pymupdf.PDF_ANNOT_LE_SQUARE: "Square",
    pymupdf.PDF_ANNOT_LE_CIRCLE: "Circle",
    pymupdf.PDF_ANNOT_LE_DIAMOND: "Diamond",
    pymupdf.PDF_ANNOT_LE_OPEN_ARROW: "OpenArrow",
    pymupdf.PDF_ANNOT_LE_CLOSED_ARROW: "ClosedArrow",
    pymupdf.PDF_ANNOT_LE_BUTT: "Butt",
    pymupdf.PDF_ANNOT_LE_R_OPEN_ARROW: "ROpenArrow",
    pymupdf.PDF_ANNOT_LE_R_CLOSED_ARROW: "RClosedArrow",
    pymupdf.PDF_ANNOT_LE_SLASH: "Slash",
}


def _overlay_text(doc: pymupdf.Document, xref: int) -> str:
    kind, value = doc.xref_get_key(xref, "OverlayText")
    return value if kind == "string" else ""


def _border_width(value: float | None) -> float:
    """MuPDF reports -1 when the annotation has no border entry; PDF's default is 1."""
    return float(value) if value is not None and value >= 0 else 1.0


def _line_end_name(code: int) -> str:
    return _LINE_END_NAMES.get(code, "None")


def _freetext_color(da: str) -> Color | None:
    """Pick the fill color from a default-appearance string like ``0 0 1 rg /Helv 12 Tf``."""
    tokens = da.replace("(", " ").replace(")", " ").split()
    for i, tok in enumerate(tokens):
        try:
            if tok == "rg" and i >= 3:
                return Color(*(float(t) for t in tokens[i - 3 : i]))
            if tok == "g" and i >= 1:
                v = float(tokens[i - 1])
                return Color(v, v, v)
        except ValueError:
            return None
    return None
