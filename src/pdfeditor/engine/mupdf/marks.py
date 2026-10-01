"""Page marks for the MuPDF backend: tag stamps as artifacts, find them, remove them.

Each stamp MuPDF draws lands in a content stream of its own; it is wrapped in an ``/Artifact``
section naming the feature (see ``contentstream.artifacts``). The settings used are kept in
the page's piece dictionary (PDF 32000-1 14.5), ``/PieceInfo /PDFEditor /Private /<feature>``,
as a UTF-8 JSON stream, so they survive saving and travel with the page.

Acrobat's own headers, footers, watermarks and backgrounds are recognised by their piece info
(``/ADBE_CompoundType /Private /Watermark`` on the form XObject the artifact section draws);
other pagination artifacts (a word processor's running headers, say) are real document
content and are never touched.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pymupdf

from pdfeditor.engine.contentstream.artifacts import (
    ArtifactSection,
    Properties,
    artifact_sections,
    begin_operator,
    remove_sections,
    section_xobjects,
)
from pdfeditor.engine.contentstream.parser import ContentSyntaxError, Operation, parse, write
from pdfeditor.engine.mupdf.sanitize import _category, _drop_entries, _resources
from pdfeditor.model.pages import MarkKind, PageMark

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.page import MuPage

log = logging.getLogger(__name__)

PIECE = "PieceInfo/PDFEditor"
_KINDS = {k.value: k for k in MarkKind}
_ACROBAT = {
    "Header": MarkKind.HEADER_FOOTER,
    "Footer": MarkKind.HEADER_FOOTER,
    "Watermark": MarkKind.WATERMARK,
    "Background": MarkKind.BACKGROUND,
}


def artifact_kind(kind: MarkKind, top_half: bool) -> tuple[str, str | None]:
    """Standard artifact /Type and /Subtype for a mark (Background is its own type)."""
    if kind is MarkKind.BACKGROUND:
        return "Background", None
    if kind is MarkKind.WATERMARK:
        return "Pagination", "Watermark"
    return "Pagination", "Header" if top_half else "Footer"


@contextmanager
def marked(page: MuPage, kind: MarkKind | None, top_half: bool = False) -> Iterator[None]:
    """Wrap whatever content the body adds to ``page`` as a ``kind`` mark."""
    if kind is None:
        yield
        return
    fz = page.fz
    if not fz.is_wrapped:
        fz.wrap_contents()  # now, so the q/Q streams MuPDF adds aren't taken for the stamp
    before = set(fz.get_contents())
    yield
    doc = fz.parent
    begin = begin_operator(kind.value, *artifact_kind(kind, top_half))
    for xref in fz.get_contents():
        if xref not in before:
            doc.update_stream(xref, begin + (doc.xref_stream(xref) or b"") + b"\nEMC\n")


# -- finding ------------------------------------------------------------------------------------
def _sections(page: MuPage) -> tuple[list[Operation], list[tuple[ArtifactSection, MarkKind, bool]]]:
    """The page's operations and its marks as (section, kind, foreign)."""
    data = page.fz.read_contents()
    if b"Artifact" not in data:  # cheap exit: most pages have none
        return [], []
    try:
        ops = parse(data)
    except ContentSyntaxError:
        log.warning("page %d: content can't be parsed; marks not searched", page.index)
        return [], []
    doc = page._doc.fz
    page_xref = page.fz.xref
    found = []
    for section in artifact_sections(ops, lambda name: _resolve(doc, page_xref, name)):
        app = section.app_mark
        if app is not None:
            if app in _KINDS:
                found.append((section, _KINDS[app], False))
            continue
        if section.name("Type") not in ("Pagination", "Background"):
            continue
        acrobat = _acrobat_kind(doc, page_xref, section_xobjects(ops, section))
        if acrobat is not None:
            found.append((section, acrobat, True))
    return ops, found


def _resolve(doc: pymupdf.Document, page_xref: int, name: str) -> Properties | None:
    scope = _resources(doc, page_xref, page=True)
    if scope is None:
        return None
    owner, prefix = scope
    kind, value = doc.xref_get_key(owner, f"{prefix}Properties/{name}")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "dict":
        return None
    try:
        (op,) = parse(value.encode("latin-1") + b" DP")
    except (ContentSyntaxError, UnicodeEncodeError, ValueError):
        return None
    props = op.operands[0] if op.operands else None
    return props if isinstance(props, dict) else None


