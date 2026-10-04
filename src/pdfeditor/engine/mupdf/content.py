"""Content editing for the MuPDF backend.

* Images, form XObjects and paths are found with the engine-neutral content-stream parser and
  moved/deleted by rewriting the page's content stream.
* Text is edited a block (paragraph) at a time: the old glyphs are removed with a text-only
  redaction (images and vector art stay) and the new text is typeset in the block's box.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
import re
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
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
from pdfeditor.engine.contentstream.spacing import LineSpacing, Spacing, apply_spacing
from pdfeditor.engine.mupdf import annots
from pdfeditor.engine.mupdf.flatten import flatten_inserted_forms
from pdfeditor.engine.textlayout import editable_blocks
from pdfeditor.engine.textspacing import detect_spacing, spaced_text
from pdfeditor.model.color import Color
from pdfeditor.model.fonts import BITMAP_ONLY, RESTRICTED, FontRefKind
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

log = logging.getLogger(__name__)

# fontTools warns about some real-world fonts' implausible 'created' timestamp; it's noise here.
logging.getLogger("fontTools.ttLib.tables._h_e_a_d").setLevel(logging.ERROR)

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
# operators that leave marks on the page (text shows count unless the render mode is 3)
_PAINT = frozenset({"f", "F", "f*", "B", "B*", "b", "b*", "S", "s", "sh", "BI", "EI", "d0", "d1"})
_SHOW = frozenset({"Tj", "TJ", "'", '"'})
_XOBJECT_ENTRY = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R")
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
    align = _alignment(block)
    char_spacing, word_spacing = detect_spacing(block)
    if align is Align.JUSTIFY:
        word_spacing = 0.0  # justification sets the word gaps
    return TextStyle(
        _SUBSET.sub("", font),
        size,
        color,
        bold,
        italic,
        align,
        round(line_height, 2),
        char_spacing,
        word_spacing,
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
    """Editable paragraphs in reading order (top to bottom, then left to right), so keys don't
    depend on where an edit put the text in the content stream. Multi-column rows are split
    into their columns (see ``engine.textlayout``)."""
    return editable_blocks(page.text_page(with_chars=True).blocks)


def _form_xobjects(doc: pymupdf.Document, form: int) -> dict[str, int]:
    kind, value = doc.xref_get_key(form, "Resources/XObject")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "dict":
        return {}
    return {m.group(1): int(m.group(2)) for m in _XOBJECT_ENTRY.finditer(value)}


def _invisible_form(doc: pymupdf.Document, xref: int, depth: int = 0) -> bool:
    """A form XObject that draws nothing visible: only invisible text (render mode 3), such as
    an OCR layer added by an older version as a page-sized form. Listing it would let the Edit
    tool select and drag it, moving the text boxes and nothing the user can see."""
    if depth > 4:
        return False
    try:
        ops = parse(doc.xref_stream(xref) or b"")
    except ContentSyntaxError:
        return False
    names: dict[str, int] | None = None
    render_mode = 0.0
    shows = False
    for op in ops:
        if op.operator == "Tr" and op.operands and isinstance(op.operands[0], int | float):
            render_mode = float(op.operands[0])
        elif op.operator in _SHOW:
            if render_mode != 3:
                return False
            shows = True
        elif op.operator in _PAINT:
            return False
        elif op.operator == "Do":
            names = _form_xobjects(doc, xref) if names is None else names
            target = names.get(str(op.operands[0])) if op.operands else None
            if target is None or doc.xref_get_key(target, "Subtype")[1] != "/Form":
                return False
            if not _invisible_form(doc, target, depth + 1):
                return False
            shows = True
    return shows


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
        text = spaced_text(block, style.char_spacing)
        out.append(
            PageObject(ObjectType.TEXT, f"text:{i}", block.bbox, text, style, not reason, reason)
        )
    to_visible = _pdf_to_visible(page)
    sizes = {str(e[7]): (int(e[2]), int(e[3])) for e in page.fz.get_images(full=True)}
    _ops_list, objects = _content_objects(page)
    forms = {
        str(name): int(xref) for xref, name, invoker, _bbox in page.fz.get_xobjects() if not invoker
    }
    for obj in objects:
        form = forms.get(obj.name) if obj.kind is ObjectKind.FORM else None
        if form is not None and _invisible_form(page._doc.fz, form):
            continue
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


def font_key(name: str) -> str:
    """Compare font names loosely: text extraction reports the font program's own name
    ("SegoeUI") while the page lists its BaseFont ("ABCDEF+Segoe UI Regular")."""
    key = re.sub(r"[^a-z0-9]", "", _SUBSET.sub("", name).lower())
    return key.removesuffix("regular").removesuffix("mt").removesuffix("ps") or key


def font_program(page: MuPage, name: str) -> bytes | None:
    """The embedded font program used on the page under ``name`` (None if not embedded)."""
    wanted = font_key(name)
    for xref, ext, _ftype, basefont, *_ in page.fz.get_fonts(full=True):
        if ext in ("n/a", "") or font_key(basefont) != wanted:
            continue
        try:
            buffer = page._doc.fz.extract_font(xref)[3]
        except Exception:
            return None
        return bytes(buffer) if buffer else None
    return None


_FILE_FONT_CACHE_MAX = 8
# (path, face index, mtime) -> standalone font program; avoids re-reading large font files on
# every edit, and a stat()-based key so an edited/replaced font file is picked up again.
_file_font_cache: OrderedDict[tuple[str, int, float], bytes] = OrderedDict()


def _check_embeddable(fs_type: int, path: str) -> None:
    if fs_type & RESTRICTED:
        raise UnsupportedFeature(f"{path!r} can't be embedded: its license forbids embedding")
    if fs_type & BITMAP_ONLY:
        raise UnsupportedFeature(f"{path!r} can't be embedded: its license allows bitmaps only")


def _extract_face(path: str, index: int) -> bytes:
    """``path``'s face ``index`` as a standalone TTF/OTF buffer (collections are split apart;
    PDF font embedding doesn't support .ttc/.otc). Raises :class:`UnsupportedFeature` if the
    font's own fsType forbids embedding; never leaves a file handle open (Windows locks them)."""
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.ttCollection import TTCollection

    suffix = Path(path).suffix.lower()
    if suffix in (".ttc", ".otc"):
        collection = TTCollection(path, lazy=True)
        try:
            if not 0 <= index < len(collection.fonts):
                raise UnsupportedFeature(f"font face {index} not found in {path!r}")
            font = collection.fonts[index]
            os2 = font.get("OS/2")
            _check_embeddable(int(getattr(os2, "fsType", 0)) if os2 is not None else 0, path)
            out = io.BytesIO()
            font.save(out)
            return out.getvalue()
        finally:
            collection.close()
    font = TTFont(path, lazy=True, fontNumber=index)
    try:
        os2 = font.get("OS/2")
        _check_embeddable(int(getattr(os2, "fsType", 0)) if os2 is not None else 0, path)
        out = io.BytesIO()
        font.save(out)
        return out.getvalue()
    finally:
        font.close()


