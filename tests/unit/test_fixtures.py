"""The generated corpus is valid for independent PDF implementations."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest

from tests.conftest import FIXTURES

PLAIN = sorted(p for p in FIXTURES.glob("*.pdf") if p.stem not in {"encrypted", "broken_xref"})


@pytest.mark.parametrize("path", PLAIN, ids=lambda p: p.stem)
def test_fixture_opens_in_pikepdf_and_pdfium(path: Path) -> None:
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) > 0
    doc = pdfium.PdfDocument(path)
    assert len(doc) > 0
    doc.close()


def test_encrypted_requires_password(fixture_pdf) -> None:
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(fixture_pdf("encrypted"))
    with pikepdf.open(fixture_pdf("encrypted"), password="user") as pdf:
        assert len(pdf.pages) == 1


def test_broken_xref_is_repairable(fixture_pdf) -> None:
    with pikepdf.open(fixture_pdf("broken_xref")) as pdf:
        assert len(pdf.pages) == 1
