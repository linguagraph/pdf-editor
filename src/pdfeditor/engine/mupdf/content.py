"""Content editing for the MuPDF backend.

* Images, form XObjects and paths are found with the engine-neutral content-stream parser and
  moved/deleted by rewriting the page's content stream.
* Text is edited a block (paragraph) at a time: the old glyphs are removed with a text-only
  redaction (images and vector art stay) and the new text is typeset in the block's box.
"""

from __future__ import annotations

import html
import io
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

import pymupdf

from pdfeditor.engine.base import EngineError, UnsupportedFeature
from pdfeditor.engine.contentstream.objects import (
    ContentObject,
    ObjectKind,
    XObjectResolver,
    delete_ranges,
    find_objects,
    page_space_edit,
    wrap_ranges,
)
from pdfeditor.engine.contentstream.parser import ContentSyntaxError, Operation, parse, write
from pdfeditor.engine.mupdf import annots
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.model.objects import (
    Align,
    FontChoice,
    ObjectType,
    PageObject,
    ShapeKind,
    ShapeSpec,
    TextStyle,
    family_of,
)
from pdfeditor.model.text import Block

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.page import MuPage

# Standard (base-14) font codes by family and (bold, italic)
_STANDARD = {
    "sans": {
        (False, False): "helv",
        (True, False): "hebo",
        (False, True): "heit",
        (True, True): "hebi",
    },
    "serif": {
        (False, False): "tiro",
        (True, False): "tibo",
        (False, True): "tiit",
        (True, True): "tibi",
    },
    "mono": {
        (False, False): "cour",
        (True, False): "cobo",
        (False, True): "coit",
        (True, True): "cobi",
    },
}
_BASE14 = re.compile(r"^(Helvetica|Arial|Times|Courier)", re.IGNORECASE)
_SUBSET = re.compile(r"^[A-Z]{6}\+")
_KIND_TYPES = {
    ObjectKind.IMAGE: ObjectType.IMAGE,
    ObjectKind.INLINE_IMAGE: ObjectType.IMAGE,
    ObjectKind.FORM: ObjectType.FORM,
    ObjectKind.PATH: ObjectType.PATH,
}


# -- content stream access ----------------------------------------------------------------------
def _resolver(page: MuPage) -> XObjectResolver:
    fz = page.fz
    kinds: dict[str, tuple[str, Rect | None]] = {}
    for entry in fz.get_images(full=True):
        kinds[str(entry[7])] = ("Image", None)
    for _xref, name, invoker, bbox in fz.get_xobjects():
        if invoker == 0:  # drawn by the page itself (nested forms are drawn by other forms)
            kinds[str(name)] = ("Form", Rect(*bbox))  # MuPDF already applied the form /Matrix
    return kinds.get


def _ops(page: MuPage) -> list[Operation]:
    try:
        return parse(page.fz.read_contents())
    except ContentSyntaxError as exc:
        raise EngineError(
            f"page content can't be edited (malformed content stream: {exc})"
        ) from exc


def _content_objects(page: MuPage) -> tuple[list[Operation], list[ContentObject]]:
    ops = _ops(page)
    return ops, find_objects(ops, _resolver(page))


def _write_ops(page: MuPage, ops: list[Operation]) -> None:
    doc = page._doc.fz
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, write(ops))
    doc.xref_set_key(page.fz.xref, "Contents", f"{xref} 0 R")
    page._doc.mark_page_changed(page.index)


def _pdf_to_visible(page: MuPage) -> Matrix:
    return page.pdf_matrix.inverted()


# -- listing ------------------------------------------------------------------------------------
def _type3_fonts(page: MuPage) -> set[str]:
    return {_SUBSET.sub("", f[3]) for f in page.fz.get_fonts(full=True) if f[2] == "Type3"}


def _block_style(block: Block) -> TextStyle:
    counts: dict[tuple[str, float, Color, bool, bool], int] = {}
    for line in block.lines:
        for s in line.spans:
            key = (s.font, round(s.size, 1), s.color, s.is_bold, s.is_italic)
            counts[key] = counts.get(key, 0) + len(s.text)
    font, size, color, bold, italic = max(counts, key=counts.__getitem__)
    lines = block.lines
    line_height = 1.2
    if len(lines) > 1 and size > 0:
        gaps = [lines[i + 1].bbox.y1 - lines[i].bbox.y1 for i in range(len(lines) - 1)]
        line_height = max(0.8, min(3.0, (sum(gaps) / len(gaps)) / size))
    return TextStyle(
        _SUBSET.sub("", font), size, color, bold, italic, _alignment(block), round(line_height, 2)
    )


def _alignment(block: Block) -> Align:
    lines = [ln.bbox for ln in block.lines]
    if len(lines) < 2:
        return Align.LEFT
    tol = 2.0
    b = block.bbox
    lefts = all(abs(r.x0 - b.x0) < tol for r in lines)
    rights = all(abs(r.x1 - b.x1) < tol for r in lines[:-1])
    centers = all(abs(r.center.x - b.center.x) < tol for r in lines)
    if lefts and rights and len(lines) > 2:
        return Align.JUSTIFY
    if lefts:
        return Align.LEFT
    if centers:
        return Align.CENTER
    if all(abs(r.x1 - b.x1) < tol for r in lines):
        return Align.RIGHT
    return Align.LEFT


