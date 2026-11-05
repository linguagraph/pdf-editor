"""Checking the signatures in a file: verdicts, revisions, placement, and no fetching."""

from __future__ import annotations

import datetime as dt
import socket
import sys
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Rect
from pdfeditor.model.signing import (
    PageBoxes,
    PfxKey,
    SignatureOptions,
    SignaturePlacement,
    SignatureVerdict,
)
from pdfeditor.services.signing import (
    SigningError,
    extract_revision,
    inspect_signatures,
    sign_file,
)
from pdfeditor.services.signing.inspect import _to_visible
from tests.services import test_signing as signing_tests

local_tsa = signing_tests.local_tsa  # the pytest fixture, borrowed from the signing tests

ENGINE = get_engine()
REAL_SIGNED = Path(__file__).parent.parent / "fixtures" / "real" / "signed_selfsigned.pdf"


def _key(pfx: Path) -> PfxKey:
    return PfxKey(pfx, "test")


def _der(pfx: Path) -> bytes:
    _key_obj, cert, _others = pkcs12.load_key_and_certificates(pfx.read_bytes(), b"test")
    assert cert is not None
    return cert.public_bytes(serialization.Encoding.DER)


def _sign(source: Path, pfx: Path, target: Path, **options: object) -> Path:
    sign_file(source, target, _key(pfx), SignatureOptions(**options), ENGINE)
    return target


@pytest.fixture
def pfx(throwaway_pfx) -> Path:
    """One key for the whole test: each call to throwaway_pfx makes a new one."""
    return throwaway_pfx()


@pytest.fixture
def signed(fixture_pdf, pfx: Path, tmp_path: Path) -> Path:
    return _sign(fixture_pdf("images"), pfx, tmp_path / "signed.pdf")


def test_unsigned_file_has_no_signatures(fixture_pdf) -> None:
    assert inspect_signatures(fixture_pdf("images"), None, []) == []


def test_signed_with_its_certificate_as_root_is_valid(signed: Path, pfx: Path) -> None:
    (report,) = inspect_signatures(signed, None, [_der(pfx)])
    assert report.verdict is SignatureVerdict.VALID
    assert report.intact and report.trusted
    assert not report.modified_after
    assert report.reason == ""
    assert report.signer_name == "Elanero Test Signer"
    assert report.revision == 1


def test_signed_without_trust_roots_is_untrusted_never_valid(signed: Path) -> None:
    (report,) = inspect_signatures(signed, None, [])
    assert report.verdict is SignatureVerdict.UNTRUSTED
    assert report.verdict is not SignatureVerdict.VALID
    assert report.intact and not report.trusted
    assert "identity couldn't be confirmed" in report.summary


def test_flipped_byte_in_the_signed_range_is_invalid(signed: Path, pfx: Path) -> None:
    data = bytearray(signed.read_bytes())
    data[20] ^= 0x01  # inside the header comment, which the signature covers
    signed.write_bytes(bytes(data))
    (report,) = inspect_signatures(signed, None, [_der(pfx)])
    assert report.verdict is SignatureVerdict.INVALID
    assert not report.intact
    assert report.reason


def test_change_after_signing_is_flagged_and_not_valid(signed: Path, pfx: Path) -> None:
    with signed.open("ab") as fh:
        fh.write(b"\n% a later edit\n")
    (report,) = inspect_signatures(signed, None, [_der(pfx)])
    assert report.modified_after
    assert report.verdict is SignatureVerdict.UNTRUSTED
    assert report.intact
    assert "changed after" in report.summary


def test_two_signatures_are_ordered_and_the_first_reports_later_changes(
    fixture_pdf, pfx: Path, tmp_path: Path
) -> None:
    first = _sign(fixture_pdf("images"), pfx, tmp_path / "one.pdf")
    second = _sign(first, pfx, tmp_path / "two.pdf")
    reports = inspect_signatures(second, None, [_der(pfx)])
    assert [r.revision for r in reports] == [1, 2]
    assert reports[0].modified_after
    assert not reports[1].modified_after
    assert reports[1].verdict is SignatureVerdict.VALID
    assert reports[0].revision_end < reports[1].revision_end

    # The first revision on its own is the file as it was when that signature was applied.
    revision = tmp_path / "rev1.pdf"
    extract_revision(second, reports[0].revision_end, revision)
    (alone,) = inspect_signatures(revision, None, [_der(pfx)])
    assert alone.verdict is SignatureVerdict.VALID
    with pikepdf.open(revision) as pdf:
        assert len(pdf.pages) >= 1
    doc = pdfium.PdfDocument(str(revision))
    try:
        doc[0].render(scale=0.5).to_pil()
    finally:
        doc.close()


