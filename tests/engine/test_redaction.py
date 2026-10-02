"""Contract tests for redaction and sanitization, plus the leak-checking verification service."""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest

from pdfeditor.engine.base import Engine, SaveOptions
from pdfeditor.model.annotations import AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Rect
from pdfeditor.model.objects import ObjectType
from pdfeditor.model.redaction import ImageRedaction, RedactOptions, SanitizeOptions
from pdfeditor.services.redaction import (
    MarkStyle,
    find_sensitive,
    luhn_ok,
    mark_areas,
    mark_for_area,
    mark_for_hit,
    verify,
)


class _Cache:
    """Minimal TextIndexCache stand-in bound to a bare Document (no session needed)."""

    def __init__(self, doc) -> None:
        from pdfeditor.services.text import PageTextIndex

        self._doc = doc
        self._index = PageTextIndex
        self.session = type("S", (), {"page_count": doc.page_count})()

    def get(self, page: int):
        return self._index(self._doc.page(page).text_page(with_chars=True))


@pytest.fixture
def redactor(engine: Engine) -> Engine:
    if not engine.capabilities.redact:
        pytest.skip("engine can't redact")
    return engine


def _open(engine: Engine, fixture_pdf, tmp_path: Path, name: str = "sensitive"):
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    return engine.open(path), path


def _text(doc) -> str:
    return doc.page(0).text_page(with_chars=False).text


