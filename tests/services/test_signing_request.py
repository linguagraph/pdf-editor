"""Signature fields for recipients: prepare a file, then each recipient signs their own field."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageChops

from pdfeditor.core.jobs import Cancelled, CancelToken
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Rect
from pdfeditor.model.signing import (
    EmptySignatureField,
    PageBoxes,
    PfxKey,
    SignatureOptions,
    SignaturePlacement,
    SignatureRequest,
)
from pdfeditor.services.signing import (
    SigningError,
    SigningRefused,
    mailto_url,
    preflight,
    prepare_for_signing,
    sign_file,
)

ENGINE = get_engine()
BOX_A = Rect(50, 50, 300, 110)
BOX_B = Rect(50, 130, 300, 190)
TOLERANCE = 1.0


def key_for(pfx: Path, password: str = "test") -> PfxKey:
    return PfxKey(pfx, password)


def assert_round_trip(path: Path, password: str | None = None) -> None:
    """The independent implementations can read the result."""
    with pikepdf.open(path, password=password or "") as pdf:
        assert len(pdf.pages) >= 1
    doc = pdfium.PdfDocument(str(path), password=password)
    try:
        doc[0].render(scale=0.5).to_pil()
    finally:
        doc.close()
    reopened = ENGINE.open(path, password)
    try:
        assert reopened.info().has_signatures
    finally:
        reopened.close()


def render_widgets(path: Path, page: int) -> Image.Image:
    doc = pdfium.PdfDocument(str(path))
    try:
        doc.init_forms()  # draws the widgets, placeholders and signatures among them
        return doc[page].render(scale=2).to_pil().convert("RGB")
    finally:
        doc.close()


def changed_area(before: Image.Image, after: Image.Image) -> bool:
    box = ImageChops.difference(before, after).point(lambda v: 255 if v > 16 else 0).getbbox()
    return box is not None


def requests_for(*boxes: tuple[str, int, Rect]) -> list[SignatureRequest]:
    return [
        SignatureRequest(name=name, email="", page=page, rect=rect) for name, page, rect in boxes
    ]


def fields_of(path: Path, password: str | None = None) -> dict[str, EmptySignatureField]:
    return {f.name: f for f in preflight(path, password).empty_fields}


def assert_same_rect(got: Rect | None, want: Rect) -> None:
    assert got is not None
    for a, b in (
        (got.x0, want.x0),
        (got.y0, want.y0),
        (got.x1, want.x1),
        (got.y1, want.y1),
    ):
        assert abs(a - b) <= TOLERANCE


def certified_pdf(source: Path, pfx: Path, out: Path) -> Path:
    """A document certified by its author with a signature that forbids all changes."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko.sign.fields import MDPPerm, SigSeedSubFilter

    signer = signers.SimpleSigner.load_pkcs12_data(pfx.read_bytes(), [], passphrase=b"test")
    meta = signers.PdfSignatureMetadata(
        field_name="Certify",
        certify=True,
        docmdp_permissions=MDPPerm.NO_CHANGES,
        subfilter=SigSeedSubFilter.PADES,
    )
    with source.open("rb") as fh, out.open("wb") as dst:
        signers.PdfSigner(meta, signer=signer).sign_pdf(IncrementalPdfFileWriter(fh), output=dst)
    return out


# -- the whole flow --------------------------------------------------------------------------
def test_prepared_fields_are_listed_then_each_is_signed(
    fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path: Path
) -> None:
    prepared = tmp_path / "for-signature.pdf"
    prepare_for_signing(
        fixture_pdf("text_multipage"),
        prepared,
        requests_for(("Ana Alvarez", 0, BOX_A), ("Ben Brook", 0, BOX_B)),
        ENGINE,
    )
    fields = preflight(prepared).empty_fields
    assert [f.name for f in fields] == ["SignatureRequest1", "SignatureRequest2"]
    assert [f.label for f in fields] == ["Signature of Ana Alvarez", "Signature of Ben Brook"]
    assert_same_rect(fields[0].rect, BOX_A)
    assert_same_rect(fields[1].rect, BOX_B)

    once = tmp_path / "once.pdf"
    sign_file(
        prepared,
        once,
        key_for(throwaway_pfx()),
        SignatureOptions(field_name="SignatureRequest1"),
        ENGINE,
    )
    twice = tmp_path / "twice.pdf"
    sign_file(
        once,
        twice,
        key_for(throwaway_pfx("ec", name="Second Signer")),
        SignatureOptions(field_name="SignatureRequest2"),
        ENGINE,
    )
    statuses = signature_status(twice)
    assert len(statuses) == 2
    assert all(s.intact and s.valid for s in statuses)
    assert statuses[0].coverage.name == "ENTIRE_REVISION"
    assert statuses[1].coverage.name == "ENTIRE_FILE"
    assert preflight(twice).empty_fields == ()
    assert_round_trip(twice)
    cms_check(twice)
    assert twice.read_bytes().startswith(prepared.read_bytes())