def test_timestamped_signature_reports_a_trusted_timestamp(
    fixture_pdf, pfx: Path, local_tsa: str, tmp_path: Path
) -> None:
    target = _sign(fixture_pdf("images"), pfx, tmp_path / "ts.pdf", tsa_url=local_tsa)
    (report,) = inspect_signatures(target, None, [_der(pfx)])
    assert report.time_source == "trusted timestamp"
    assert report.signing_time is not None


def test_untimestamped_signature_uses_the_signers_clock(signed: Path, pfx: Path) -> None:
    (report,) = inspect_signatures(signed, None, [_der(pfx)])
    assert report.time_source == "signer's computer clock"


def test_visible_signature_has_page_and_rect_invisible_has_neither(
    fixture_pdf, pfx: Path, tmp_path: Path
) -> None:
    visible = _sign(
        fixture_pdf("images"),
        pfx,
        tmp_path / "visible.pdf",
        placement=SignaturePlacement(0, Rect(50, 50, 250, 110)),
    )
    (report,) = inspect_signatures(visible, None, [_der(pfx)])
    assert not report.invisible
    assert report.page == 0
    assert report.rect is not None
    assert report.rect.x0 == pytest.approx(50, abs=1)
    assert report.rect.y0 == pytest.approx(50, abs=1)  # visible space: origin top-left

    invisible = _sign(fixture_pdf("images"), pfx, tmp_path / "invisible.pdf")
    (report,) = inspect_signatures(invisible, None, [_der(pfx)])
    assert report.invisible
    assert report.page is None and report.rect is None


def test_visible_rect_is_converted_for_a_rotated_page() -> None:
    boxes = PageBoxes(media=(0, 0, 200, 300), rotate=90)
    # Rotated 90 degrees, the user point (10, 20) sits at visible x = 20 (its y) and visible
    # y = 10 (its x), measured from the top-left corner.
    assert _to_visible(boxes, 10, 20) == (20, 10)


def test_damaged_signature_dictionary_is_a_clear_error(signed: Path) -> None:
    data = bytearray(signed.read_bytes())
    marker = data.rfind(b"/ByteRange")
    data[marker + 12] = ord("x")  # break the ByteRange array itself
    signed.write_bytes(bytes(data))
    with pytest.raises(SigningError, match="can't be read"):
        inspect_signatures(signed, None, [])


def test_real_signed_sample_does_not_crash() -> None:
    reports = inspect_signatures(REAL_SIGNED, None, [])
    assert reports
    assert all(r.verdict is not SignatureVerdict.VALID for r in reports)


def test_protected_document_is_refused_without_a_password(fixture_pdf, tmp_path: Path) -> None:
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(fixture_pdf("images")) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="u", owner="o"))
    with pytest.raises(SigningError, match="protected document"):
        inspect_signatures(locked, None, [])


def test_checking_makes_no_network_connection(signed: Path, pfx: Path, monkeypatch) -> None:
    real_connect = socket.socket.connect

    def refuse_remote(self: socket.socket, address: object) -> None:
        # asyncio's event loop links itself up over 127.0.0.1; anything else is a fetch.
        if isinstance(address, tuple) and address and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError(f"signature checks must stay offline (tried {address[0]})")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", refuse_remote)
    reports = inspect_signatures(signed, None, [_der(pfx)])
    assert reports[0].verdict is SignatureVerdict.VALID


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows certificate stores")
def test_root_certificates_are_der_and_use_no_key_api(monkeypatch) -> None:
    from asn1crypto import x509

    from pdfeditor.services.signing import windows_store

    real = windows_store._api()
    touched: list[str] = []

    class Recorder:
        def __getattr__(self, name: str):
            touched.append(name)
            return getattr(real, name)

    monkeypatch.setattr(windows_store, "_api", lambda: Recorder())
    roots = windows_store.root_certificates()
    assert isinstance(roots, list)
    for der in roots[:20]:
        x509.Certificate.load(der)
    assert touched
    assert not [n for n in touched if "Key" in n or "NCrypt" in n or "Acquire" in n]


def test_root_certificates_is_empty_off_windows(monkeypatch) -> None:
    from pdfeditor.services.signing import windows_store

    monkeypatch.setattr(windows_store, "available", lambda: False)
    assert windows_store.root_certificates() == []


def test_another_trusted_certificate_does_not_validate_this_one(
    signed: Path, throwaway_pfx
) -> None:
    other = throwaway_pfx(name="Someone Else")
    (report,) = inspect_signatures(signed, None, [_der(other)])
    assert report.verdict is SignatureVerdict.UNTRUSTED
    assert isinstance(report.checked_at, dt.datetime)