def _text_blocks(page: MuPage) -> list[Block]:
    """Text blocks in reading order (top to bottom, then left to right), so keys don't depend
    on where an edit put the text in the content stream."""
    blocks = [b for b in page.text_page(with_chars=False).blocks if not b.is_image and b.lines]
    return sorted(blocks, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))


def list_objects(page: MuPage) -> list[PageObject]:
    out: list[PageObject] = []
    type3 = _type3_fonts(page)
    for i, block in enumerate(_text_blocks(page)):
        style = _block_style(block)
        reason = ""
        if any(abs(ln.direction[0] - 1) > 1e-3 for ln in block.lines):
            reason = "rotated or vertical text can't be edited yet"
        elif style.font in type3:
            reason = "text in Type3 fonts can't be edited"
        out.append(
            PageObject(
                ObjectType.TEXT, f"text:{i}", block.bbox, block.text, style, not reason, reason
            )
        )
    to_visible = _pdf_to_visible(page)
    sizes = {str(e[7]): (int(e[2]), int(e[3])) for e in page.fz.get_images(full=True)}
    _ops_list, objects = _content_objects(page)
    for obj in objects:
        out.append(
            PageObject(
                _KIND_TYPES[obj.kind],
                obj.key,
                obj.bbox.transform(to_visible),
                image_size=sizes.get(obj.name, (0, 0)),
                stroke=obj.stroke,
                fill=obj.fill,
            )
        )
    return out


# -- text ---------------------------------------------------------------------------------------
def _text_block(page: MuPage, key: str) -> Block:
    try:
        index = int(key.split(":", 1)[1])
        return _text_blocks(page)[index]
    except (ValueError, IndexError) as exc:
        raise EngineError(f"no text block {key!r} on this page") from exc


def _remove_text(page: MuPage, rects: Sequence[Rect]) -> None:
    """Delete glyphs inside ``rects`` (visible space), leaving images and vector art alone.

    MuPDF applies *all* redaction annotations at once, so the user's own pending redaction
    marks are set aside and restored.
    """
    fz = page.fz
    stashed = annots.stash_redactions(page)
    for r in rects:
        unrot = (pymupdf.Rect(*r.as_tuple()) * fz.derotation_matrix).normalize()
        inner = pymupdf.Rect(unrot.x0 + 0.5, unrot.y0 + 0.5, unrot.x1 - 0.5, unrot.y1 - 0.5)
        fz.add_redact_annot(inner, fill=False)
    try:
        fz.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    finally:
        annots.restore_redactions(page, stashed)
    page._doc.mark_page_changed(page.index)


def _font_for(page: MuPage, style: TextStyle, text: str) -> tuple[bytes, FontChoice]:
    """The font buffer to typeset ``text`` with, preferring the document's own font."""
    doc = page._doc.fz
    wanted = {c for c in text if not c.isspace()}
    original_embedded = False
    for xref, ext, _ftype, basefont, *_ in page.fz.get_fonts(full=True):
        if _SUBSET.sub("", basefont) != style.font or ext in ("n/a", ""):
            continue
        original_embedded = True
        try:
            _name, _ext, _type, buffer = doc.extract_font(xref)
        except Exception:
            break
        if buffer and _covers(buffer, wanted):
            return buffer, FontChoice(style.font, embedded_reused=True)
        break
    code = _STANDARD[family_of(style.font)][(style.bold, style.italic)]
    font = pymupdf.Font(code)
    substituted = original_embedded or not _BASE14.match(style.font)
    return font.buffer, FontChoice(font.name, substituted=substituted)


def _covers(buffer: bytes, chars: set[str]) -> bool:
    try:
        from fontTools.ttLib import TTFont

        cmap = TTFont(io.BytesIO(buffer), lazy=True).getBestCmap() or {}
    except Exception:
        return False  # CFF/Type1 or damaged: don't risk missing glyphs
    return all(ord(c) in cmap for c in chars)


def insert_text(page: MuPage, rect: Rect, text: str, style: TextStyle) -> FontChoice:
    """Typeset ``text`` in ``rect`` (visible space), growing downward if it doesn't fit."""
    buffer, choice = _font_for(page, style, text)
    archive = pymupdf.Archive()
    archive.add(buffer, "edit-font")
    body = "<br>".join(html.escape(line) for line in text.split("\n"))
    css = (
        '@font-face {font-family: EditFont; src: url("edit-font");} '
        "body {font-family: EditFont; margin: 0; "
        f"font-size: {style.size:.2f}pt; color: {style.color.to_hex()}; "
        f"text-align: {style.align.value}; line-height: {style.line_height}; "
        "font-weight: normal; font-style: normal;} p {margin: 0; padding: 0;}"
    )
    fz = page.fz
    page_bottom = fz.rect.height
    height = max(rect.height, style.size * style.line_height)
    while True:
        box = Rect(rect.x0, rect.y0, rect.x1, min(page_bottom, rect.y0 + height))
        unrot = (pymupdf.Rect(*box.as_tuple()) * fz.derotation_matrix).normalize()
        spare, _scale = fz.insert_htmlbox(
            unrot, f"<p>{body}</p>", css=css, archive=archive, scale_low=1, rotate=fz.rotation
        )
        if spare >= 0:
            break
        if box.y1 >= page_bottom:
            raise EngineError("the text doesn't fit on the page")
        height *= 1.6
    page._doc.mark_page_changed(page.index)
    return choice


