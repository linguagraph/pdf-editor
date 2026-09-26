"""Destination points on rotated target pages (backend test: builds input with PyMuPDF)."""

from __future__ import annotations

import pymupdf
import pytest

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Point
from pdfeditor.model.outline import Destination, OutlineItem


def _rotated_doc() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page().set_rotation(90)
    # A link on page 1 targeting the top-left region of the rotated page 2. insert_link takes
    # unrotated coordinates: unrotated (100, 200) is visible (842 - 200, 100) = (642, 100).
    doc[0].insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(10, 10, 50, 50),
            "page": 1,
            "to": pymupdf.Point(100, 200),
        }
    )
    return doc.tobytes()


def test_link_dest_on_rotated_page_is_visible_space() -> None:
    doc = get_engine("mupdf").open(_rotated_doc())
    (link,) = doc.page(0).links()
    assert link.dest is not None and link.dest.page_index == 1
    assert link.dest.point == Point(642, 100)
    doc.close()


def test_outline_dest_roundtrip_on_rotated_page() -> None:
    doc = get_engine("mupdf").open(_rotated_doc())
    doc.set_outline([OutlineItem("rotated", dest=Destination(1, Point(300, 40)))])
    data = doc.to_bytes()
    doc.close()
    again = get_engine("mupdf").open(data)
    (item,) = again.outline()
    assert item.dest is not None and item.dest.point is not None
    assert (item.dest.point.x, item.dest.point.y) == pytest.approx((300, 40))
    again.close()
