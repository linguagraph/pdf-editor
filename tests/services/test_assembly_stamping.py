from __future__ import annotations

import io
import shutil
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Rect
from pdfeditor.services.assembly import (
    MergeSource,
    SplitMode,
    extract,
    merge,
    plan_split,
    subset_size,
    write_split,
)
from pdfeditor.services.stamping import (
    HeaderFooter,
    Slot,
    Watermark,
    apply_background,
    apply_header_footer,
    apply_watermark,
    content_bounds,
    expand,
    trimmed_rect,
)

ENGINE = get_engine()


@pytest.fixture
def png(tmp_path: Path) -> Path:
    path = tmp_path / "pic.png"
    Image.new("RGB", (96, 96), (0, 128, 255)).save(path)
    return path


def test_merge_pdfs_images_and_bookmarks(fixture_pdf, png: Path) -> None:
    doc = merge(
        ENGINE,
        [
            MergeSource(fixture_pdf("outline"), pages=[4, 5]),  # chapter 3 only
            MergeSource(png),
            MergeSource(fixture_pdf("encrypted"), password="user"),
        ],
    )
    assert doc.page_count == 4
    roots = doc.outline()
    assert [r.title for r in roots] == ["outline", "pic", "encrypted"]
    assert [c.title for c in roots[0].children] == ["Chapter 3"]
    assert roots[0].children[0].dest.page_index == 0
    assert [c.title for c in roots[0].children[0].children] == ["Section 3.1", "Section 3.2"]
    assert roots[1].dest.page_index == 2 and roots[2].dest.page_index == 3
    data = doc.to_bytes()
    doc.close()
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert len(pdf.pages) == 4


def test_split_plans(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("outline"))
    assert plan_split(doc, SplitMode.EVERY_N_PAGES, pages_per_file=4).groups == [
        [0, 1, 2, 3],
        [4, 5],
    ]
    plan = plan_split(doc, SplitMode.TOP_BOOKMARKS)
    assert plan.groups == [[0, 1], [2, 3], [4, 5]] and plan.names == [
        "Chapter 1",
        "Chapter 2",
        "Chapter 3",
    ]
    assert plan_split(doc, SplitMode.RANGES, ranges=[[0], [2, 3]]).groups == [[0], [2, 3]]
    one = subset_size(ENGINE, doc, [0])
    by_size = plan_split(
        doc,
        SplitMode.MAX_SIZE,
        max_bytes=int(one * 2.2),
        size_of=lambda g: subset_size(ENGINE, doc, g),
    )
    assert all(len(g) >= 1 for g in by_size.groups) and sum(map(len, by_size.groups)) == 6
    assert len(by_size.groups) < 6
    with pytest.raises(ValueError):
        plan_split(doc, SplitMode.MAX_SIZE)
    doc.close()


def test_write_split_and_extract(fixture_pdf, tmp_path: Path) -> None:
    doc = ENGINE.open(fixture_pdf("outline"))
    written = write_split(
        ENGINE, doc, plan_split(doc, SplitMode.TOP_BOOKMARKS), tmp_path / "out", "book"
    )
    assert [p.name for p in written] == [
        "book_Chapter 1.pdf",
        "book_Chapter 2.pdf",
        "book_Chapter 3.pdf",
    ]
    with pikepdf.open(written[2]) as pdf:
        assert len(pdf.pages) == 2
    part = extract(ENGINE, doc, [5, 0])
    assert part.page_count == 2
    part.close()
    doc.close()


def test_expand_tokens() -> None:
    spec = HeaderFooter(bates_prefix="ABC", bates_start=7, bates_digits=4)
    text = expand("<<bates>> p<<page>>/<<pages>> <<label>> <<file>>", spec, 2, 9, "iii", "x.pdf", 1)
    assert text == "ABC0008 p3/9 iii x.pdf"
    assert expand("<<unknown>>", spec, 0, 1, "", "", 0) == "<<unknown>>"


def test_header_footer_positions(fixture_pdf, tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("rotated_pages"), path)
    doc = ENGINE.open(path)
    spec = HeaderFooter(
        texts={Slot.FOOTER_RIGHT: "Page <<page>> of <<pages>>", Slot.HEADER_LEFT: "CONFIDENTIAL"}
    )
    assert apply_header_footer(ENGINE, doc, spec, [0, 1], "doc.pdf") == 4
    for index in (0, 1):  # upright and rotated pages
        page = doc.page(index)
        lines = {ln.text: ln.bbox for b in page.text_page().blocks for ln in b.lines}
        footer = lines[f"Page {index + 1} of 5"]
        header = lines["CONFIDENTIAL"]
        assert footer.x1 == pytest.approx(page.rect.x1 - 36, abs=2)
        assert footer.y1 > page.rect.height - 40
        assert header.x0 == pytest.approx(36, abs=2) and header.y0 < 40
    doc.close()


def test_watermark_is_centered(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("text_multipage").read_bytes())
    apply_watermark(ENGINE, doc, Watermark(text="DRAFT", angle=45), [0])
    page = doc.page(0)
    line = next(ln for b in page.text_page().blocks for ln in b.lines if ln.text == "DRAFT")
    center = page.rect.center
    assert line.bbox.center.x == pytest.approx(center.x, abs=15)
    assert line.bbox.center.y == pytest.approx(center.y, abs=15)
    doc.close()


def test_image_watermark_and_background(fixture_pdf, png: Path) -> None:
    doc = ENGINE.open(fixture_pdf("text_multipage").read_bytes())
    from pdfeditor.engine.base import ColorMode, RenderRequest

    apply_watermark(ENGINE, doc, Watermark(image=png.read_bytes(), opacity=0.5), [1])
    center = doc.page(1).rect.center
    probe = RenderRequest(
        color=ColorMode.RGB, clip=Rect(center.x, center.y + 50, center.x + 1, center.y + 51)
    )
    r, _g, b = doc.page(1).render(probe).samples[:3]
    assert b > r + 60  # blue image blended at 50% over white paper
    apply_background(doc, Color(0.8, 0.9, 1), [2])
    bounds = content_bounds(doc, 2)
    assert bounds is not None and bounds.width > doc.page(2).rect.width - 2  # whole page tinted
    doc.close()


def test_trimmed_rect(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("vector_art"))
    rect = trimmed_rect(doc, 0, padding=5)
    assert rect is not None
    assert rect.x0 == pytest.approx(72 - 5, abs=4) and rect.y0 < 100
    assert rect.x1 < doc.page(0).rect.width - 50
    blank = ENGINE.new_document()
    blank.insert_blank_page(0, 200, 200)
    assert trimmed_rect(blank, 0) is None
    blank.close()
    doc.close()


def test_size_split_is_log_linear_and_correct() -> None:
    from pdfeditor.services.assembly import _groups_by_size

    calls: list[int] = []

    def size_of(group: list[int]) -> int:
        calls.append(len(group))
        return 1000 + 100 * len(group)  # fixed overhead + 100 bytes per page

    groups = _groups_by_size(1000, 1000 + 100 * 37, size_of)
    assert all(len(g) == 37 for g in groups[:-1]) and sum(map(len, groups)) == 1000
    assert [g[0] for g in groups] == list(range(0, 1000, 37))
    assert len(calls) < 400  # was ~1000 measurements with the linear scan
    # a page bigger than the limit still ends up in a group of its own
    assert _groups_by_size(3, 10, lambda g: 50 * len(g)) == [[0], [1], [2]]
