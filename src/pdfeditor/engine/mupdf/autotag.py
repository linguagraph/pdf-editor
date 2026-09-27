"""Experimental auto-tagging of untagged documents for the MuPDF backend.

Every page's content stream is rewritten so each paragraph's text and each image sits in a
marked-content sequence with an MCID; a structure tree (Document -> H1..H3 / P / Figure) then
points at those sequences, and a ParentTree maps them back. Paragraphs are MuPDF's text
blocks; headings are blocks set noticeably larger than the body text (the same rule the
reflowing exports use). Vector art and form XObjects without text become artifacts.

Marked content doesn't change what is drawn or extracted, so rendering and text are unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pdfeditor.engine.base import EngineError
from pdfeditor.engine.contentstream.marking import (
    Mark,
    has_marked_content,
    insert_marks,
    strip_mcids,
    text_runs,
    text_show_points,
)
from pdfeditor.engine.contentstream.objects import ObjectKind, find_objects
from pdfeditor.engine.contentstream.parser import Operation
from pdfeditor.engine.mupdf import content
from pdfeditor.engine.textlayout import body_size, heading_levels
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.text import Block

if TYPE_CHECKING:
    from pdfeditor.engine.mupdf.document import MuDocument
    from pdfeditor.engine.mupdf.page import MuPage

BLOCK_TOLERANCE = 2.0  # points: a text show this close to a block belongs to it
ARTIFACT = "Artifact"


@dataclass
class _Element:
    tag: str
    page: int
    top: float  # visible-space y, for slotting figures between paragraphs
    mcids: list[int] = field(default_factory=list)
    xref: int = 0


@dataclass
class _PagePlan:
    index: int
    ops: list[Operation]
    elements: list[_Element]  # reading order
    mcid_owner: list[_Element] = field(default_factory=list)  # index = MCID


def _distance(r: Rect, p: Point) -> float:
    dx = max(r.x0 - p.x, 0.0, p.x - r.x1)
    dy = max(r.y0 - p.y, 0.0, p.y - r.y1)
    return math.hypot(dx, dy)


def _owner(blocks: list[Block], p: Point) -> int | None:
    """The block a text show at ``p`` (visible space) belongs to; the nearest one if none
    contains it (whitespace and clipped text aren't part of any extracted block)."""
    if not blocks:
        return None
    for n, b in enumerate(blocks):
        if b.bbox.inflated(BLOCK_TOLERANCE).contains(p):
            return n
    return min(range(len(blocks)), key=lambda n: _distance(blocks[n].bbox, p))


def _block_size(block: Block) -> float:
    sizes = [s.size for ln in block.lines for s in ln.spans if s.text.strip()]
    return round(max(sizes) * 2) / 2 if sizes else 0.0


def _text_blocks(page: MuPage) -> list[Block]:
    return [b for b in page.text_page(with_chars=False).blocks if not b.is_image and b.text.strip()]


def _plan_page(
    page: MuPage, ops: list[Operation], blocks: list[Block], levels: dict[float, int]
) -> _PagePlan:
    to_visible = page.pdf_matrix.inverted()
    elements = [
        _Element(
            f"H{levels[_block_size(b)]}" if _block_size(b) in levels else "P", page.index, b.bbox.y0
        )
        for b in blocks
    ]
    owner = {
        i: n
        for i, p in text_show_points(ops).items()
        if (n := _owner(blocks, p.transform(to_visible))) is not None
    }
    # (start, end, element or None for an artifact)
    ranges: list[tuple[int, int, _Element | None]] = [
        (start, end, elements[n]) for start, end, n in text_runs(ops, owner)
    ]
    in_text: set[int] = set()
    for start, end, _e in ranges:
        in_text.update(range(start, end + 1))
    figures: list[_Element] = []
    for obj in find_objects(ops, content._resolver(page)):
        if obj.start in in_text or obj.end in in_text:
            continue
        if has_marked_content(ops, obj.start, obj.end):
            continue
        box = obj.bbox.transform(to_visible)
        if obj.kind in (ObjectKind.IMAGE, ObjectKind.INLINE_IMAGE):
            fig = _Element("Figure", page.index, box.y0)
            figures.append(fig)
            ranges.append((obj.start, obj.end, fig))
        elif obj.kind is ObjectKind.FORM:
            inside = [n for n, b in enumerate(blocks) if box.inflated(1).contains(b.bbox.center)]
            ranges.append((obj.start, obj.end, elements[inside[0]] if inside else None))
        else:
            ranges.append((obj.start, obj.end, None))
    ranges.sort(key=lambda r: r[0])
    plan = _PagePlan(page.index, [], [])
    marks: list[tuple[int, int, Mark]] = []
    for start, end, elem in ranges:
        if elem is None:
            marks.append((start, end, Mark(ARTIFACT)))
            continue
        mcid = len(plan.mcid_owner)
        plan.mcid_owner.append(elem)
        elem.mcids.append(mcid)
        marks.append((start, end, Mark(elem.tag, mcid)))
    plan.ops = insert_marks(ops, marks)
    # reading order: the engine's block order, with figures slotted in by position
    figures.sort(key=lambda f: f.top)
    ordered: list[_Element] = []
    for elem in elements:
        while figures and figures[0].top <= elem.top:
            ordered.append(figures.pop(0))
        ordered.append(elem)
    ordered += figures
    plan.elements = [e for e in ordered if e.mcids]
    return plan


def _kids(mcids: list[int]) -> str:
    return str(mcids[0]) if len(mcids) == 1 else "[" + " ".join(map(str, mcids)) + "]"


def auto_tag(doc: MuDocument) -> dict[str, int]:
    """Tag an untagged document; returns how many elements of each type were created."""
    if doc.info().is_tagged:
        raise EngineError(
            "The document is already tagged. Auto-tagging only works on untagged documents; "
            "edit the existing tags in the Tags panel instead."
        )
    fz = doc.fz
    # read and check every page before changing anything
    pages = [doc.page(i) for i in range(doc.page_count)]
    parsed = [strip_mcids(content._ops(p)) for p in pages]
    page_blocks = [_text_blocks(p) for p in pages]
    all_blocks = [b for blocks in page_blocks for b in blocks]
    levels = heading_levels(all_blocks, body_size(all_blocks))
    plans = [
        _plan_page(p, ops, blocks, levels)
        for p, ops, blocks in zip(pages, parsed, page_blocks, strict=True)
    ]
    elements = [e for plan in plans for e in plan.elements]
    if not elements:
        raise EngineError("There's no text or image on any page to tag.")

    root = fz.get_new_xref()
    fz.update_object(root, "<<>>")
    document = fz.get_new_xref()
    fz.update_object(document, "<<>>")
    parent_tree = fz.get_new_xref()
    for e in elements:
        e.xref = fz.get_new_xref()
        fz.update_object(
            e.xref,
            f"<</Type/StructElem/S/{e.tag}/P {document} 0 R"
            f"/Pg {pages[e.page].fz.xref} 0 R/K {_kids(e.mcids)}>>",
        )
    nums: list[str] = []
    key = 0
    for plan, page in zip(plans, pages, strict=True):
        content._write_ops(page, plan.ops)
        if not plan.mcid_owner:
            continue
        fz.xref_set_key(page.fz.xref, "StructParents", str(key))
        refs = " ".join(f"{e.xref} 0 R" for e in plan.mcid_owner)
        nums.append(f"{key} [{refs}]")
        key += 1
    fz.update_object(parent_tree, "<</Nums [" + " ".join(nums) + "]>>")
    kids = " ".join(f"{e.xref} 0 R" for e in elements)
    fz.update_object(document, f"<</Type/StructElem/S/Document/P {root} 0 R/K [{kids}]>>")
    fz.update_object(
        root,
        f"<</Type/StructTreeRoot/K {document} 0 R/ParentTree {parent_tree} 0 R"
        f"/ParentTreeNextKey {key}>>",
    )
    catalog = fz.pdf_catalog()
    fz.xref_set_key(catalog, "StructTreeRoot", f"{root} 0 R")
    fz.xref_set_key(catalog, "MarkInfo", "<</Marked true>>")
    counts: dict[str, int] = {}
    for e in elements:
        counts[e.tag] = counts.get(e.tag, 0) + 1
    return counts