def load_file_font(path: str, index: int) -> bytes:
    try:
        mtime = Path(path).stat().st_mtime
    except OSError as exc:
        raise UnsupportedFeature(f"font file not found: {path}") from exc
    key = (path, index, mtime)
    cached = _file_font_cache.get(key)
    if cached is not None:
        _file_font_cache.move_to_end(key)
        return cached
    try:
        buffer = _extract_face(path, index)
    except UnsupportedFeature:
        raise
    except Exception as exc:
        raise EngineError(f"can't read font file {path!r}: {exc}") from exc
    _file_font_cache[key] = buffer
    _file_font_cache.move_to_end(key)
    if len(_file_font_cache) > _FILE_FONT_CACHE_MAX:
        _file_font_cache.popitem(last=False)
    return buffer


def _missing_chars(buffer: bytes, chars: set[str]) -> str:
    """The characters of ``chars`` the font program can't show, for the substitution notice."""
    try:
        from fontTools.ttLib import TTFont

        cmap = TTFont(io.BytesIO(buffer), lazy=True).getBestCmap() or {}
        return "".join(sorted(c for c in chars if ord(c) not in cmap))
    except Exception:
        try:
            font = pymupdf.Font(fontbuffer=buffer)
            return "".join(sorted(c for c in chars if not font.has_glyph(ord(c))))
        except Exception:
            return "".join(sorted(chars))


