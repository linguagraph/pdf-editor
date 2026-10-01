"""Independent page copies for the MuPDF backend.

``Document.select`` with a repeated index puts the *same* page object in the page tree twice, so
an edit to one "copy" (a stamp, a content edit, a redaction, an annotation) shows on both. Each
repeat is therefore replaced by a fresh copy of the page before selecting.

A copy gets its own page dictionary, content streams, resource dictionaries, annotations and
appearance streams, i.e. everything an edit may write to. The objects those point at (fonts,
images, form XObjects, colour spaces) are immutable as far as editing goes and stay shared, so
duplicating an image-heavy page costs almost nothing in file size.

Objects are copied through their PDF source text (``xref_object``/``update_object``) rather
than ``xref_copy``, which round-trips strings through Python and mangles them.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

import pymupdf

_REF = re.compile(r"\b(\d+)\s+0\s+R\b")
_INHERITABLE = ("Resources", "MediaBox", "CropBox", "Rotate")
# resource categories an edit may add names to (inserting text adds a /Font entry, etc.)
_RESOURCE_CATEGORIES = (
    "ExtGState",
    "ColorSpace",
    "Pattern",
    "Shading",
    "XObject",
    "Font",
    "Properties",
)


def select_with_copies(fz: pymupdf.Document, order: Sequence[int]) -> None:
    """``Document.select`` that copies repeated pages and keeps the document catalog.

    Copies are appended first (so the indices in ``order`` stay valid) and the selection then
    drops whatever ``order`` doesn't list. The first occurrence keeps the original object, so
    bookmarks and links to the page keep pointing at it.

    MuPDF's selection rebuilds the catalog with little more than the page tree and outline: the
    form, language, XMP metadata, output intents, viewer preferences, signatures' DSS and the
    like would vanish, so entries it drops are put back.
    """
    seen: set[int] = set()
    distinct: list[int] = []
    for index in order:
        xref = fz.page_xref(index)
        if xref in seen:
            index = _append_copy(fz, index)
        else:
            seen.add(xref)
        distinct.append(index)
    catalog = fz.pdf_catalog()  # after copying: copied form fields join the form
    entries = {key: _raw_value(fz, catalog, key) for key in fz.xref_get_keys(catalog)}
    fz.select(distinct)
    catalog = fz.pdf_catalog()
    for key, value in entries.items():
        # page labels are re-applied by index by the caller
        if key != "PageLabels" and fz.xref_get_key(catalog, key)[0] == "null":
            fz.xref_set_key(catalog, key, value)


def _raw_value(fz: pymupdf.Document, xref: int, key: str) -> str:
    kind, value = fz.xref_get_key(xref, key)
    return pymupdf.get_pdf_str(value) if kind == "string" else str(value)


def _append_copy(fz: pymupdf.Document, index: int) -> int:
    src = fz.page_xref(index)
    inherited = {
        key: value
        for key in _INHERITABLE
        if fz.xref_get_key(src, key)[0] == "null"
        and (value := _inherited_value(fz, src, key)) is not None
    }
    fz.new_page(-1)  # a slot in the page tree; its dictionary is replaced below
    new_index = fz.page_count - 1
    page = fz.page_xref(new_index)
    parent = fz.xref_get_key(page, "Parent")[1]
    fz.update_object(page, fz.xref_object(src, compressed=True))
    fz.xref_set_key(page, "Parent", parent)
    for key, value in inherited.items():
        fz.xref_set_key(page, key, value)
    if fz.xref_get_key(page, "PieceInfo")[0] == "xref":  # page marks' settings are kept there
        piece = _clone_object(fz, int(fz.xref_get_key(page, "PieceInfo")[1].split()[0]))
        fz.xref_set_key(page, "PieceInfo", f"{piece} 0 R")
    # the structure tree maps the original's marked content; a second claim would be invalid
    _drop(fz, page, "StructParents")
    _copy_contents(fz, page)
    _copy_resources(fz, page)
    _copy_annotations(fz, page)
    return new_index


def _inherited_value(fz: pymupdf.Document, xref: int, key: str) -> str | None:
    seen: set[int] = set()
    kind, value = fz.xref_get_key(xref, "Parent")
    while kind == "xref":
        node = int(value.split()[0])
        if node in seen:  # a malformed, cyclic page tree
            return None
        seen.add(node)
        found = fz.xref_get_key(node, key)
        if found[0] != "null":
            return str(found[1])
        kind, value = fz.xref_get_key(node, "Parent")
    return None


def _drop(fz: pymupdf.Document, xref: int, key: str) -> None:
    if fz.xref_get_key(xref, key)[0] != "null":
        fz.xref_set_key(xref, key, "null")  # a null value is the same as no entry


def _clone_object(fz: pymupdf.Document, xref: int) -> int:
    new = int(fz.get_new_xref())
    fz.update_object(new, fz.xref_object(xref, compressed=True))
    return new


def _clone_stream(fz: pymupdf.Document, xref: int) -> int:
    if not fz.xref_is_stream(xref):
        return _clone_object(fz, xref)
    new = _clone_object(fz, xref)
    for key in ("Filter", "DecodeParms"):  # the data is stored decoded, then recompressed
        _drop(fz, new, key)
    fz.update_stream(new, fz.xref_stream(xref))
    return new


def _clone_refs(fz: pymupdf.Document, text: str) -> str:
    """``text`` (a PDF array or dictionary) with every referenced stream cloned."""
    return _REF.sub(lambda m: f"{_clone_stream(fz, int(m.group(1)))} 0 R", text)


def _copy_contents(fz: pymupdf.Document, page: int) -> None:
    kind, value = fz.xref_get_key(page, "Contents")
    if kind == "xref":
        ref = int(value.split()[0])
        if fz.xref_is_stream(ref):
            fz.xref_set_key(page, "Contents", f"{_clone_stream(fz, ref)} 0 R")
            return
        value = fz.xref_object(ref, compressed=True)  # an indirect array of streams
    elif kind != "array":
        return
    fz.xref_set_key(page, "Contents", _clone_refs(fz, value))


def _copy_resources(fz: pymupdf.Document, page: int) -> None:
    kind, value = fz.xref_get_key(page, "Resources")
    if kind == "xref":
        owner, prefix = _clone_object(fz, int(value.split()[0])), ""
        fz.xref_set_key(page, "Resources", f"{owner} 0 R")
    elif kind == "dict":
        owner, prefix = page, "Resources/"
    else:
        return
    for category in _RESOURCE_CATEGORIES:
        kind, value = fz.xref_get_key(owner, prefix + category)
        if kind == "xref":
            new = _clone_object(fz, int(value.split()[0]))
            fz.xref_set_key(owner, prefix + category, f"{new} 0 R")


def _copy_annotations(fz: pymupdf.Document, page: int) -> None:
    kind, value = fz.xref_get_key(page, "Annots")
    if kind == "xref":
        value = fz.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "array":
        return
    refs = [int(r) for r in _REF.findall(value)]
    copies = {old: _clone_object(fz, old) for old in dict.fromkeys(refs)}
    for new in copies.values():
        fz.xref_set_key(new, "P", f"{page} 0 R")
        _drop(fz, new, "StructParent")
        if fz.xref_get_key(new, "NM")[0] == "string":  # /NM identifies one annotation
            fz.xref_set_key(new, "NM", pymupdf.get_pdf_str(f"pdfeditor-{uuid.uuid4().hex}"))
        for key in ("Popup", "Parent", "IRT"):  # links among the page's own annotations
            kind, target = fz.xref_get_key(new, key)
            if kind == "xref" and (old := int(target.split()[0])) in copies:
                fz.xref_set_key(new, key, f"{copies[old]} 0 R")
        _copy_appearance(fz, new)
        if fz.xref_get_key(new, "Subtype")[1] == "/Widget":
            _register_widget(fz, new)
    fz.xref_set_key(page, "Annots", "[" + " ".join(f"{copies[r]} 0 R" for r in refs) + "]")


def _copy_appearance(fz: pymupdf.Document, annot: int) -> None:
    kind, value = fz.xref_get_key(annot, "AP")
    if kind == "xref":
        owner, prefix = _clone_object(fz, int(value.split()[0])), ""
        fz.xref_set_key(annot, "AP", f"{owner} 0 R")
    elif kind == "dict":
        owner, prefix = annot, "AP/"
    else:
        return
    for state in ("N", "R", "D"):
        kind, value = fz.xref_get_key(owner, prefix + state)
        if kind == "xref":
            ref = int(value.split()[0])
            if fz.xref_is_stream(ref):
                fz.xref_set_key(owner, prefix + state, f"{_clone_stream(fz, ref)} 0 R")
            else:  # an indirect dictionary of appearance states
                new = _clone_object(fz, ref)
                fz.update_object(new, _clone_refs(fz, fz.xref_object(new, compressed=True)))
                fz.xref_set_key(owner, prefix + state, f"{new} 0 R")
        elif kind == "dict":
            fz.xref_set_key(owner, prefix + state, _clone_refs(fz, value))


def _register_widget(fz: pymupdf.Document, widget: int) -> None:
    """Make a copied form widget part of the form: another widget of the same field when it is
    a field's kid, otherwise a new field with a distinct name (two fields can't share one)."""
    kind, name = fz.xref_get_key(widget, "T")
    if kind == "string":
        fz.xref_set_key(widget, "T", pymupdf.get_pdf_str(f"{name}#{widget}"))
    kind, parent = fz.xref_get_key(widget, "Parent")
    if kind == "xref":
        _append_ref(fz, int(parent.split()[0]), "Kids", widget)
    else:
        _append_ref(fz, fz.pdf_catalog(), "AcroForm/Fields", widget)


def _append_ref(fz: pymupdf.Document, xref: int, key: str, ref: int) -> None:
    kind, value = fz.xref_get_key(xref, key)
    if kind == "xref":  # an indirect array
        target = int(value.split()[0])
        text = fz.xref_object(target, compressed=True).strip()
        if text.startswith("[") and text.endswith("]"):
            fz.update_object(target, f"{text[:-1]} {ref} 0 R]")
    elif kind == "array":
        fz.xref_set_key(xref, key, f"{value.strip()[:-1]} {ref} 0 R]")
