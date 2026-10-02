"""Independent page copies for the MuPDF backend.

``Document.select`` with a repeated index puts the *same* page object in the page tree twice, so
an edit to one "copy" (a stamp, a content edit, a redaction, an annotation) shows on both. Each
repeat is therefore replaced by a fresh copy of the page before selecting.

A copy gets its own page dictionary, content streams, resource dictionaries, annotations and
appearance streams, i.e. everything an edit may write to. The objects those point at (fonts,
images, form XObjects, colour spaces) are immutable as far as editing goes and stay shared, so
duplicating an image-heavy page costs almost nothing in file size.

The same copying protects pages that share objects for other reasons (see "copy on write"
below): :func:`ensure_page_unshared` runs before every in-place page edit.

Objects are copied through their PDF source text (``xref_object``/``update_object``) rather
than ``xref_copy``, which round-trips strings through Python and mangles them.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

from pdfeditor.engine.mupdf.prune import as_ref, items, prune_references

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
    before = {fz.page_xref(i) for i in range(fz.page_count)}
    catalog = fz.pdf_catalog()  # after copying: copied form fields join the form
    entries = {key: _raw_value(fz, catalog, key) for key in fz.xref_get_keys(catalog)}
    names = _names_entries(fz, catalog)
    fz.select(distinct)
    catalog = fz.pdf_catalog()
    for key, value in entries.items():
        # page labels are re-applied by index by the caller
        if key != "PageLabels" and fz.xref_get_key(catalog, key)[0] == "null":
            fz.xref_set_key(catalog, key, value)
    # the selection keeps only the destinations in /Names: put the other trees (attached files,
    # document JavaScript, ...) back
    owner, prefix = _names_scope(fz, catalog)
    for key, value in names.items():
        if fz.xref_get_key(owner, prefix + key)[0] == "null":
            fz.xref_set_key(owner, prefix + key, value)
    removed = before - {fz.page_xref(i) for i in range(fz.page_count)}
    if removed:
        prune_references(fz, removed)


# /Names entries (PDF 32000-1 table 31)
_NAME_TREES = (
    "Dests",
    "AP",
    "JavaScript",
    "Pages",
    "Templates",
    "IDS",
    "URLS",
    "EmbeddedFiles",
    "AlternatePresentations",
    "Renditions",
)


def _names_scope(fz: pymupdf.Document, catalog: int) -> tuple[int, str]:
    kind, value = fz.xref_get_key(catalog, "Names")
    return (int(value.split()[0]), "") if kind == "xref" else (catalog, "Names/")


def _names_entries(fz: pymupdf.Document, catalog: int) -> dict[str, str]:
    owner, prefix = _names_scope(fz, catalog)
    return {
        key: _raw_value(fz, owner, prefix + key)
        for key in _NAME_TREES
        if fz.xref_get_key(owner, prefix + key)[0] != "null"
    }


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


# -- copy on write ------------------------------------------------------------------------------
# Pages can share what an edit writes to without being duplicates: saving with ``garbage=4``
# (Optimize) merges identical content streams and dictionaries, and other producers share
# content streams, resource dictionaries or even annotations between pages. Before a page is
# changed in place, whatever it shares of those is copied (fonts, images and form XObjects stay
# shared: edits never rewrite them).


@dataclass(frozen=True, slots=True)
class _PageRefs:
    page: int  # the page object
    # mutable page-level objects: content streams (and an indirect array of them), the resource
    # dictionary and its indirect category dictionaries, /PieceInfo, an indirect /Annots array
    objects: frozenset[int]
    annots: tuple[int, ...]
    inherits_resources: bool


@dataclass(frozen=True, slots=True)
class Unshared:
    """What :func:`ensure_page_unshared` changed."""

    pages: frozenset[int] = frozenset()  # indices of pages whose objects were replaced
    structure: bool = False  # the page tree changed (a page object listed twice was copied)


class SharingIndex:
    """Who refers to which mutable object: built once per document state, then kept current
    a page at a time, so guarding every page of a large document stays linear.

    Once a page has been made unshared it stays so until the page tree changes (:meth:`reset`):
    edits only give a page new objects of its own, so the next guard of that page is free.
    Pages an edit changed are only re-read when they are guarded next; until then their entry
    may list objects they no longer use, which can cost an unnecessary copy but never a missed
    one. The index holds object numbers only, never MuPDF objects.
    """

    def __init__(self) -> None:
        self._pages: list[_PageRefs] | None = None
        self._edited: set[int] = set()  # changed by an edit since last read
        self._copied: set[int] = set()  # changed by unsharing: re-read before the next check
        self.clean: set[int] = set()  # pages known to share nothing mutable
        self.page_users: Counter[int] = Counter()  # page object -> times in the page tree
        self.users: Counter[int] = Counter()  # object -> pages/annotations referring to it
        self.annot_pages: Counter[int] = Counter()  # annotation -> pages listing it
        self.appearances: dict[int, frozenset[int]] = {}  # annotation -> its appearance objects

    def reset(self) -> None:
        self._pages = None
        self._edited.clear()
        self._copied.clear()
        self.clean.clear()

    def dirty(self, index: int) -> None:
        """Page ``index`` was edited."""
        self._edited.add(index)

    def is_clean(self, fz: pymupdf.Document, index: int) -> bool:
        return self._pages is not None and len(self._pages) == fz.page_count and index in self.clean

    def pages(self, fz: pymupdf.Document, index: int) -> list[_PageRefs]:
        """All pages' references, current for page ``index`` and pages unsharing changed."""
        if self._pages is None or len(self._pages) != fz.page_count:
            self.reset()
            for counter in (self.page_users, self.users, self.annot_pages):
                counter.clear()
            self.appearances.clear()
            resources: dict[int, frozenset[int]] = {}
            self._pages = []
            for i in range(fz.page_count):
                refs = _page_refs(fz, i, resources)
                self._pages.append(refs)
                self._add(fz, refs, 1)
            return self._pages
        todo = self._copied | ({index} & self._edited)
        for i in sorted(todo):
            self._add(fz, self._pages[i], -1)
            self._pages[i] = _page_refs(fz, i, {}, self._pages[i].page)
            self._add(fz, self._pages[i], 1)
        self._edited -= todo
        self._copied.clear()
        return self._pages

    def copied(self, indices: set[int]) -> None:
        self._copied |= indices

    def _add(self, fz: pymupdf.Document, refs: _PageRefs, sign: int) -> None:
        self.page_users[refs.page] += sign
        for xref in refs.objects:
            self.users[xref] += sign
        for annot in refs.annots:
            self.annot_pages[annot] += sign
            # an annotation's appearance counts once however many pages list it
            if sign > 0 and self.annot_pages[annot] == 1:
                self.appearances[annot] = _appearance_refs(fz, annot)
                for xref in self.appearances[annot]:
                    self.users[xref] += 1
            elif sign < 0 and self.annot_pages[annot] == 0:
                for xref in self.appearances.pop(annot, frozenset()):
                    self.users[xref] -= 1

    def shared(self, xref: int) -> bool:
        return self.users[xref] > 1