def _font_for_known(
    page: MuPage, style: TextStyle, wanted: set[str], reuse_embedded: bool
) -> tuple[bytes, FontChoice]:
    """The embedded-reuse-then-base-14 fallback used for ``DOCUMENT``/no ``font_ref``, and as
    the fallback when a ``FILE`` ref doesn't cover the text."""
    buffer = font_program(page, style.font)
    original_embedded = buffer is not None
    if reuse_embedded and buffer and _covers(buffer, wanted):
        return _single_codepoint_cmap(buffer, wanted), FontChoice(style.font, embedded_reused=True)
    code = _STANDARD[family_of(style.font)][(style.bold, style.italic)]
    font = pymupdf.Font(code)
    substituted = original_embedded or not _BASE14.match(style.font)
    return font.buffer, FontChoice(font.name, substituted=substituted)


def _font_for(
    page: MuPage, style: TextStyle, text: str, reuse_embedded: bool = True
) -> tuple[bytes, FontChoice]:
    """The font buffer to typeset ``text`` with, preferring the document's own font.

    ``style.font_ref`` picks the font explicitly: ``FILE`` embeds that font file (falling back,
    with a substitution notice listing the missing characters, if it doesn't cover ``text``),
    ``STANDARD`` goes straight to the matching base-14 font, and ``DOCUMENT`` (or no ref at all)
    keeps today's embedded-font reuse.
    """
    wanted = {c for c in text if not c.isspace()}
    ref = style.font_ref
    if ref is not None and ref.kind is FontRefKind.FILE:
        buffer = load_file_font(ref.path, ref.index)
        if _covers(buffer, wanted):
            page._doc.note_file_font_embedded()
            return _single_codepoint_cmap(buffer, wanted), FontChoice(ref.name or style.font)
        missing = _missing_chars(buffer, wanted)
        fallback_buffer, choice = _font_for_known(page, style, wanted, reuse_embedded)
        return fallback_buffer, replace(choice, substituted=True, missing=missing)
    if ref is not None and ref.kind is FontRefKind.STANDARD:
        code = _STANDARD[family_of(style.font)][(style.bold, style.italic)]
        font = pymupdf.Font(code)
        missing = "".join(sorted(c for c in wanted if not font.has_glyph(ord(c))))
        return font.buffer, FontChoice(font.name, substituted=bool(missing), missing=missing)
    if ref is not None and ref.kind is FontRefKind.DOCUMENT and ref.path:
        # the document's own program first; the installed copy for what it can't show
        embedded = font_program(page, style.font)
        if not (reuse_embedded and embedded and _covers(embedded, wanted)):
            try:
                installed = load_file_font(ref.path, ref.index)
            except (OSError, UnsupportedFeature, EngineError):
                installed = None
            if installed is not None and _covers(installed, wanted):
                page._doc.note_file_font_embedded()
                return _single_codepoint_cmap(installed, wanted), FontChoice(style.font)
    return _font_for_known(page, style, wanted, reuse_embedded)


