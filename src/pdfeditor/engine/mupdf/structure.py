"""Structure tree (tags) and accessibility settings for the MuPDF backend.

MuPDF exposes objects as PDF text; ``engine.pdfobject`` parses it, and writes go back through
``xref_set_key`` with values rendered as PDF syntax.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pdfeditor.engine.base import EngineError
from pdfeditor.engine.pdfobject import (
    Name,
    PdfSyntaxError,
    Ref,
    parse,
    text_string,
    write_name,
    write_text_string,
)
from pdfeditor.model.structure import AccessibilitySettings, StructNode

if TYPE_CHECKING:
    import pymupdf

MAX_DEPTH = 64  # malformed files can nest (or loop) deeply


def _get(fz: pymupdf.Document, xref: int, key: str) -> Any:
    kind, value = fz.xref_get_key(xref, key)
    if kind == "null":
        return None
    if kind == "string":
        return value  # MuPDF already decoded the text string
    try:
        return parse(value)
    except PdfSyntaxError:
        return None


def _direct(fz: pymupdf.Document, xref: int, key: str) -> Any:
    """Like ``_get``, but follows an indirect reference to a leaf value.

    Real files store even simple values indirectly (IRS forms: ``/Lang 1029 0 R``,
    ``/DisplayDocTitle 1059 0 R``); MuPDF reports those as "xref" rather than the value.
    """
    value = _get(fz, xref, key)
    if isinstance(value, Ref):
        try:
            value = parse(fz.xref_object(value.num, compressed=True))
        except (PdfSyntaxError, RuntimeError, ValueError):
            return None
    return value


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return text_string(value)
    return value if isinstance(value, str) and not isinstance(value, Name) else ""


def _root_xref(fz: pymupdf.Document) -> int | None:
    ref = _get(fz, fz.pdf_catalog(), "StructTreeRoot")
    return ref.num if isinstance(ref, Ref) else None


def _role_map(fz: pymupdf.Document, root: int) -> dict[str, str]:
    rm = _get(fz, root, "RoleMap")
    if isinstance(rm, Ref):
        rm = parse(fz.xref_object(rm.num, compressed=True))
    if not isinstance(rm, dict):
        return {}
    return {k: str(v) for k, v in rm.items() if isinstance(v, Name)}


def _resolve_role(t: str, role_map: dict[str, str]) -> str:
    seen = set()
    while t in role_map and t not in seen:
        seen.add(t)
        t = role_map[t]
    return t


def _kids(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def structure_tree(fz: pymupdf.Document) -> list[StructNode]:
    root = _root_xref(fz)
    if root is None:
        return []
    page_of = {fz[i].xref: i for i in range(fz.page_count)}
    roles = _role_map(fz, root)
    visited: set[int] = set()

    def build(xref: int, depth: int) -> StructNode | None:
        if xref in visited or depth > MAX_DEPTH:
            return None
        visited.add(xref)
        s = _get(fz, xref, "S")
        if not isinstance(s, Name):
            return None
        pg = _get(fz, xref, "Pg")
        node = StructNode(
            ref=xref,
            type=str(s),
            role=_resolve_role(str(s), roles) if str(s) in roles else "",
            title=_text(_direct(fz, xref, "T")),
            alt=_text(_direct(fz, xref, "Alt")),
            actual_text=_text(_direct(fz, xref, "ActualText")),
            lang=_text(_direct(fz, xref, "Lang")),
            page_index=page_of.get(pg.num) if isinstance(pg, Ref) else None,
        )
        for kid in _kids(_get(fz, xref, "K")):
            if isinstance(kid, Ref):
                child = build(kid.num, depth + 1)
                if child is not None:
                    node.children.append(child)
            elif isinstance(kid, dict) and node.page_index is None:
                kpg = kid.get("Pg")  # marked-content or object reference on another page
                if isinstance(kpg, Ref):
                    node.page_index = page_of.get(kpg.num)
        if node.page_index is None:
            node.page_index = next(
                (c.page_index for c in node.children if c.page_index is not None), None
            )
        return node

    roots = []
    for kid in _kids(_get(fz, root, "K")):
        if isinstance(kid, Ref):
            n = build(kid.num, 0)
            if n is not None:
                roots.append(n)
    return roots


def _is_struct_elem(fz: pymupdf.Document, xref: int) -> bool:
    return isinstance(_get(fz, xref, "S"), Name)


def set_struct_element(
    fz: pymupdf.Document, ref: int, type: str | None = None, alt: str | None = None
) -> None:
    if not _is_struct_elem(fz, ref):
        raise EngineError(f"object {ref} is not a structure element")
    if type is not None:
        if not type.strip():
            raise EngineError("a tag needs a type")
        fz.xref_set_key(ref, "S", write_name(type.strip()))
    if alt is not None:
        fz.xref_set_key(ref, "Alt", write_text_string(alt) if alt else "null")


def reorder_children(fz: pymupdf.Document, parent: int | None, order: Sequence[int]) -> None:
    """Reorder the element children of ``parent`` (None: the tree root) to ``order`` (their
    refs); marked-content kids keep their relative places after the elements."""
    holder = _root_xref(fz) if parent is None else parent
    if holder is None:
        raise EngineError("the document has no structure tree")
    kids = _kids(_get(fz, holder, "K"))
    elems = [k.num for k in kids if isinstance(k, Ref)]
    if sorted(elems) != sorted(order):
        raise EngineError("the new order must list the same children")
    others = [k for k in kids if not isinstance(k, Ref)]
    parts = [f"{n} 0 R" for n in order] + [_write_value(k) for k in others]
    fz.xref_set_key(holder, "K", "[" + " ".join(parts) + "]")


def _write_value(v: Any) -> str:
    if isinstance(v, Ref):
        return f"{v.num} {v.gen} R"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, Name):
        return write_name(str(v))
    if isinstance(v, bytes):
        return "<" + v.hex() + ">"
    if isinstance(v, str):
        return write_text_string(v)
    if isinstance(v, list):
        return "[" + " ".join(_write_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "<<" + " ".join(f"{write_name(k)} {_write_value(x)}" for k, x in v.items()) + ">>"
    return "null"


def accessibility_settings(fz: pymupdf.Document) -> AccessibilitySettings:
    catalog = fz.pdf_catalog()
    lang = _text(_direct(fz, catalog, "Lang"))
    prefs = _get(fz, catalog, "ViewerPreferences")
    display = False
    if isinstance(prefs, Ref):
        display = _direct(fz, prefs.num, "DisplayDocTitle") is True
    elif isinstance(prefs, dict):
        flag = prefs.get("DisplayDocTitle")
        if isinstance(flag, Ref):
            flag = parse(fz.xref_object(flag.num, compressed=True))
        display = flag is True
    with_annots = [p for p in fz if p.annots() is not None and next(p.annots(), None) is not None]
    tabs = all(_direct(fz, p.xref, "Tabs") == "S" for p in with_annots)
    return AccessibilitySettings(lang, display, tabs)


def set_accessibility_settings(fz: pymupdf.Document, s: AccessibilitySettings) -> None:
    catalog = fz.pdf_catalog()
    fz.xref_set_key(catalog, "Lang", write_text_string(s.language) if s.language else "null")
    kind, value = fz.xref_get_key(catalog, "ViewerPreferences")
    if kind == "xref":  # an indirect dictionary: set the key on it
        fz.xref_set_key(int(value.split()[0]), "DisplayDocTitle", _write_value(s.display_doc_title))
    else:
        fz.xref_set_key(
            catalog, "ViewerPreferences/DisplayDocTitle", _write_value(s.display_doc_title)
        )
    for page in fz:
        if s.tab_order_structure:
            fz.xref_set_key(page.xref, "Tabs", "/S")
        elif _get(fz, page.xref, "Tabs") == "S":
            fz.xref_set_key(page.xref, "Tabs", "null")
