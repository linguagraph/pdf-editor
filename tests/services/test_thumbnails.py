from __future__ import annotations

from pathlib import Path

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.engine.registry import get_engine
from pdfeditor.services.thumbnails import first_page_thumbnail


def test_first_page_fits_the_box(fixture_pdf) -> None:
    with ENGINE_LOCK:
        result = first_page_thumbnail(get_engine(), fixture_pdf("images"), 100, 200)
    assert result is not None
    assert result.width <= 100 and result.height <= 200
    assert max(result.width, result.height / 2) >= 99  # scaled to fit, not smaller


def test_no_thumbnail_without_asking(fixture_pdf, tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf at all")
    with ENGINE_LOCK:
        engine = get_engine()
        assert first_page_thumbnail(engine, fixture_pdf("encrypted"), 100, 100) is None
        assert first_page_thumbnail(engine, tmp_path / "missing.pdf", 100, 100) is None
        assert first_page_thumbnail(engine, junk, 100, 100) is None