def _single_codepoint_cmap(buffer: bytes, used: set[str]) -> bytes:
    """Give every glyph a single Unicode value.

    Fonts often map several code points to one glyph ("(" and the ornate U+FD3E, "-" and
    U+2010). MuPDF writes the ToUnicode map by reverse lookup and may pick the exotic one, so
    the edited text would copy and search wrong. Keep the code point the text uses, else the
    lowest one.
    """
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(io.BytesIO(buffer))
        best = font.getBestCmap() or {}
    except Exception:
        return buffer  # not a TrueType/OpenType program: leave it alone
    by_glyph: dict[str, list[int]] = {}
    for code, glyph in best.items():
        by_glyph.setdefault(glyph, []).append(code)
    wanted = {ord(c) for c in used}
    drop: set[int] = set()
    for codes in by_glyph.values():
        if len(codes) > 1:
            keep = min((c for c in codes if c in wanted), default=min(codes))
            drop.update(c for c in codes if c != keep)
    if not drop:
        return buffer
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap = {c: g for c, g in table.cmap.items() if c not in drop}
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def _covers(buffer: bytes, chars: set[str]) -> bool:
    try:
        from fontTools.ttLib import TTFont

        cmap = TTFont(io.BytesIO(buffer), lazy=True).getBestCmap() or {}
    except Exception:
        # bare CFF/Type1 programs: ask MuPDF (a font without a Unicode map reports no glyphs,
        # so we never risk missing ones)
        try:
            font = pymupdf.Font(fontbuffer=buffer)
            return all(font.has_glyph(ord(c)) for c in chars)
        except Exception:
            return False
    return all(ord(c) in cmap for c in chars)


def insert_text(
    page: MuPage,
    rect: Rect,
    text: str,
    style: TextStyle,
    reuse_embedded: bool = True,
    keep_lines: bool = False,
    font: tuple[bytes, FontChoice] | None = None,
    baseline: float | None = None,
) -> FontChoice:
    """Typeset ``text`` in ``rect`` (visible space), growing downward if it doesn't fit.

    The box widens (up to the page edge) so no word is broken, and with ``keep_lines`` so that
    no line wraps: a one-line label stays one line after it gets longer.
    """
    buffer, choice = font or _font_for(page, style, text, reuse_embedded)
    spaced = _Spaced(buffer, style) if style.char_spacing or style.word_spacing else None
    rect = _widen(page, rect, text, style, buffer, keep_lines, spaced)
    if baseline is not None:
        rect = _on_baseline(rect, baseline, style, buffer)
    line_spacing: tuple[LineSpacing, ...] = ()
    if spaced is not None:
        # MuPDF's HTML layout ignores letter-/word-spacing: break the lines here, lay them out
        # left-aligned in a box wide enough to never wrap, then space and align them (below)
        lines = spaced.wrap(text, rect.width, keep_lines)
        line_spacing = spaced.line_spacing(lines, rect.width)
        style = replace(style, align=Align.LEFT)
        need = max((spaced.nominal(t) for t, _soft in lines), default=0.0) * 1.03 + 2
        right = min(max(rect.x1, rect.x0 + need), page.fz.rect.width)
        rect = Rect(rect.x0, rect.y0, right, rect.y1)
        body = "<br>".join(html.escape(t) for t, _soft in lines)
    else:
        body = "<br>".join(html.escape(line) for line in text.split("\n"))
    archive, css = _html_setup(buffer, style)
    fz = page.fz
    page_bottom = fz.rect.height
    height = max(rect.height, style.size * style.line_height)
    before_contents = set(fz.get_contents())  # insert_htmlbox rolls back on failure: safe to
    # capture once, outside the retry loop below
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
    # insert_htmlbox always draws through a Form XObject; inline it so content_objects() doesn't
    # report a spurious empty "form" sitting on top of the text we just placed.
    flatten_inserted_forms(fz, before_contents)
    if spaced is not None:
        spaced.apply(page, before_contents, line_spacing)
    page._doc.mark_page_changed(page.index)
    return choice


