"""Conversions between PyMuPDF objects and ``pdfeditor.model`` types."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pymupdf

from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Point, Quad, Rect


def rect(r: Any) -> Rect:
    return Rect(float(r[0]), float(r[1]), float(r[2]), float(r[3]))


def to_fz_rect(r: Rect) -> pymupdf.Rect:
    return pymupdf.Rect(r.x0, r.y0, r.x1, r.y1)


def point(p: Any) -> Point:
    return Point(float(p[0]), float(p[1]))


def to_fz_point(p: Point) -> pymupdf.Point:
    return pymupdf.Point(p.x, p.y)


def quad(q: pymupdf.Quad) -> Quad:
    return Quad(point(q.ul), point(q.ur), point(q.ll), point(q.lr))


def to_fz_quad(q: Quad) -> pymupdf.Quad:
    return pymupdf.Quad(to_fz_point(q.ul), to_fz_point(q.ur), to_fz_point(q.ll), to_fz_point(q.lr))


def to_fz_matrix(m: Matrix) -> pymupdf.Matrix:
    return pymupdf.Matrix(m.a, m.b, m.c, m.d, m.e, m.f)


def color(comps: Sequence[float] | None) -> Color | None:
    if not comps:
        return None
    return Color.from_components(list(comps))


_PDF_DATE = re.compile(
    r"^D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Zz+\-])?(\d{2})?'?(\d{2})?'?"
)


def parse_pdf_date(value: str | None) -> datetime | None:
    """Parse a PDF date string like ``D:20240131235959+01'00'`` (lenient)."""
    if not value:
        return None
    m = _PDF_DATE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, sign, tzh, tzm = m.groups()
    tz: timezone | None = None
    if sign in ("Z", "z"):
        tz = UTC
    elif sign in ("+", "-"):
        offset = timedelta(hours=int(tzh or 0), minutes=int(tzm or 0))
        tz = timezone(offset if sign == "+" else -offset)
    try:
        return datetime(
            int(year),
            int(month or 1),
            int(day or 1),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=tz,
        )
    except ValueError:
        return None


def format_pdf_date(dt: datetime) -> str:
    base = dt.strftime("D:%Y%m%d%H%M%S")
    offset = dt.utcoffset()
    if offset is None:
        return base
    if offset == timedelta(0):
        return base + "Z"
    total = int(offset.total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total), 60)
    return f"{base}{sign}{hh:02d}'{mm:02d}'"