def ensure_page_unshared(
    fz: pymupdf.Document, index: int, sharing: SharingIndex | None = None
) -> Unshared:
    """Copy what page ``index`` shares with other pages before it is changed in place.

    Its content streams, resource dictionaries, /PieceInfo and /Annots array become its own,
    and so do the appearance streams of its annotations. An annotation listed on several pages
    stays on this page (its id is what the caller knows it by) and the *other* pages get copies.
    A page object listed twice in the page tree is split by copying the repeat.
    """
    sharing = sharing or SharingIndex()
    if sharing.is_clean(fz, index):
        return Unshared()
    pages = sharing.pages(fz, index)
    refs = pages[index]
    structure = False
    if sharing.page_users[refs.page] > 1:
        labels = fz.get_page_labels()  # the selection drops them
        select_with_copies(fz, range(fz.page_count))
        if labels:
            fz.set_page_labels(labels)
        sharing.reset()
        pages = sharing.pages(fz, index)
        refs = pages[index]
        structure = True
    if not (
        refs.inherits_resources
        or any(sharing.shared(x) for x in refs.objects)
        or any(sharing.annot_pages[a] > 1 for a in refs.annots)
        or any(sharing.shared(x) for a in refs.annots for x in sharing.appearances.get(a, ()))
    ):
        sharing.clean.add(index)
        return Unshared(structure=structure)
    page = refs.page
    _unshare_contents(fz, page, sharing)
    _unshare_resources(fz, page, sharing)
    for key in ("PieceInfo", "Annots"):
        kind, value = fz.xref_get_key(page, key)
        if kind == "xref" and sharing.shared(ref := int(value.split()[0])):
            if key == "PieceInfo":
                fz.xref_set_key(page, key, f"{_clone_object(fz, ref)} 0 R")
            else:  # a direct copy of the array
                fz.xref_set_key(page, key, fz.xref_object(ref, compressed=True))
    changed = {index} | _give_others_copies(fz, index, pages, sharing)
    for annot in refs.annots:
        if any(sharing.shared(x) for x in sharing.appearances.get(annot, ())):
            _copy_appearance(fz, annot)
    sharing.copied(changed)
    sharing.clean.add(index)
    return Unshared(frozenset(changed), structure)