def test_presets_find_personal_data(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(redactor, fixture_pdf, tmp_path)
    cache = _Cache(doc)
    found = {
        h.text
        for h in find_sensitive(
            cache,
            [
                "Email addresses",
                "Credit card numbers",
                "IBAN",
                "US Social Security numbers",
                "Phone numbers",
            ],
        )
    }
    assert "jane.example@example.com" in found
    assert "4111 1111 1111 1111" in found
    assert any(t.startswith("DE89 3704") for t in found)
    assert "123-45-6789" in found
    assert any("555" in t for t in found)
    assert luhn_ok("4111 1111 1111 1111") and not luhn_ok("4111 1111 1111 1112")
    doc.close()


def test_mark_apply_verify(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(redactor, fixture_pdf, tmp_path)
    page = doc.page(0)
    hits = find_sensitive(_Cache(doc), ["Email addresses", "US Social Security numbers"])
    style = MarkStyle(fill=Color(0, 0, 0), overlay_text="REDACTED")
    marks = [page.add_annotation(mark_for_hit(h, style)) for h in hits]
    assert all(m.type is AnnotationType.REDACT and m.overlay_text == "REDACTED" for m in marks)
    areas = {0: [r for m in marks for r in mark_areas(m)]}
    assert "jane.example@example.com" in _text(doc)  # marks alone remove nothing
    applied = doc.page(0).apply_redactions(None, RedactOptions())
    assert applied == len(marks)
    text = _text(doc)
    assert "jane.example" not in text and "123-45-6789" not in text
    assert "Public paragraph that must survive." in text and "Card: 4111" in text
    report = verify(doc, areas, overlay_texts=["REDACTED"])
    assert report.ok, report.leaks
    assert not verify(doc, areas).ok  # without knowing the label, it looks like a leak
    assert report.images_checked == 1  # the background image under the text was blanked
    assert not [a for a in doc.page(0).annotations() if a.type is AnnotationType.REDACT]
    doc.save(options=SaveOptions(garbage=4))
    doc.close()
    raw = path.read_bytes()
    with pikepdf.open(path) as pdf:
        streams = b"".join(
            bytes(pdf.pages[0].Contents.read_bytes())
            if not isinstance(pdf.pages[0].Contents, pikepdf.Array)
            else b"".join(c.read_bytes() for c in pdf.pages[0].Contents)
            for _ in [0]
        )
    assert b"jane.example" not in raw and b"jane.example" not in streams


def test_multi_line_text_mark(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    """A mark over text on two lines keeps one quad per line (adding one used to fail)."""
    from pdfeditor.model.geometry import Quad

    doc, path = _open(redactor, fixture_pdf, tmp_path)
    index = _Cache(doc).get(0)
    first_line_end = index.line_range(0)[1]
    rects = index.rects(0, first_line_end + 7)  # "Customer: Jane Example" and "Email: "
    assert len(rects) >= 2
    box = rects[0]
    for r in rects[1:]:
        box = box.union(r)
    mark = mark_for_area(0, box)
    mark.quads = tuple(Quad.from_rect(r) for r in rects)
    added = doc.page(0).add_annotation(mark)
    assert added.type is AnnotationType.REDACT
    assert doc.page(0).apply_redactions(None, RedactOptions()) == 1
    text = _text(doc)
    assert "Customer" not in text and "Jane Example" not in text  # leak check
    assert "Phone: +1 (555) 010-7788" in text  # the next line is untouched
    doc.save(options=SaveOptions(garbage=4))
    doc.close()
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 1


def test_selective_apply_keeps_other_marks(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(redactor, fixture_pdf, tmp_path)
    page = doc.page(0)
    hits = find_sensitive(_Cache(doc), ["Email addresses", "US Social Security numbers"])
    email, ssn = (page.add_annotation(mark_for_hit(h)) for h in hits)
    assert doc.page(0).apply_redactions([email.id], RedactOptions()) == 1
    text = _text(doc)
    assert "jane.example" not in text and "123-45-6789" in text
    remaining = [a for a in doc.page(0).annotations() if a.type is AnnotationType.REDACT]
    assert [a.name for a in remaining] == [ssn.name]  # the other mark is still pending
    doc.close()


def test_verification_catches_leaks(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(redactor, fixture_pdf, tmp_path)
    page = doc.page(0)
    (image,) = (o for o in page.content_objects() if o.type is ObjectType.IMAGE)
    area = Rect(image.bbox.x0 + 10, image.bbox.y0 + 5, image.bbox.x0 + 200, image.bbox.y0 + 30)
    mark = page.add_annotation(mark_for_area(0, area))
    # apply with images left alone and text kept: both kinds of leak must be reported
    doc.page(0).apply_redactions([mark.id], RedactOptions(images=ImageRedaction.NONE, text=False))
    report = verify(doc, {0: [area]})
    assert not report.ok
    assert any("text" in leak for leak in report.leaks) and any(
        "image" in leak for leak in report.leaks
    )
    doc.close()


def test_sanitize_removes_hidden_data(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(redactor, fixture_pdf, tmp_path)
    report = doc.sanitize(SanitizeOptions())
    joined = " | ".join(report)
    for expected in (
        "comment",
        "properties",
        "XMP",
        "JavaScript",
        "attached",
        "link",
        "hidden text",
    ):
        assert expected in joined, (expected, report)
    info = doc.info()
    assert not info.has_javascript and not info.has_xmp
    assert doc.embedded_files() == [] and doc.page(0).links() == []
    assert doc.metadata().author == "" and doc.page(0).annotations() == []
    assert "invisible OCR layer" not in _text(doc)
    assert "Public paragraph that must survive." in _text(doc)
    doc.save(options=SaveOptions(garbage=4))
    doc.close()
    raw = path.read_bytes()
    assert b"secret attachment" not in raw and b"app.alert" not in raw
    again = redactor.open(path)
    assert "Jane Example" in _text(again)  # visible content isn't touched by sanitizing
    again.close()


def test_text_edit_preserves_pending_marks(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(redactor, fixture_pdf, tmp_path, "mixed_content")
    page = doc.page(0)
    before = [a for a in page.annotations() if a.type is AnnotationType.REDACT]
    heading = next(o for o in page.content_objects() if o.type is ObjectType.TEXT)
    page.replace_text(heading.key, "Edited heading")
    after = [a for a in doc.page(0).annotations() if a.type is AnnotationType.REDACT]
    assert len(after) == len(before) == 1
    assert after[0].rect.intersects(before[0].rect)
    doc.close()


# -- hidden layers and off-page text ------------------------------------------------------------
def _pdfium_text(path: Path) -> str:
    import pypdfium2 as pdfium

    pd = pdfium.PdfDocument(path)
    try:
        return "\n".join(page.get_textpage().get_text_range() for page in pd)
    finally:
        pd.close()


def _pdfium_renders(path: Path) -> list:
    import pypdfium2 as pdfium

    pd = pdfium.PdfDocument(path)
    try:
        return [page.render(scale=1, draw_annots=False).to_pil().convert("L") for page in pd]
    finally:
        pd.close()


def _same_pixels(a, b) -> bool:
    from PIL import ImageChops

    if a.size != b.size:
        return False
    diff = ImageChops.difference(a, b)
    return max(diff.getextrema()) <= 8  # antialiasing noise only


def _streams_mention(path: Path, needle: bytes) -> bool:
    """Any decoded stream holds ``needle`` as a literal or hex string (independent of MuPDF)."""
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    data = obj.read_bytes().lower()
                except pikepdf.PdfError:
                    continue
                if needle.lower() in data or needle.hex().encode() in data:
                    return True
    return False


def test_sanitize_removes_hidden_layers(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(redactor, fixture_pdf, tmp_path, "hidden_layers")
    original = fixture_pdf("hidden_layers")
    assert "secret" in _pdfium_text(original)  # pdfium extracts hidden-layer text
    before = _pdfium_renders(original)
    report = doc.sanitize(SanitizeOptions(comments=False))
    layer_line = next(r for r in report if "hidden layer" in r)
    assert '"Secret layer"' in layer_line and "6 content section(s)" in layer_line
    assert [layer.name for layer in doc.layers()] == ["Shown layer"]
    doc.save(options=SaveOptions(garbage=4))
    doc.close()
    text = _pdfium_text(path)
    assert "secret" not in text.lower()
    for visible in ("Always visible", "On the shown layer", "Visible form text", "Visible footer"):
        assert visible in text
    assert not _streams_mention(path, b"secret")
    assert b"Secret layer" not in path.read_bytes()
    after = _pdfium_renders(path)
    assert all(_same_pixels(a, b) for a, b in zip(before, after, strict=True))
    with pikepdf.open(path) as pdf:
        assert len(pdf.Root.OCProperties.OCGs) == 1
    again = redactor.open(path)
    assert "Always visible" in _text(again)
    again.close()


def test_sanitize_removes_off_page_text(redactor: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(redactor, fixture_pdf, tmp_path, "off_page_text")
    original = fixture_pdf("off_page_text")
    assert "Far left secret" in _pdfium_text(original)
    before = _pdfium_renders(original)
    report = doc.sanitize(SanitizeOptions())
    assert any("off-page text" in r for r in report), report
    doc.save(options=SaveOptions(garbage=4))
    doc.close()
    text = _pdfium_text(path)
    assert "secret" not in text.lower()
    assert text.count("Visible text stays") == 2
    assert text.count("Stradd") == 2  # glyphs partly inside the crop box stay
    assert "Straddle" not in text
    assert not _streams_mention(path, b"secret")
    after = _pdfium_renders(path)
    assert all(_same_pixels(a, b) for a, b in zip(before, after, strict=True))
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 2


def test_sanitize_options_keep_layers_and_off_page_text(
    redactor: Engine, fixture_pdf, tmp_path: Path
) -> None:
    keep = SanitizeOptions(hidden_layers=False, off_page_text=False)
    for name, needle in (("hidden_layers", "Hidden layer secret"), ("off_page_text", "Far left")):
        doc, path = _open(redactor, fixture_pdf, tmp_path, name)
        report = doc.sanitize(keep)
        assert not any("hidden layer" in r or "off-page" in r for r in report)
        doc.save(options=SaveOptions(garbage=4))
        doc.close()
        assert needle in _pdfium_text(path)


def test_sanitize_without_layers_or_off_page_text_reports_nothing(
    redactor: Engine, fixture_pdf, tmp_path: Path
) -> None:
    doc, _ = _open(redactor, fixture_pdf, tmp_path, "sensitive")
    report = doc.sanitize(SanitizeOptions())
    assert not any("hidden layer" in r or "off-page" in r for r in report)
    doc.close()
