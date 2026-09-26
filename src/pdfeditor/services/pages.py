"""Page-range parsing shared by print, export, split and extract dialogs."""

from __future__ import annotations


def parse_page_ranges(spec: str, page_count: int) -> list[int]:
    """Parse ``"1-3, 5, 8-"`` (1-based, inclusive) into sorted unique 0-based indices.

    ``"-4"`` means pages 1 to 4, ``"8-"`` means page 8 to the end, and ``"all"`` or an empty
    string means every page. Raises ValueError for malformed or out-of-range input.
    """
    text = spec.strip().lower()
    if text in ("", "all"):
        return list(range(page_count))
    pages: set[int] = set()
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            lo = int(lo_s) if lo_s.strip() else 1
            hi = int(hi_s) if hi_s.strip() else page_count
        else:
            lo = hi = int(part)
        if lo < 1 or hi > page_count or lo > hi:
            raise ValueError(f"page range {part!r} is outside 1-{page_count}")
        pages.update(range(lo - 1, hi))
    if not pages:
        raise ValueError("no pages selected")
    return sorted(pages)


def format_page_ranges(indices: list[int]) -> str:
    """Inverse of :func:`parse_page_ranges` for display: ``[0,1,2,4]`` -> ``"1-3, 5"``."""
    out: list[str] = []
    run: list[int] = []
    for i in sorted(set(indices)):
        if run and i == run[-1] + 1:
            run.append(i)
            continue
        if run:
            out.append(_fmt(run))
        run = [i]
    if run:
        out.append(_fmt(run))
    return ", ".join(out)


def _fmt(run: list[int]) -> str:
    return str(run[0] + 1) if len(run) == 1 else f"{run[0] + 1}-{run[-1] + 1}"