def _page_refs(
    fz: pymupdf.Document, index: int, resources: dict[int, frozenset[int]], page: int = 0
) -> _PageRefs:
    """What page ``index`` (object ``page``, when known) refers to. The page dictionary is
    read once as source text: this runs for every page, so MuPDF calls are kept few."""
    page = page or fz.page_xref(index)
    values = items(fz.xref_object(page, compressed=True))
    entries = dict(zip(values[::2], values[1::2], strict=False))
    objects: set[int] = set()
    value = entries.get("/Contents", "")
    if (ref := as_ref(value)) is not None:
        objects.add(ref)
        if not fz.xref_is_stream(ref):  # an indirect array of streams
            objects.update(int(r) for r in _REF.findall(fz.xref_object(ref, compressed=True)))
    elif value.startswith("["):
        objects.update(int(r) for r in _REF.findall(value))
    value = entries.get("/Resources", "null")
    if (ref := as_ref(value)) is not None:
        if ref not in resources:  # pages often share one: read it once
            text = fz.xref_object(ref, compressed=True)
            resources[ref] = frozenset({ref, *_category_refs(fz, ref, "", text)})
        objects.update(resources[ref])
    elif value.startswith("<<"):
        objects.update(_category_refs(fz, page, "Resources/", value))
    for key in ("/PieceInfo", "/Annots"):
        if (ref := as_ref(entries.get(key, ""))) is not None:
            objects.add(ref)
    value = entries.get("/Annots", "")
    if (ref := as_ref(value)) is not None:
        value = fz.xref_object(ref, compressed=True)
    annots = tuple(dict.fromkeys(int(r) for r in _REF.findall(value)))
    return _PageRefs(page, frozenset(objects), annots, entries.get("/Resources", "null") == "null")


_CATEGORY_REF = re.compile(r"/(" + "|".join(_RESOURCE_CATEGORIES) + r")\s*\d+\s+0\s+R")


def _category_refs(fz: pymupdf.Document, owner: int, prefix: str, text: str) -> set[int]:
    """Indirect category dictionaries of a resource dictionary (``text`` is its source; most
    categories are direct, so only the candidates the text shows are looked up)."""
    out: set[int] = set()
    for category in dict.fromkeys(_CATEGORY_REF.findall(text)):
        kind, value = fz.xref_get_key(owner, prefix + category)
        if kind == "xref":
            out.add(int(value.split()[0]))
    return out


def _appearance_refs(fz: pymupdf.Document, annot: int) -> frozenset[int]:
    kind, value = fz.xref_get_key(annot, "AP")
    out: set[int] = set()
    if kind == "xref":
        out.add(int(value.split()[0]))
        value = fz.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "dict":
        return frozenset()
    for match in _REF.findall(value):
        ref = int(match)
        out.add(ref)
        if not fz.xref_is_stream(ref):  # a dictionary of appearance states
            out.update(int(r) for r in _REF.findall(fz.xref_object(ref, compressed=True)))
    return frozenset(out)