def replace_text(
    page: MuPage, key: str, text: str, style: TextStyle | None = None
) -> FontChoice | None:
    """Replace a text block's content; ``style`` defaults to the block's own style."""
    block = _text_block(page, key)
    obj = next(o for o in list_objects(page) if o.key == key)
    if not obj.editable:
        raise UnsupportedFeature(obj.reason)
    style = style or obj.style or TextStyle()
    _remove_text(page, [block.bbox])
    if not text.strip():
        return None
    return insert_text(page, block.bbox, text, style)


# -- objects ------------------------------------------------------------------------------------
def delete_objects(page: MuPage, keys: Sequence[str]) -> None:
    text_rects = [_text_block(page, k).bbox for k in keys if k.startswith("text:")]
    others = [k for k in keys if not k.startswith("text:")]
    if others:
        ops, objects = _content_objects(page)
        by_key = {o.key: o for o in objects}
        missing = [k for k in others if k not in by_key]
        if missing:
            raise EngineError(f"objects not found: {missing}")
        _write_ops(page, delete_ranges(ops, [(by_key[k].start, by_key[k].end) for k in others]))
    if text_rects:
        _remove_text(page, text_rects)


def transform_objects(page: MuPage, keys: Sequence[str], visible: Matrix) -> None:
    """Move/resize/rotate objects by ``visible`` (a matrix in visible page space)."""
    texts = [(k, _text_block(page, k)) for k in keys if k.startswith("text:")]
    text_styles = {o.key: o for o in list_objects(page) if o.key in dict(texts)}
    others = [k for k in keys if not k.startswith("text:")]
    if others:
        a = page.pdf_matrix
        pdf_transform = a.inverted() @ visible @ a
        ops, objects = _content_objects(page)
        by_key = {o.key: o for o in objects}
        edits = []
        for k in others:
            obj = by_key.get(k)
            if obj is None:
                raise EngineError(f"object {k!r} not found")
            x = page_space_edit(obj, pdf_transform)
            if x is not None:
                edits.append((obj.start, obj.end, x))
        _write_ops(page, wrap_ranges(ops, edits))
    for key, block in texts:
        item = text_styles[key]
        if not item.editable:
            raise UnsupportedFeature(item.reason)
        _remove_text(page, [block.bbox])
        target = block.bbox.transform(visible)
        insert_text(page, target, block.text, item.style or TextStyle())


def image_data(page: MuPage, key: str) -> tuple[bytes, str]:
    xref = _image_xref(page, key)
    info = page._doc.fz.extract_image(xref)
    return bytes(info["image"]), str(info["ext"])


def replace_image(page: MuPage, key: str, data: bytes) -> None:
    """Swap the picture (every place this image is drawn in the document shows the new one)."""
    xref = _image_xref(page, key)
    try:
        page.fz.replace_image(xref, stream=data)
    except Exception as exc:
        raise EngineError(f"not a supported image: {exc}") from exc
    page._doc.structure_changed()


def _image_xref(page: MuPage, key: str) -> int:
    _ops_list, objects = _content_objects(page)
    obj = next((o for o in objects if o.key == key), None)
    if obj is None or obj.kind is not ObjectKind.IMAGE:
        raise EngineError(f"{key!r} is not an image XObject")
    for entry in page.fz.get_images(full=True):
        if str(entry[7]) == obj.name:
            return int(entry[0])
    raise EngineError(f"image {obj.name!r} not found in the page resources")


def add_shape(page: MuPage, spec: ShapeSpec) -> None:
    fz = page.fz
    shape = fz.new_shape()
    m = fz.derotation_matrix
    if spec.kind is ShapeKind.LINE:
        shape.draw_line(
            pymupdf.Point(spec.start.x, spec.start.y) * m, pymupdf.Point(spec.end.x, spec.end.y) * m
        )
    else:
        rect = (pymupdf.Rect(*spec.rect.as_tuple()) * m).normalize()
        if spec.kind is ShapeKind.RECTANGLE:
            shape.draw_rect(rect)
        else:
            shape.draw_oval(rect)
    shape.finish(
        color=spec.stroke.rgb() if spec.stroke else None,
        fill=spec.fill.rgb() if spec.fill else None,
        width=spec.width,
    )
    shape.commit(overlay=True)
    page._doc.mark_page_changed(page.index)
