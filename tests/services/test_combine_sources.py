from __future__ import annotations

from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.core.jobs import Cancelled, CancelToken
from pdfeditor.engine.registry import get_engine
from pdfeditor.services.assembly import MergeSource, describe_source, merge
from pdfeditor.services.pages import move_order

ENGINE = get_engine()


def describe(path: Path, password: str | None = None):
    with ENGINE_LOCK:
        return describe_source(ENGINE, path, 100, 150, password)


def test_describe_pdf_counts_pages_and_previews(fixture_pdf) -> None:
    info = describe(fixture_pdf("outline"))
    assert info.page_count == 6 and not info.locked and info.error == ""
    assert info.preview is not None
    assert info.preview.width <= 100 and info.preview.height <= 150
    assert max(info.preview.width, info.preview.height * 100 / 150) >= 99


def test_describe_encrypted_needs_the_password(fixture_pdf) -> None:
    path = fixture_pdf("encrypted")
    for password in (None, "nope"):
        info = describe(path, password)
        assert info.locked and info.page_count == 0 and info.error == ""
    info = describe(path, "user")
    assert not info.locked and info.page_count == 1 and info.preview is not None


def test_describe_reports_unreadable_files(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf at all")
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    for path in (junk, empty, tmp_path / "missing.pdf"):
        info = describe(path)
        assert info.error and info.page_count == 0 and not info.locked, path.name
    assert describe(tmp_path / "missing.pdf").error == "file not found"


def test_describe_image_is_one_page(tmp_path: Path) -> None:
    good = tmp_path / "cover.png"
    Image.new("RGB", (120, 160), (0, 0, 200)).save(good)
    info = describe(good)
    assert info.page_count == 1 and info.error == "" and info.preview is not None
    bad = tmp_path / "garbage.png"
    bad.write_bytes(b"this is not an image")
    info = describe(bad)
    assert info.error == "this image can't be read" and info.page_count == 0


def test_merge_progress_and_cancel(fixture_pdf) -> None:
    sources = [MergeSource(fixture_pdf("outline")), MergeSource(fixture_pdf("report"))]
    seen: list[tuple[int, int]] = []
    with ENGINE_LOCK:
        doc = merge(ENGINE, sources, True, None, lambda d, t: seen.append((d, t)))
        doc.close()
    assert seen == [(1, 2), (2, 2)]
    token = CancelToken()
    token.cancel()
    with ENGINE_LOCK, pytest.raises(Cancelled):
        merge(ENGINE, sources, True, token)


def test_merged_file_round_trips(fixture_pdf, tmp_path: Path) -> None:
    png = tmp_path / "cover.png"
    Image.new("RGB", (120, 160), (200, 0, 0)).save(png)
    sources = [MergeSource(png), MergeSource(fixture_pdf("outline"), [0, 2])]
    out = tmp_path / "merged.pdf"
    with ENGINE_LOCK:
        doc = merge(ENGINE, sources, True)
        try:
            doc.save(out)
        finally:
            doc.close()
    with ENGINE_LOCK:
        again = ENGINE.open(out)
        try:
            assert again.page_count == 3
        finally:
            again.close()
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 3
    rendered = pdfium.PdfDocument(str(out))[0].render(scale=0.5).to_pil().convert("L")
    assert rendered.getextrema() != (255, 255)  # not blank


def test_move_order() -> None:
    assert move_order(5, [0], 3) == ([1, 2, 0, 3, 4], [2])
    assert move_order(5, [3, 4], 0) == ([3, 4, 0, 1, 2], [0, 1])
    assert move_order(3, [1], 1) == ([0, 1, 2], [1])
    assert move_order(3, [0], 3) == ([1, 2, 0], [2])