def _unshare_contents(fz: pymupdf.Document, page: int, sharing: SharingIndex) -> None:
    kind, value = fz.xref_get_key(page, "Contents")
    if kind == "xref":
        ref = int(value.split()[0])
        if fz.xref_is_stream(ref):
            if sharing.shared(ref):
                fz.xref_set_key(page, "Contents", f"{_clone_stream(fz, ref)} 0 R")
            return
        array_shared = sharing.shared(ref)
        value = fz.xref_object(ref, compressed=True)
    elif kind == "array":
        array_shared = False
    else:
        return
    streams = [int(r) for r in _REF.findall(value)]
    if array_shared or any(sharing.shared(s) for s in streams):
        copies = [_clone_stream(fz, s) if sharing.shared(s) else s for s in streams]
        fz.xref_set_key(page, "Contents", "[" + " ".join(f"{s} 0 R" for s in copies) + "]")


def _unshare_resources(fz: pymupdf.Document, page: int, sharing: SharingIndex) -> None:
    kind, value = fz.xref_get_key(page, "Resources")
    copy_all = False
    if kind == "null":  # inherited from the page tree: shared by definition
        inherited = _inherited_value(fz, page, "Resources")
        if inherited is None:
            return
        fz.xref_set_key(page, "Resources", inherited)
        kind, value = fz.xref_get_key(page, "Resources")
        copy_all = True
    if kind == "xref":
        ref = int(value.split()[0])
        if copy_all or sharing.shared(ref):
            ref = _clone_object(fz, ref)
            fz.xref_set_key(page, "Resources", f"{ref} 0 R")
        owner, prefix = ref, ""
    elif kind == "dict":
        owner, prefix = page, "Resources/"
    else:
        return
    for category in _RESOURCE_CATEGORIES:
        kind, value = fz.xref_get_key(owner, prefix + category)
        if kind == "xref" and (copy_all or sharing.shared(int(value.split()[0]))):
            new = _clone_object(fz, int(value.split()[0]))
            fz.xref_set_key(owner, prefix + category, f"{new} 0 R")


def _give_others_copies(
    fz: pymupdf.Document, index: int, pages: list[_PageRefs], sharing: SharingIndex
) -> set[int]:
    """Replace this page's annotations on the other pages that list them with copies; returns
    the indices of those pages. Form widgets are left alone: a copy would be a new field."""
    shared = [
        a
        for a in pages[index].annots
        if sharing.annot_pages[a] > 1 and fz.xref_get_key(a, "Subtype")[1] != "/Widget"
    ]
    if not shared:
        return set()
    others = [i for i, refs in enumerate(pages) if i != index and set(shared) & set(refs.annots)]
    copies: dict[int, int] = {}
    for annot in shared:
        new = copies[annot] = _clone_object(fz, annot)
        owner = next(i for i in others if annot in pages[i].annots)
        fz.xref_set_key(new, "P", f"{pages[owner].page} 0 R")
        _drop(fz, new, "StructParent")
        if fz.xref_get_key(new, "NM")[0] == "string":  # /NM identifies one annotation
            fz.xref_set_key(new, "NM", pymupdf.get_pdf_str(f"pdfeditor-{uuid.uuid4().hex}"))
        _copy_appearance(fz, new)
    for new in copies.values():
        for key in ("Popup", "Parent", "IRT"):  # links among the copied annotations
            kind, target = fz.xref_get_key(new, key)
            if kind == "xref" and (old := int(target.split()[0])) in copies:
                fz.xref_set_key(new, key, f"{copies[old]} 0 R")
    done: set[int] = set()  # indirect /Annots arrays already rewritten (pages may share one)
    for i in others:
        page = pages[i].page
        kind, value = fz.xref_get_key(page, "Annots")
        holder = int(value.split()[0]) if kind == "xref" else None
        if holder is not None:
            if holder in done:
                continue
            done.add(holder)
            value = fz.xref_object(holder, compressed=True)
        text = _REF.sub(lambda m: f"{copies.get(int(m.group(1)), int(m.group(1)))} 0 R", value)
        if holder is not None:
            fz.update_object(holder, text)
        else:
            fz.xref_set_key(page, "Annots", text)
    return set(others)