class _Spaced:
    """Measuring, line breaking and post-layout spacing for text with character/word spacing
    (PDF ``Tc``/``Tw``), which MuPDF's HTML layout can't do itself."""

    def __init__(self, buffer: bytes, style: TextStyle) -> None:
        self.font = pymupdf.Font(fontbuffer=buffer)
        self.style = style

    def nominal(self, text: str) -> float:
        return float(self.font.text_length(text, fontsize=self.style.size))

    def width(self, text: str) -> float:
        """Visible width of one line once spaced (the last glyph's spacing is blank)."""
        text = text.rstrip()
        s = self.style
        extra = s.char_spacing * max(0, len(text) - 1) + s.word_spacing * text.count(" ")
        return self.nominal(text) + extra

    def wrap(self, text: str, width: float, keep_lines: bool) -> list[tuple[str, bool]]:
        """Lines as ``(text, soft)``: ``soft`` when the line was wrapped (not ended by the
        user), which is what justification stretches."""
        out: list[tuple[str, bool]] = []
        for para in text.split("\n"):
            if keep_lines:
                out.append((para, False))
                continue
            words = para.split(" ")
            line = words[0]
            for word in words[1:]:
                candidate = f"{line} {word}"
                if not line.strip() or self.width(candidate) <= width + _WRAP_TOLERANCE:
                    line = candidate
                else:
                    out.append((line, True))
                    line = word
            out.append((line, False))
        return out

    def line_spacing(self, lines: list[tuple[str, bool]], width: float) -> tuple[LineSpacing, ...]:
        """Shift/fill for each line that will be drawn (blank lines draw nothing)."""
        align = self.style.align
        out: list[LineSpacing] = []
        for text, soft in lines:
            if not text.strip():
                continue
            slack = width - self.width(text)
            spaces = text.rstrip().count(" ")
            if align is Align.CENTER:
                out.append(LineSpacing(shift=slack / 2))
            elif align is Align.RIGHT:
                out.append(LineSpacing(shift=slack))
            elif align is Align.JUSTIFY and soft and spaces:
                out.append(LineSpacing(fill=slack / spaces))
            else:
                out.append(LineSpacing())
        return tuple(out)

    def apply(
        self, page: MuPage, before_contents: set[int], lines: tuple[LineSpacing, ...]
    ) -> None:
        """Space the text just inserted (the content streams added since ``before_contents``)."""
        gid = self.font.has_glyph(32)
        spacing = Spacing(
            self.style.char_spacing,
            self.style.word_spacing,
            gid.to_bytes(2, "big") if gid else None,
            lines,
        )
        doc = page._doc.fz
        for xref in page.fz.get_contents():
            if xref in before_contents:
                continue
            try:
                ops = parse(doc.xref_stream(xref) or b"")
            except ContentSyntaxError:
                log.debug("inserted text not spaced: unreadable content stream", exc_info=True)
                continue
            doc.update_stream(xref, write(apply_spacing(ops, spacing)))


_WRAP_TOLERANCE = 0.5  # points: a line as wide as the original box must still fit


def _widen(
    page: MuPage,
    rect: Rect,
    text: str,
    style: TextStyle,
    buffer: bytes,
    keep_lines: bool,
    spaced: _Spaced | None = None,
) -> Rect:
    pieces = text.splitlines() if keep_lines else text.split()
    if spaced is not None:
        # exact: the spaced path breaks lines itself, so no slack for HTML rounding is needed
        need = max((spaced.width(p) for p in pieces), default=0.0) - _WRAP_TOLERANCE
    else:
        font = pymupdf.Font(fontbuffer=buffer)
        need = max((font.text_length(p, fontsize=style.size) for p in pieces), default=0.0)
        need = need * 1.03 + 2  # rounding in the HTML layout
    if need <= rect.width:
        return rect
    right = max(rect.x1, min(page.fz.rect.width - 2, rect.x0 + need))
    return Rect(rect.x0, rect.y0, right, rect.y1)


def _html_setup(buffer: bytes, style: TextStyle) -> tuple[pymupdf.Archive, str]:
    archive = pymupdf.Archive()
    archive.add(buffer, "edit-font")
    css = (
        '@font-face {font-family: EditFont; src: url("edit-font");} '
        "body {font-family: EditFont; margin: 0; "
        f"font-size: {style.size:.2f}pt; color: {style.color.to_hex()}; "
        f"text-align: {style.align.value}; line-height: {style.line_height}; "
        "font-weight: normal; font-style: normal; white-space: pre-wrap;} "
        "p {margin: 0; padding: 0;}"
    )
    return archive, css