def test_signing_replaces_the_placeholder(fixture_pdf, throwaway_pfx, tmp_path: Path) -> None:
    prepared = tmp_path / "p.pdf"
    prepare_for_signing(
        fixture_pdf("text_multipage"),
        prepared,
        requests_for(("Ana Alvarez", 0, BOX_A)),
        ENGINE,
    )
    signed = tmp_path / "s.pdf"
    sign_file(
        prepared,
        signed,
        key_for(throwaway_pfx()),
        SignatureOptions(field_name="SignatureRequest1"),
        ENGINE,
    )
    placeholder = render_widgets(prepared, 0)
    after = render_widgets(signed, 0)
    assert changed_area(placeholder, after)
    with pikepdf.open(prepared) as before, pikepdf.open(signed) as done:
        old = before.Root.AcroForm.Fields[0].AP.N.objgen
        new = done.Root.AcroForm.Fields[0].AP.N.objgen
        assert old != new  # the widget now shows the signature's own appearance


def test_placeholder_names_the_signer_in_any_script(fixture_pdf, tmp_path: Path) -> None:
    prepared = tmp_path / "cyr.pdf"
    name = "Иван Петров"
    prepare_for_signing(
        fixture_pdf("text_multipage"),
        prepared,
        requests_for((name, 0, BOX_A)),
        ENGINE,
    )
    with pikepdf.open(prepared) as pdf:
        field = pdf.Root.AcroForm.Fields[0]
        assert str(field.TU) == f"Signature of {name}"
    assert render_widgets(prepared, 0).size[0] > 0


# -- placement ---------------------------------------------------------------------------------
@pytest.mark.parametrize("page", range(5))
def test_fields_land_where_asked_on_rotated_and_cropped_pages(
    page: int, fixture_pdf, tmp_path: Path
) -> None:
    source = fixture_pdf("rotated_pages")
    rect = Rect(40, 60, 240, 120)
    prepared = tmp_path / f"rot{page}.pdf"
    prepare_for_signing(source, prepared, requests_for(("Ana", page, rect)), ENGINE)
    [field] = preflight(prepared).empty_fields
    assert field.page == page
    assert_same_rect(field.rect, rect)


def test_visible_to_user_round_trip_for_every_rotation_and_crop() -> None:
    rect = Rect(30, 40, 180, 90)
    for rotate in (0, 90, 180, 270):
        boxes = PageBoxes(
            media=(10, 20, 610, 820),
            crop=(50, 60, 500, 700),
            rotate=rotate,
        )
        user = boxes.visible_to_user(rect)
        back = boxes.user_to_visible(user)
        got = (back.x0, back.y0, back.x1, back.y1)
        for a, b in zip(got, (rect.x0, rect.y0, rect.x1, rect.y1), strict=True):
            assert a == pytest.approx(b, abs=1e-6)


# -- what must be refused --------------------------------------------------------------------
def test_already_signed_document_keeps_its_signature(
    fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path
) -> None:
    earlier = tmp_path / "earlier.pdf"
    sign_file(
        fixture_pdf("text_multipage"),
        earlier,
        key_for(throwaway_pfx()),
        SignatureOptions(),
        ENGINE,
    )
    prepared = tmp_path / "prepared.pdf"
    prepare_for_signing(earlier, prepared, requests_for(("Ana", 0, BOX_A)), ENGINE)
    statuses = signature_status(prepared)
    assert len(statuses) == 1
    assert statuses[0].intact and statuses[0].valid
    assert prepared.read_bytes().startswith(earlier.read_bytes())
    assert [f.name for f in preflight(prepared).empty_fields] == ["SignatureRequest1"]


