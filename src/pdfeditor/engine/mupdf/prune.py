"""Drop references to deleted pages for the MuPDF backend.

MuPDF's page selection filters the outline, named destinations and links, but the catalog
entries we put back after it (see ``pagecopy.select_with_copies``) still name the old pages:
an open action, PDF 1.1 ``/Dests``, the destinations in a restored ``/Names`` tree, and form
fields whose widgets sat on a deleted page. Each would keep the deleted page object alive in
the saved file and send a reader to a page that no longer exists, so they are removed.

Values are read and written as PDF source text, like the rest of the backend; ``items`` splits
an array or dictionary into its top-level values.
"""

from __future__ import annotations

import re

import pymupdf

_REF = re.compile(r"(\d+)\s+(\d+)\s+R")
_ATOM = re.compile(r"\d+\s+\d+\s+R\b|/[^\s/\[\]()<>{}%]*|[^\s/\[\]()<>{}%]+")
_GOTO = re.compile(r"/S\s*/GoTo(?![A-Za-z0-9])")
_MAX_DEPTH = 32  # name trees and field hierarchies are shallow; guards against cycles


def prune_references(fz: pymupdf.Document, removed: set[int]) -> None:
    """Remove references to the page objects ``removed`` (already out of the page tree)."""
    catalog = fz.pdf_catalog()
    _prune_open_action(fz, catalog, removed)
    _prune_old_dests(fz, catalog, removed)
    kind, value = fz.xref_get_key(catalog, "Names")
    owner, prefix = (_ref(value), "") if kind == "xref" else (catalog, "Names/")
    if kind in ("xref", "dict"):
        kind, value = fz.xref_get_key(owner, prefix + "Dests")
        if kind == "xref":
            _prune_name_tree(fz, _ref(value), "", removed, set(), 0)
        elif kind == "dict":
            _prune_name_tree(fz, owner, prefix + "Dests/", removed, set(), 0)
    _prune_fields(fz, catalog, removed)


# -- parsing ----------------------------------------------------------------------------------
def items(text: str) -> list[str]:
    """Top-level values of a PDF array (``[...]``) or dictionary (``<<...>>``, keys and values
    alternating) as source text; anything else gives an empty list."""
    text = text.strip()
    if text.startswith("<<") and text.endswith(">>"):
        body = text[2:-2]
    elif text.startswith("[") and text.endswith("]"):
        body = text[1:-1]
    else:
        return []
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i].isspace():
            i += 1
            continue
        end = _skip(body, i)
        out.append(body[i:end])
        i = end
    return out


def _skip(text: str, i: int) -> int:
    """Index just past the value starting at ``text[i]``."""
    if text.startswith("<<", i) or text[i] == "[":
        closing = ">>" if text[i] == "<" else "]"
        i += len(closing)  # the opening "<<" or "[" is as long as its closing
        while i < len(text) and not text.startswith(closing, i):
            i = i + 1 if text[i].isspace() else _skip(text, i)
        return min(len(text), i + len(closing))
    if text[i] == "(":
        depth = 0
        while i < len(text):
            c = text[i]
            if c == "\\":
                i += 2
                continue
            depth += c == "("
            depth -= c == ")"
            i += 1
            if depth == 0:
                return i
        return i
    if text[i] == "<":
        end = text.find(">", i)
        return len(text) if end < 0 else end + 1
    match = _ATOM.match(text, i)
    return match.end() if match and match.end() > i else i + 1


def _ref(text: str) -> int:
    return int(text.split()[0])


def as_ref(text: str) -> int | None:
    match = _REF.fullmatch(text.strip())
    return int(match.group(1)) if match else None


def _resolve(fz: pymupdf.Document, text: str) -> str:
    ref = as_ref(text)
    return text if ref is None else str(fz.xref_object(ref, compressed=True))


def _dict_value(text: str, key: str) -> str | None:
    values = items(text)
    for k, v in zip(values[::2], values[1::2], strict=False):
        if k == key:
            return v
    return None


def dest_page(fz: pymupdf.Document, value: str, action: bool = False) -> int | None:
    """The page object a destination (or, with ``action``, a GoTo action) points at."""
    text = _resolve(fz, value).strip()
    if text.startswith("<<"):
        if action and not _GOTO.search(text):
            return None  # a URI, launch, script ... action: no page
        d = _dict_value(text, "/D")
        if d is None:
            return None
        text = _resolve(fz, d).strip()
    values = items(text) if text.startswith("[") else []
    return as_ref(values[0]) if values else None