def _acrobat_kind(doc: pymupdf.Document, page_xref: int, names: list[str]) -> MarkKind | None:
    scope = _resources(doc, page_xref, page=True)  # pages may inherit their resources
    xobjects = _category(doc, scope, "XObject") if scope is not None and names else {}
    for name in names:
        if name not in xobjects:
            continue
        kind, private = doc.xref_get_key(xobjects[name], "PieceInfo/ADBE_CompoundType/Private")
        if kind == "name" and private.lstrip("/") in _ACROBAT:
            return _ACROBAT[private.lstrip("/")]
    return None


def page_marks(page: MuPage) -> list[PageMark]:
    _ops, found = _sections(page)
    kinds: dict[MarkKind, bool] = {}
    for _section, kind, foreign in found:
        kinds[kind] = kinds.get(kind, True) and foreign
    return [
        PageMark(kind, "" if foreign else _settings(page, kind), foreign)
        for kind, foreign in kinds.items()
    ]


# -- settings -----------------------------------------------------------------------------------
def _settings(page: MuPage, kind: MarkKind) -> str:
    doc = page._doc.fz
    ref, value = doc.xref_get_key(page.fz.xref, f"{PIECE}/Private/{kind.value}")
    if ref != "xref":
        return ""
    try:
        return (doc.xref_stream(int(value.split()[0])) or b"").decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""


def set_settings(page: MuPage, kind: MarkKind, settings: str) -> None:
    doc = page._doc.fz
    page_xref = page.fz.xref
    if not settings and doc.xref_get_key(page_xref, f"{PIECE}/Private/{kind.value}")[0] == "null":
        return
    # MuPDF sets keys only along direct dictionaries: start below an indirect /PieceInfo
    # (another application's); everything under /PDFEditor is ours and direct.
    ref, value = doc.xref_get_key(page_xref, "PieceInfo")
    owner, prefix = (int(value.split()[0]), "") if ref == "xref" else (page_xref, "PieceInfo/")
    key = f"{prefix}PDFEditor/Private/{kind.value}"
    try:
        if not settings:
            doc.xref_set_key(owner, key, "null")
            return
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, settings.encode("utf-8"))
        now = pymupdf.get_pdf_str(pymupdf.get_pdf_now())
        doc.xref_set_key(owner, key, f"{xref} 0 R")
        doc.xref_set_key(owner, f"{prefix}PDFEditor/LastModified", now)
        doc.xref_set_key(page_xref, "LastModified", now)  # required with /PieceInfo
    except Exception:  # an indirect /PDFEditor or /Private written by someone else
        log.warning("page %d: mark settings couldn't be stored", page.index, exc_info=True)


# -- removing -----------------------------------------------------------------------------------
def remove_marks(page: MuPage, kinds: Collection[MarkKind]) -> int:
    wanted = set(kinds)
    ops, found = _sections(page)
    doomed = [section for section, kind, _foreign in found if kind in wanted]
    for kind in wanted:
        set_settings(page, kind, "")
    if not doomed:
        return 0
    new_ops = remove_sections(ops, doomed)
    doc = page._doc.fz
    fz = page.fz
    drawn = {name for s in doomed for name in section_xobjects(ops, s)}
    still = {str(op.operands[0]) for op in new_ops if op.operator == "Do" and op.operands}
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, write(new_ops))
    doc.xref_set_key(fz.xref, "Contents", f"{xref} 0 R")
    _drop_unused_images(page, drawn - still)
    page._doc.mark_page_changed(page.index)
    return len(doomed)


def _drop_unused_images(page: MuPage, names: set[str]) -> None:
    """Forget the pictures (and Acrobat's mark forms) only the removed marks drew, so that
    updating an image watermark doesn't keep every old picture in the file. Skipped when other
    pages share the resource dictionary: they may still draw them."""
    if not names:
        return
    doc = page._doc.fz
    scope = _resources(doc, page.fz.xref, page=True)
    if scope is None or any(
        i != page.index and _resources(doc, doc.page_xref(i), page=True) == scope
        for i in range(doc.page_count)
    ):
        return
    unused = set()
    for name, xref in _category(doc, scope, "XObject").items():
        if name not in names:
            continue
        subtype = doc.xref_get_key(xref, "Subtype")[1]
        acrobat = doc.xref_get_key(xref, "PieceInfo/ADBE_CompoundType")[0] != "null"
        if subtype == "/Image" or (subtype == "/Form" and acrobat):
            unused.add(name)
    try:
        _drop_entries(doc, scope, "XObject", unused)
    except Exception:  # an unusual resource layout (MuPDF won't set keys through indirects)
        log.debug("page %d: unused mark resources left in place", page.index, exc_info=True)
