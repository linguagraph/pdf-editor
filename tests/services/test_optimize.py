from __future__ import annotations

import io

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.engine.base import OptimizeOptions
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.metadata import SPACE_CATEGORIES
from pdfeditor.services.optimize import (
    PRESETS,
    ReduceOptions,
    audit,
    preset,
    reduce_size,
    with_images,
)

ENGINE = get_engine()


@pytest.fixture
def heavy(fixture_pdf):
    doc = ENGINE.open(fixture_pdf("heavy"))
    yield doc
    doc.close()


def image_size(data: bytes) -> tuple[int, int]:
    doc = ENGINE.open(data)
    try:
        info = doc.images()
        return info[0].width, info[0].height
    finally:
        doc.close()


def test_audit_categories(heavy) -> None:
    usage = audit(heavy)
    assert set(usage.categories) == set(SPACE_CATEGORIES)
    assert usage.total == sum(usage.categories.values())
    assert usage.share("Images") > 0.7 and usage.share("Fonts") > 0.15
    assert usage.categories["Metadata and thumbnails"] > 1000  # the page thumbnail


def test_presets_shrink_in_order(heavy, fixture_pdf) -> None:
    on_disk = fixture_pdf("heavy").stat().st_size
    sizes = {}
    for p in PRESETS:
        result = reduce_size(heavy, p.options, before=on_disk)
        assert result.before == on_disk and result.after == len(result.data)
        sizes[p.key] = result.after
        pdfium.PdfDocument(result.data)  # independent reader accepts it
        with pikepdf.open(io.BytesIO(result.data)) as pdf:
            assert len(pdf.pages) == 1
    assert sizes["screen"] < sizes["ebook"] < sizes["print"] < sizes["lossless"] < on_disk
    assert sizes["ebook"] < on_disk / 20
    # images are downsampled to the target resolution (4 inches wide on the page)
    assert image_size(reduce_size(heavy, preset("ebook").options).data)[0] == pytest.approx(
        600, abs=2
    )
    assert image_size(reduce_size(heavy, preset("lossless").options).data) == (2400, 1600)


def test_text_survives_and_open_document_is_untouched(heavy) -> None:
    before = heavy.page(0).text_page(with_chars=False).text
    result = reduce_size(heavy, preset("screen").options)
    out = ENGINE.open(result.data)
    assert out.page(0).text_page(with_chars=False).text == before
    fonts = out.fonts()
    out.close()
    assert any(f.subset for f in fonts)  # the full CJK font was subset
    assert image_size(heavy.to_bytes()) == (2400, 1600)  # the session doc is unchanged


def test_grayscale_and_thumbnails(heavy) -> None:
    opts = with_images(preset("ebook").options, 150, 70, gray=True)
    data = reduce_size(heavy, opts).data
    doc = ENGINE.open(data)
    raw, _ext = doc.extract_image(doc.images()[0].ref)
    with Image.open(io.BytesIO(raw)) as img:
        rgb = img.convert("RGB")
        samples = [rgb.getpixel((x, y)) for x in range(0, 600, 37) for y in range(0, 400, 29)]
    assert all(max(p) - min(p) <= 3 for p in samples)  # neutral: no colour left
    assert doc.space_usage().categories["Metadata and thumbnails"] < 500
    doc.close()


def test_linearize(heavy) -> None:
    opts = ReduceOptions(OptimizeOptions(image_dpi=72, jpeg_quality=50), linearize=True)
    result = reduce_size(heavy, opts)
    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        assert pdf.is_linearized
    assert result.notes == []


def test_encrypted_stays_encrypted_and_skips_linearize(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("encrypted"), "user")
    result = reduce_size(doc, ReduceOptions(linearize=True))
    doc.close()
    assert any("encrypted" in n for n in result.notes)
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(io.BytesIO(result.data))
    ENGINE.open(result.data, "user").close()


def test_already_compact_is_reported(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("text_multipage"))
    small = reduce_size(doc, preset("lossless").options)
    doc.close()
    again = ENGINE.open(small.data)
    result = reduce_size(again, preset("lossless").options, before=len(small.data))
    again.close()
    assert result.after <= result.before * 1.05