# -- catalog entries --------------------------------------------------------------------------
def _prune_open_action(fz: pymupdf.Document, catalog: int, removed: set[int]) -> None:
    kind, value = fz.xref_get_key(catalog, "OpenAction")
    if kind != "null" and dest_page(fz, value, action=True) in removed:
        fz.xref_set_key(catalog, "OpenAction", "null")


def _prune_old_dests(fz: pymupdf.Document, catalog: int, removed: set[int]) -> None:
    """PDF 1.1 named destinations: a dictionary in the catalog."""
    kind, value = fz.xref_get_key(catalog, "Dests")
    if kind == "xref":
        owner, prefix, text = _ref(value), "", fz.xref_object(_ref(value), compressed=True)
    elif kind == "dict":
        owner, prefix, text = catalog, "Dests/", value
    else:
        return
    values = items(text)
    for key, dest in zip(values[::2], values[1::2], strict=False):
        if key.startswith("/") and dest_page(fz, dest) in removed:
            fz.xref_set_key(owner, prefix + key[1:], "null")


def _prune_name_tree(
    fz: pymupdf.Document, owner: int, prefix: str, removed: set[int], seen: set[int], depth: int
) -> None:
    if depth > _MAX_DEPTH or (not prefix and owner in seen):
        return
    seen.add(owner)
    kind, value = fz.xref_get_key(owner, prefix + "Kids")
    if kind == "xref":
        value = fz.xref_object(_ref(value), compressed=True)
    if kind in ("xref", "array"):
        for kid in _REF.findall(value):
            _prune_name_tree(fz, int(kid[0]), "", removed, seen, depth + 1)
    kind, value = fz.xref_get_key(owner, prefix + "Names")
    if kind == "xref":
        holder = _ref(value)
        value = fz.xref_object(holder, compressed=True)
    elif kind != "array":
        return
    values = items(value)
    pairs = list(zip(values[::2], values[1::2], strict=False))
    kept = [(k, v) for k, v in pairs if dest_page(fz, v) not in removed]
    if len(kept) == len(pairs):
        return
    text = "[" + " ".join(f"{k} {v}" for k, v in kept) + "]"
    if kind == "xref":
        fz.update_object(holder, text)
    else:
        fz.xref_set_key(owner, prefix + "Names", text)
    if kept and fz.xref_get_key(owner, prefix + "Limits")[0] != "null":
        fz.xref_set_key(owner, prefix + "Limits", f"[{kept[0][0]} {kept[-1][0]}]")


# -- form fields ------------------------------------------------------------------------------
def _prune_fields(fz: pymupdf.Document, catalog: int, removed: set[int]) -> None:
    """Drop widgets that were on deleted pages, and fields left without widgets."""
    kind, value = fz.xref_get_key(catalog, "AcroForm")
    if kind == "xref":
        owner, prefix = _ref(value), ""
    elif kind == "dict":
        owner, prefix = catalog, "AcroForm/"
    else:
        return
    dropped: set[int] = set()
    if _prune_kids(fz, owner, prefix + "Fields", removed, dropped, 0) is None:
        return
    if dropped:  # the calculation order lists fields too
        _remove_refs(fz, owner, prefix + "CO", dropped)


def _prune_kids(
    fz: pymupdf.Document, owner: int, key: str, removed: set[int], dropped: set[int], depth: int
) -> int | None:
    """Prune the array ``owner``/``key`` of fields or widgets; returns how many remain (None
    when there is no such array)."""
    kind, value = fz.xref_get_key(owner, key)
    if kind == "xref":
        value = fz.xref_object(_ref(value), compressed=True)
    elif kind != "array":
        return None
    if depth > _MAX_DEPTH:
        return len(_REF.findall(value))
    gone: set[int] = set()
    for match in _REF.findall(value):
        field = int(match[0])
        page = fz.xref_get_key(field, "P")
        if page[0] == "xref" and _ref(page[1]) in removed:
            gone.add(field)  # a widget on a deleted page
        elif _prune_kids(fz, field, "Kids", removed, dropped, depth + 1) == 0:
            gone.add(field)  # a field whose widgets were all on deleted pages
    if gone:
        dropped |= gone
        _remove_refs(fz, owner, key, gone)
    return len(_REF.findall(value)) - len(gone)


def _remove_refs(fz: pymupdf.Document, owner: int, key: str, refs: set[int]) -> None:
    kind, value = fz.xref_get_key(owner, key)
    holder = _ref(value) if kind == "xref" else None
    if holder is not None:
        value = fz.xref_object(holder, compressed=True)
    elif kind != "array":
        return
    kept = [m.group(0) for m in _REF.finditer(value) if int(m.group(1)) not in refs]
    text = "[" + " ".join(kept) + "]"
    if holder is not None:
        fz.update_object(holder, text)
    else:
        fz.xref_set_key(owner, key, text)
