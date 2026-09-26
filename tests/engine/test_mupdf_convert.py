"""Backend-specific helpers (allowed to import the mupdf backend directly)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from pdfeditor.engine.mupdf.convert import format_pdf_date, parse_pdf_date


def test_parse_pdf_dates() -> None:
    assert parse_pdf_date("D:20240131235959+01'00'") == datetime(
        2024, 1, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=1))
    )
    assert parse_pdf_date("D:2024") == datetime(2024, 1, 1)
    assert parse_pdf_date("D:20240101000000Z") == datetime(2024, 1, 1, tzinfo=UTC)
    assert parse_pdf_date("garbage") is None
    assert parse_pdf_date("") is None


def test_format_roundtrip() -> None:
    dt = datetime(2025, 6, 7, 8, 9, 10, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))
    assert format_pdf_date(dt) == "D:20250607080910-05'30'"
    assert parse_pdf_date(format_pdf_date(dt)) == dt


def test_pdf_numbers_never_use_exponents() -> None:
    from pdfeditor.engine.mupdf.annots import pdf_num

    assert pdf_num(2.48081e-06) == "0"
    assert pdf_num(-1e-7) == "0"
    assert pdf_num(1.5) == "1.5" and pdf_num(100.0) == "100" and pdf_num(-0.25) == "-0.25"
    assert "e" not in pdf_num(1e20)