_BASELINE_CACHE: dict[tuple[str, float, float], float] = {}


def _baseline_offset(buffer: bytes, style: TextStyle) -> float:
    """Distance from the box top to the first baseline in MuPDF's HTML layout, measured by
    typesetting a sample on a scratch page (cached per font, size and line height)."""
    key = (hashlib.sha1(buffer).hexdigest(), style.size, style.line_height)
    if key not in _BASELINE_CACHE:
        archive, css = _html_setup(buffer, style)
        scratch = pymupdf.open()
        try:
            page = scratch.new_page(width=400, height=200)
            page.insert_htmlbox(
                pymupdf.Rect(10, 50, 390, 190), "<p>Hx</p>", css=css, archive=archive
            )
            spans = [
                s
                for b in page.get_text("dict")["blocks"]
                for ln in b.get("lines", [])
                for s in ln["spans"]
                if s["text"].strip()
            ]
            offset = spans[0]["origin"][1] - 50 if spans else style.size * 0.9
        finally:
            scratch.close()
        _BASELINE_CACHE[key] = offset
    return _BASELINE_CACHE[key]


def _on_baseline(rect: Rect, baseline: float, style: TextStyle, buffer: bytes) -> Rect:
    """Move ``rect`` so the first typeset line sits on ``baseline`` (visible space); otherwise
    an edited line lands a point or so off and drifts further with every edit."""
    top = baseline - _baseline_offset(buffer, style)
    return Rect(rect.x0, top, rect.x1, top + rect.height)


def _baseline(block: Block) -> float | None:
    spans = [s for s in block.lines[0].spans if s.text.strip()] if block.lines else []
    return spans[0].origin.y if spans else None


def replace_text(
    page: MuPage, key: str, text: str, style: TextStyle | None = None
) -> FontChoice | None:
    """Replace a text block's content; ``style`` defaults to the block's own style."""
    block = _text_block(page, key)
    obj = next(o for o in list_objects(page) if o.key == key)
    if not obj.editable:
        raise UnsupportedFeature(obj.reason)
    original = obj.style or TextStyle()
    style = style or original
    # the document's font program only has the original weight/slant
    same_face = font_key(style.font) == font_key(original.font) and (
        style.bold,
        style.italic,
    ) == (original.bold, original.italic)
    # pick the font first: removing the old text can drop an unused font from the resources
    font = _font_for(page, style, text, reuse_embedded=same_face)
    _remove_text(page, [block.bbox])
    if not text.strip():
        return None
    single_line = len(block.lines) == 1
    return insert_text(
        page,
        block.bbox,
        text,
        style,
        keep_lines=single_line,
        font=font,
        baseline=_baseline(block),
    )


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
    for key, _block in texts:
        if not text_styles[key].editable:
            raise UnsupportedFeature(text_styles[key].reason)
    # resolve fonts before removing anything (removal can drop now-unused fonts)
    fonts = {
        key: _font_for(page, text_styles[key].style or TextStyle(), text_styles[key].text)
        for key, block in texts
    }
    if texts:
        _remove_text(page, [block.bbox for _key, block in texts])
    for key, block in texts:
        insert_text(
            page,
            block.bbox.transform(visible),
            text_styles[key].text,
            text_styles[key].style or TextStyle(),
            keep_lines=len(block.lines) == 1,
            font=fonts[key],
            baseline=_moved_baseline(block, visible),
        )


def _moved_baseline(block: Block, visible: Matrix) -> float | None:
    """The block's baseline after a move; None when resized (the box decides then)."""
    if abs(visible.a - 1) > 1e-6 or abs(visible.d - 1) > 1e-6:
        return None
    base = _baseline(block)
    return None if base is None else base + visible.f


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
