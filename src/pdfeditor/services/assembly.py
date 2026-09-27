"""Combine, split and extract documents (headless)."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import Document, Engine, PasswordCallback
from pdfeditor.model.outline import Destination, OutlineItem

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".jxr"}


@dataclass
class MergeSource:
    path: Path
    pages: list[int] | None = None  # None = all
    password: str | PasswordCallback | None = None


def _shift(items: list[OutlineItem], offset: int, keep: dict[int, int]) -> list[OutlineItem]:
    """Outline copy with destinations remapped (``keep``: old page -> new page); items whose
    page was left out are dropped, their children kept."""
    out: list[OutlineItem] = []
    for item in items:
        children = _shift(item.children, offset, keep)
        if item.dest is not None:
            if item.dest.page_index not in keep:
                out.extend(children)
                continue
            new = copy.copy(item)
            new.dest = Destination(
                keep[item.dest.page_index] + offset, item.dest.point, item.dest.zoom
            )
        else:
            new = copy.copy(item)
        new.children = children
        out.append(new)
    return out


def merge(
    engine: Engine,
    sources: Sequence[MergeSource],
    bookmarks: bool = True,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> Document:
    """A new document with the chosen pages of every source, in order.

    PDFs keep their annotations, links and (remapped) bookmarks; images become one page each.
    With ``bookmarks``, each source gets a top-level bookmark holding its own outline.
    """
    out = engine.new_document()
    outline: list[OutlineItem] = []
    try:
        for n, source in enumerate(sources):
            if token is not None:
                token.check()
            start = out.page_count
            if source.path.suffix.lower() in IMAGE_SUFFIXES:
                out.insert_image_page(start, source.path.read_bytes())
                if bookmarks:
                    outline.append(OutlineItem(source.path.stem, dest=Destination(start)))
            else:
                doc = engine.open(source.path, source.password)
                try:
                    pages = (
                        list(range(doc.page_count)) if source.pages is None else list(source.pages)
                    )
                    out.insert_pages(doc, pages, start)
                    if bookmarks:
                        keep = {p: i for i, p in reversed(list(enumerate(pages)))}
                        children = _shift(doc.outline(), start, keep)
                        outline.append(
                            OutlineItem(
                                source.path.stem, dest=Destination(start), children=children
                            )
                        )
                finally:
                    doc.close()
            progress(n + 1, len(sources))
        if bookmarks and outline:
            out.set_outline(outline)
    except Exception:
        out.close()
        raise
    return out


class SplitMode(Enum):
    EVERY_N_PAGES = "pages"
    RANGES = "ranges"
    TOP_BOOKMARKS = "bookmarks"
    MAX_SIZE = "size"


@dataclass
class SplitPlan:
    groups: list[list[int]] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # suggested file-name suffixes


def plan_split(
    doc: Document,
    mode: SplitMode,
    pages_per_file: int = 1,
    ranges: Sequence[Sequence[int]] = (),
    max_bytes: int = 0,
    size_of: Callable[[list[int]], int] | None = None,
) -> SplitPlan:
    count = doc.page_count
    plan = SplitPlan()
    if mode is SplitMode.EVERY_N_PAGES:
        n = max(1, pages_per_file)
        for start in range(0, count, n):
            plan.groups.append(list(range(start, min(count, start + n))))
    elif mode is SplitMode.RANGES:
        plan.groups = [list(r) for r in ranges if r]
    elif mode is SplitMode.TOP_BOOKMARKS:
        starts = sorted({i.dest.page_index for i in doc.outline() if i.dest is not None})
        titles = {i.dest.page_index: i.title for i in reversed(doc.outline()) if i.dest is not None}
        if not starts or starts[0] != 0:
            starts = [0, *starts]
        for a, b in zip(starts, [*starts[1:], count], strict=True):
            if b > a:
                plan.groups.append(list(range(a, b)))
                plan.names.append(titles.get(a, ""))
    elif mode is SplitMode.MAX_SIZE:
        if max_bytes <= 0 or size_of is None:
            raise ValueError("splitting by size needs a size limit")
        group: list[int] = []
        for page in range(count):
            candidate = [*group, page]
            if group and size_of(candidate) > max_bytes:
                plan.groups.append(group)
                group = [page]
            else:
                group = candidate
        if group:
            plan.groups.append(group)
    if not plan.names:
        width = max(1, int(math.log10(max(1, len(plan.groups)))) + 1)
        plan.names = [f"part{i + 1:0{width}d}" for i in range(len(plan.groups))]
    return plan


def extract(engine: Engine, doc: Document, pages: Sequence[int]) -> Document:
    """A new document with copies of ``pages`` (annotations and links included)."""
    out = engine.new_document()
    out.insert_pages(doc, list(pages), 0)
    return out


def subset_size(engine: Engine, doc: Document, pages: list[int]) -> int:
    part = extract(engine, doc, pages)
    try:
        return len(part.to_bytes())
    finally:
        part.close()


def safe_name(text: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_." else "_" for c in text).strip()
    return keep[:80] or "part"


def write_split(
    engine: Engine,
    doc: Document,
    plan: SplitPlan,
    folder: Path,
    stem: str,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for n, (group, name) in enumerate(zip(plan.groups, plan.names, strict=True)):
        if token is not None:
            token.check()
        target = folder / f"{stem}_{safe_name(name) if name else n + 1}.pdf"
        part = extract(engine, doc, group)
        try:
            written.append(part.save(target))
        finally:
            part.close()
        progress(n + 1, len(plan.groups))
    return written
