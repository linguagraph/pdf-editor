"""Inline the Form XObject that ``Page.insert_htmlbox`` draws through.

MuPDF's HTML text layout always renders into a self-contained Form XObject and appends one
``/Name Do`` to the page (typically nested: an outer form invoking an inner ``fullpage`` form
holding the real ``Tf``/``Tj`` operators). Left as is, that ``Do`` shows up in our own
content-object listing as a separate, contentless "form" object sitting right on top of the
text we just placed — an empty box in the Edit tool, and one more orphaned object on every edit.

This module rewrites the newly-appended content stream(s) so the form's operators are inlined
directly (with its ``Matrix``/``BBox`` applied explicitly, since a bare ``Do`` no longer does
that for us), and its ``Font``/``XObject`` resources are merged into the page's own resources.
The now-unreferenced Form objects are left for the next ``garbage`` collection on save.

Best-effort: if a page's resource structure doesn't fit the simple shapes handled here, we log
and leave the Form XObject in place rather than risk corrupting the content stream.
"""

from __future__ import annotations

import logging
import re

import pymupdf

from pdfeditor.engine.contentstream.parser import Name, Operation, parse, write

log = logging.getLogger(__name__)

_REF = re.compile(r"/(\S+)\s+(\d+)\s+0\s+R")


def flatten_inserted_forms(page_fz: pymupdf.Page, before_contents: set[int]) -> None:
    """Inline every content stream appended since ``before_contents`` was captured."""
    doc = page_fz.parent
    new_xrefs = [x for x in page_fz.get_contents() if x not in before_contents]
    if not new_xrefs:
        return
    try:
        holder, prefix = _resource_scope(doc, page_fz.xref)
        for xref in new_xrefs:
            raw = doc.xref_stream(xref) or b""
            if not raw.strip():
                continue
            ops = parse(raw)
            cache: dict[tuple[str, int], str] = {}
            flat = _inline(doc, ops, page_fz.xref, holder, prefix, cache, frozenset())
            doc.update_stream(xref, write(flat))
    except Exception:
        log.debug(
            "could not inline an inserted text form; leaving it as a Form XObject", exc_info=True
        )


# -- resource-dict plumbing ----------------------------------------------------------------------
def _resource_scope(doc: pymupdf.Document, holder_xref: int) -> tuple[int, str]:
    """Where to add ``Font``/``XObject`` entries for ``holder_xref``: the xref to set keys on,
    and the path prefix to use there (empty once ``Resources`` itself is a real object)."""
    kind, val = doc.xref_get_key(holder_xref, "Resources")
    if kind == "xref":
        return int(val.split()[0]), ""
    return holder_xref, "Resources/"


def _category_dict(doc: pymupdf.Document, holder: int, prefix: str, category: str) -> str:
    kind, val = doc.xref_get_key(holder, f"{prefix}{category}")
    return val if kind == "dict" else ""


def _lookup_ref(dict_text: str, name: str) -> int | None:
    m = re.search(rf"/{re.escape(name)}\s+(\d+)\s+0\s+R", dict_text)
    return int(m.group(1)) if m else None


def _existing_name(dict_text: str, target_xref: int) -> str | None:
    for name, xref in _REF.findall(dict_text):
        if int(xref) == target_xref:
            return name
    return None


def _fresh_name(dict_text: str, base: str) -> str:
    used = {name for name, _xref in _REF.findall(dict_text)}
    n = 0
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def _merge_resource(
    doc: pymupdf.Document,
    holder: int,
    prefix: str,
    category: str,
    target_xref: int,
    cache: dict[tuple[str, int], str],
) -> str:
    """Add ``target_xref`` under ``category`` (Font/XObject) if it isn't there yet; returns its
    (possibly renamed, possibly already-existing) name in that scope."""
    key = (category, target_xref)
    if key in cache:
        return cache[key]
    dict_text = _category_dict(doc, holder, prefix, category)
    name = _existing_name(dict_text, target_xref)
    if name is None:
        name = _fresh_name(dict_text, "EF" if category == "Font" else "EX")
        doc.xref_set_key(holder, f"{prefix}{category}/{name}", f"{target_xref} 0 R")
    cache[key] = name
    return name


def _num_array(doc: pymupdf.Document, xref: int, key: str) -> list[float] | None:
    kind, val = doc.xref_get_key(xref, key)
    if kind != "array":
        return None
    return [float(v) for v in val.strip("[]").split()]


# -- inlining -------------------------------------------------------------------------------------
def _inline(
    doc: pymupdf.Document,
    ops: list[Operation],
    scope_xref: int,
    holder: int,
    prefix: str,
    cache: dict[tuple[str, int], str],
    seen: frozenset[int],
) -> list[Operation]:
    out: list[Operation] = []
    for op in ops:
        if op.operator == "Do" and op.operands and isinstance(op.operands[0], Name):
            replaced = _inline_do(doc, str(op.operands[0]), scope_xref, holder, prefix, cache, seen)
            if replaced is not None:
                out.extend(replaced)
                continue
        elif op.operator == "Tf" and len(op.operands) == 2 and isinstance(op.operands[0], Name):
            font_xref = _lookup_ref(
                _category_dict(doc, scope_xref, "Resources/", "Font"), str(op.operands[0])
            )
            if font_xref is not None:
                new_name = _merge_resource(doc, holder, prefix, "Font", font_xref, cache)
                out.append(Operation("Tf", [Name(new_name), op.operands[1]]))
                continue
        out.append(op)
    return out


def _inline_do(
    doc: pymupdf.Document,
    name: str,
    scope_xref: int,
    holder: int,
    prefix: str,
    cache: dict[tuple[str, int], str],
    seen: frozenset[int],
) -> list[Operation] | None:
    xobjects = _category_dict(doc, scope_xref, "Resources/", "XObject")
    target = _lookup_ref(xobjects, name)
    if target is None or target in seen:
        return None
    subtype = doc.xref_get_key(target, "Subtype")[1].lstrip("/")
    if subtype == "Image":
        new_name = _merge_resource(doc, holder, prefix, "XObject", target, cache)
        return [Operation("Do", [Name(new_name)])]
    if subtype != "Form":
        return None
    bbox = _num_array(doc, target, "BBox")
    matrix = _num_array(doc, target, "Matrix") or [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    raw = doc.xref_stream(target) or b""
    inner = parse(raw) if raw.strip() else []
    flat_inner = _inline(doc, inner, target, holder, prefix, cache, seen | {target})
    wrapped = [Operation("q"), Operation("cm", list(matrix))]
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        wrapped += [
            Operation("re", [x0, y0, x1 - x0, y1 - y0]),
            Operation("W"),
            Operation("n"),
        ]
    wrapped += flat_inner
    wrapped.append(Operation("Q"))
    return wrapped
