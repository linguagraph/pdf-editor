"""Opening damaged, empty, foreign and unsupported files: clear errors or repair, no crashes."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pikepdf
import pytest

from pdfeditor.engine.base import Engine, OpenError


def _with_encrypt_filter(src: Path, out: Path, handler: str) -> Path:
    """A copy whose trailer names an /Encrypt dictionary with ``handler`` as its /Filter."""
    with pikepdf.open(src) as pdf:
        pdf.save(out, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    raw = out.read_bytes()
    n = max(int(m) for m in re.findall(rb"(\d+) 0 obj", raw)) + 1
    obj = f"{n} 0 obj\n<< /Filter /{handler} /V 4 /R 4 >>\nendobj\n".encode()
    at = raw.rindex(b"\nxref") + 1  # before the classic xref section (not "startxref")
    raw = raw[:at] + obj + raw[at:]
    raw = re.sub(rb"trailer\s*<<", b"trailer << /Encrypt %d 0 R" % n, raw, count=1)
    out.write_bytes(raw)
    return out


def test_truncated_file_is_repaired(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    data = fixture_pdf("report").read_bytes()
    path = tmp_path / "truncated.pdf"
    path.write_bytes(data[: len(data) // 2])
    doc = engine.open(path)
    assert doc.info().is_repaired and doc.page_count >= 1
    assert doc.page(0).text_page(with_chars=False).text.strip()
    doc.close()


@pytest.mark.parametrize(
    ("content", "message"),
    [(b"", "empty"), (os.urandom(3000), "isn't a PDF"), (b"%PDF-1.7\n", "isn't a PDF")],
)
def test_unreadable_files(engine: Engine, tmp_path: Path, content: bytes, message: str) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(content)
    with pytest.raises(OpenError, match=message):
        engine.open(path)


def test_certificate_security_is_refused(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    path = _with_encrypt_filter(fixture_pdf("report"), tmp_path / "pubsec.pdf", "Adobe.PubSec")
    with pytest.raises(OpenError, match=r"Adobe.PubSec"):
        engine.open(path)


def test_standard_security_still_opens(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("encrypted"), "user")
    assert doc.page_count == 1
    doc.close()