def test_certified_document_is_refused_and_nothing_is_written(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    certified = certified_pdf(fixture_pdf("text_multipage"), throwaway_pfx(), tmp_path / "c.pdf")
    target = tmp_path / "out.pdf"
    with pytest.raises(SigningRefused):
        prepare_for_signing(certified, target, requests_for(("Ana", 0, BOX_A)), ENGINE)
    assert not target.exists()


def test_encrypted_document_stays_encrypted(fixture_pdf, tmp_path: Path) -> None:
    source = fixture_pdf("encrypted")
    target = tmp_path / "enc.pdf"
    prepare_for_signing(
        source,
        target,
        requests_for(("Ana", 0, BOX_A)),
        ENGINE,
        password="owner",
    )
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(target)
    assert [f.name for f in preflight(target, "owner").empty_fields] == ["SignatureRequest1"]
    assert_round_trip(target, "owner")
    assert target.read_bytes().startswith(source.read_bytes())


def test_missing_source_password_is_refused(fixture_pdf, tmp_path: Path) -> None:
    target = tmp_path / "x.pdf"
    with pytest.raises(SigningError):
        prepare_for_signing(
            fixture_pdf("encrypted"),
            target,
            requests_for(("Ana", 0, BOX_A)),
            ENGINE,
            password=None,
        )
    assert not target.exists()


def test_no_requests_or_same_file_is_refused(fixture_pdf, tmp_path: Path) -> None:
    source = fixture_pdf("text_multipage")
    with pytest.raises(SigningRefused):
        prepare_for_signing(source, tmp_path / "a.pdf", [], ENGINE)
    with pytest.raises(SigningRefused):
        prepare_for_signing(source, source, requests_for(("Ana", 0, BOX_A)), ENGINE)


def test_sign_into_missing_or_signed_field_is_refused(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    prepared = tmp_path / "p.pdf"
    prepare_for_signing(
        fixture_pdf("text_multipage"),
        prepared,
        requests_for(("Ana", 0, BOX_A)),
        ENGINE,
    )
    signed = tmp_path / "s.pdf"
    sign_file(
        prepared,
        signed,
        key_for(throwaway_pfx()),
        SignatureOptions(field_name="SignatureRequest1"),
        ENGINE,
    )
    already = tmp_path / "already.pdf"
    with pytest.raises(SigningRefused, match="already signed"):
        sign_file(
            signed,
            already,
            key_for(throwaway_pfx()),
            SignatureOptions(field_name="SignatureRequest1"),
            ENGINE,
        )
    missing = tmp_path / "missing.pdf"
    with pytest.raises(SigningRefused, match="no signature field"):
        sign_file(
            prepared,
            missing,
            key_for(throwaway_pfx()),
            SignatureOptions(field_name="SignatureRequest9"),
            ENGINE,
        )
    assert not already.exists()
    assert not missing.exists()


def test_sign_into_field_with_a_placement_is_refused(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    prepared = tmp_path / "p.pdf"
    prepare_for_signing(
        fixture_pdf("text_multipage"),
        prepared,
        requests_for(("Ana", 0, BOX_A)),
        ENGINE,
    )
    target = tmp_path / "x.pdf"
    with pytest.raises(SigningRefused):
        sign_file(
            prepared,
            target,
            key_for(throwaway_pfx()),
            SignatureOptions(
                placement=SignaturePlacement(0, BOX_B),
                field_name="SignatureRequest1",
            ),
            ENGINE,
        )
    assert not target.exists()


def test_cancel_while_preparing_writes_no_file(fixture_pdf, tmp_path: Path) -> None:
    token = CancelToken()
    token.cancel()
    target = tmp_path / "cancelled.pdf"
    with pytest.raises(Cancelled):
        prepare_for_signing(
            fixture_pdf("text_multipage"),
            target,
            requests_for(("Ana", 0, BOX_A)),
            ENGINE,
            token=token,
        )
    assert not target.exists()


def test_field_names_carry_no_dots_or_names(fixture_pdf, tmp_path: Path) -> None:
    prepared = tmp_path / "names.pdf"
    prepare_for_signing(
        fixture_pdf("text_multipage"),
        prepared,
        requests_for(("Иван Петров", 0, BOX_A), ("Ana.Alvarez", 0, BOX_B)),
        ENGINE,
    )
    names = [f.name for f in preflight(prepared).empty_fields]
    assert names == ["SignatureRequest1", "SignatureRequest2"]
    assert all(re.fullmatch(r"[A-Za-z0-9]+", name) for name in names)


# -- mailto ------------------------------------------------------------------------------------
def test_mailto_encodes_the_document_name_and_drops_blanks_and_dupes() -> None:
    requests = [
        SignatureRequest("A", " ana@example.com ", 0, BOX_A),
        SignatureRequest("A2", "ANA@example.com", 0, BOX_A),
        SignatureRequest("B", "", 0, BOX_A),
        SignatureRequest("C", "bo@example.com", 1, BOX_B),
    ]
    url = mailto_url(requests, "A&B <x> ?#.pdf")
    assert url.startswith("mailto:ana@example.com,bo@example.com?subject=")
    assert "A%26B%20%3Cx%3E%20%3F%23.pdf" in url
    assert unquote(url.split("subject=")[1].split("&body=")[0]) == "Please sign: A&B <x> ?#.pdf"


def test_mailto_keeps_cyrillic_names_and_is_valid_without_emails() -> None:
    url = mailto_url([SignatureRequest("Иван", "", 0, BOX_A)], "Договор.pdf")
    assert url.startswith("mailto:?subject=")
    assert unquote(url.split("subject=")[1].split("&body=")[0]) == "Please sign: Договор.pdf"
    assert "Иван" not in url  # the names are not in the address line at all


def test_invisible_empty_field_has_no_box_and_signs_without_an_appearance(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    """An empty /Rect from another tool: there's no 0 x 0 appearance to build."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign.fields import SigFieldSpec, append_signature_field

    source = tmp_path / "invisible-field.pdf"
    with fixture_pdf("text_multipage").open("rb") as fh:
        writer = IncrementalPdfFileWriter(fh, strict=False)
        append_signature_field(writer, SigFieldSpec("Hidden"))
        with source.open("wb") as out:
            writer.write(out)
    (field,) = preflight(source).empty_fields
    assert field.rect is None
    target = tmp_path / "signed.pdf"
    key = PfxKey(throwaway_pfx(), "test")
    sign_file(source, target, key, SignatureOptions(field_name="Hidden"), get_engine())
    assert preflight(target).empty_fields == ()


def test_panel_reads_a_prepared_file_signed_out_of_field_order(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    """The whole flow: the second field signed first. Revisions follow signing order, and the
    first signer stays valid when the next one fills their own field."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    from pdfeditor.model.signing import SignatureVerdict
    from pdfeditor.services.signing import inspect_signatures

    prepared = tmp_path / "prepared.pdf"
    requests = [
        SignatureRequest("Ana", "", 0, Rect(50, 50, 300, 110)),
        SignatureRequest("Ben", "", 0, Rect(50, 130, 300, 190)),
    ]
    prepare_for_signing(fixture_pdf("text_multipage"), prepared, requests, ENGINE)
    assert inspect_signatures(prepared, None, []) == []
    pfx = throwaway_pfx()
    key = PfxKey(pfx, "test")
    once = tmp_path / "once.pdf"
    sign_file(prepared, once, key, SignatureOptions(field_name="SignatureRequest2"), ENGINE)
    twice = tmp_path / "twice.pdf"
    sign_file(once, twice, key, SignatureOptions(field_name="SignatureRequest1"), ENGINE)
    _key, cert, _others = pkcs12.load_key_and_certificates(pfx.read_bytes(), b"test")
    assert cert is not None
    root = cert.public_bytes(serialization.Encoding.DER)
    first, second = inspect_signatures(twice, None, [root])
    assert (first.field_name, second.field_name) == ("SignatureRequest2", "SignatureRequest1")
    assert first.modified_after and not second.modified_after
    assert first.verdict is SignatureVerdict.VALID, first.summary
    assert second.verdict is SignatureVerdict.VALID, second.summary
