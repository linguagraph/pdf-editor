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
