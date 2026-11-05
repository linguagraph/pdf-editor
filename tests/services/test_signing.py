"""PAdES signing, end to end with throwaway PFX keys: sign -> save -> reopen -> validate.

Every result must validate in pyHanko, open in pikepdf and render in pypdfium2, not just MuPDF.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import http.server
import io
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageChops, ImageStat

from pdfeditor.core.jobs import Cancelled, CancelToken
from pdfeditor.engine.base import ColorMode, RenderRequest
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.model.signing import (
    PageBoxes,
    PfxKey,
    SignatureOptions,
    SignaturePlacement,
)
from pdfeditor.services.signing import (
    SigningError,
    SigningRefused,
    TimestampFailed,
    WrongPassword,
    pfx_certificate,
    preflight,
    sign_file,
)

ENGINE = get_engine()
FIXED_TIME = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
REAL_SIGNED = Path(__file__).parent.parent / "fixtures" / "real" / "signed_selfsigned.pdf"
REAL_FW9 = Path(__file__).parent.parent / "fixtures" / "real" / "irs_fw9.pdf"


def key_for(pfx: Path) -> PfxKey:
    return PfxKey(pfx, "test")


def assert_round_trip(path: Path, password: str | None = None) -> None:
    """The independent implementations can read the result."""
    with pikepdf.open(path, password=password or "") as pdf:
        assert len(pdf.pages) >= 1
    doc = pdfium.PdfDocument(str(path), password=password)
    try:
        assert len(doc) >= 1
        doc[0].render(scale=0.5).to_pil()
    finally:
        doc.close()
    reopened = ENGINE.open(path, password)
    try:
        assert reopened.info().has_signatures
    finally:
        reopened.close()


def assert_valid(statuses: list, count: int = 1) -> None:
    assert len(statuses) == count
    for status in statuses:
        assert status.intact and status.valid
        assert status.docmdp_ok is not False


def render_rgb(path: Path, page: int, password: str | None = None) -> Image.Image:
    doc = ENGINE.open(path, password)
    try:
        shot = doc.page(page).render(RenderRequest(matrix=Matrix.scale(2), color=ColorMode.RGB))
    finally:
        doc.close()
    return Image.frombytes(
        "RGB", (shot.width, shot.height), shot.samples, "raw", "RGB", shot.stride
    )


def render_pdfium(path: Path, page: int) -> Image.Image:
    doc = pdfium.PdfDocument(str(path))
    try:
        doc.init_forms()  # draws the widgets, signature appearances among them
        return doc[page].render(scale=2).to_pil().convert("RGB")
    finally:
        doc.close()


def changed_box(before: Image.Image, after: Image.Image) -> tuple[float, ...] | None:
    """Where the page changed, in visible points (the images are at 2x)."""
    # Differences of a level or two are anti-aliasing noise, not drawing.
    box = ImageChops.difference(before, after).point(lambda v: 255 if v > 16 else 0).getbbox()
    return None if box is None else tuple(v / 2 for v in box)


def cropped_rotated_pdf(tmp_path: Path, rotate: int) -> Path:
    """A page with a MediaBox off the origin, a smaller CropBox, and /Rotate on the Pages node
    (so rotation is inherited, not on the page)."""
    path = tmp_path / f"crop{rotate}.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(600, 800))
        page = pdf.pages[0]
        page.obj["/MediaBox"] = pikepdf.Array([10, 20, 610, 820])
        page.obj["/CropBox"] = pikepdf.Array([50, 60, 500, 700])
        pdf.Root.Pages["/Rotate"] = rotate
        pdf.save(path)
    return path


# -- coordinates -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rotate", "expected"),
    [
        (0, (110, 620, 210, 660)),  # x from the crop's left, y down from its top
        (90, (90, 120, 130, 220)),  # the page turned clockwise: visible y runs along user x
        (180, (390, 100, 490, 140)),
        (270, (470, 540, 510, 640)),
    ],
)
def test_visible_to_user_space(rotate: int, expected: tuple[float, ...]) -> None:
    boxes = PageBoxes(media=(0, 0, 612, 792), crop=(50, 60, 550, 700), rotate=rotate)
    assert boxes.visible_to_user(Rect(60, 40, 160, 80)) == pytest.approx(expected)


def test_visible_to_user_space_user_unit_and_bad_crop() -> None:
    boxes = PageBoxes(media=(0, 0, 100, 100), rotate=0, user_unit=2.0)
    assert boxes.visible_to_user(Rect(20, 20, 40, 40)) == pytest.approx((10, 80, 20, 90))
    # a CropBox outside the MediaBox: viewers fall back on the MediaBox
    outside = PageBoxes(media=(0, 0, 100, 100), crop=(200, 200, 300, 300))
    assert outside.visible_box == (0, 0, 100, 100)
    assert PageBoxes(media=(100, 100, 0, 0)).visible_box == (0, 0, 100, 100)


# -- signing ---------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_invisible_signature_round_trip(
    kind: str, fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path: Path
) -> None:
    source = fixture_pdf("text_multipage")
    target = tmp_path / "out.pdf"
    result = sign_file(
        source,
        target,
        key_for(throwaway_pfx(kind)),
        SignatureOptions(reason="Approved", location="Sofia"),
        ENGINE,
    )
    assert result.signer == "Elanero Test Signer" and not result.timestamped
    statuses = signature_status(target)
    assert_valid(statuses)
    cms_check(target)
    assert statuses[0].coverage.name == "ENTIRE_FILE"
    assert statuses[0].md_algorithm == "sha256"
    # an incremental update: the original bytes are untouched at the start of the file
    assert target.read_bytes().startswith(source.read_bytes())
    assert_round_trip(target)
    with pikepdf.open(target) as pdf:
        field = pdf.Root.AcroForm.Fields[0]
        assert str(field.T) == "ElaneroSignature1"
        assert str(field.V.SubFilter) == "/ETSI.CAdES.detached"
        assert str(field.V.Reason) == "Approved" and str(field.V.Location) == "Sofia"
        assert int(pdf.Root.AcroForm.SigFlags) == 3
    # nothing draws on the page
    assert changed_box(render_rgb(source, 0), render_rgb(target, 0)) is None


@pytest.mark.parametrize("page", [0, 1, 2, 3, 4])
def test_visible_signature_on_rotated_and_cropped_pages(
    page: int, fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path
) -> None:
    """rotated_pages has /Rotate 0, 90, 180, 270 and a cropped landscape page."""
    source = fixture_pdf("rotated_pages")
    target = tmp_path / "out.pdf"
    rect = Rect(60, 120, 300, 180)
    sign_file(
        source,
        target,
        key_for(throwaway_pfx()),
        SignatureOptions(SignaturePlacement(page, rect), signed_at=FIXED_TIME),
        ENGINE,
    )
    assert_valid(signature_status(target))
    assert_round_trip(target)
    box = changed_box(render_rgb(source, page), render_rgb(target, page))
    assert box is not None, "the appearance didn't draw"
    assert box[0] >= rect.x0 - 1 and box[1] >= rect.y0 - 1
    assert box[2] <= rect.x1 + 1 and box[3] <= rect.y1 + 1
    assert box[2] - box[0] > rect.width / 2  # the name and details, not just a speck
    # the same place in an independent renderer
    box = changed_box(render_pdfium(source, page), render_pdfium(target, page))
    assert box is not None and box[0] >= rect.x0 - 2 and box[3] <= rect.y1 + 2


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_visible_signature_with_offset_boxes_and_inherited_rotation(
    rotate: int, throwaway_pfx, signature_status, tmp_path: Path
) -> None:
    source = cropped_rotated_pdf(tmp_path, rotate)
    target = tmp_path / "out.pdf"
    rect = Rect(30, 40, 230, 100)
    sign_file(
        source,
        target,
        key_for(throwaway_pfx()),
        SignatureOptions(SignaturePlacement(0, rect), signed_at=FIXED_TIME),
        ENGINE,
    )
    assert_valid(signature_status(target))
    box = changed_box(render_rgb(source, 0), render_rgb(target, 0))
    assert box is not None
    # the text and logo sit inside the box; no border is drawn at its edges
    assert box[0] >= rect.x0 - 1.5 and box[1] >= rect.y0 - 1.5
    assert box[2] <= rect.x1 + 1.5 and box[3] <= rect.y1 + 1.5
    assert box[2] - box[0] > rect.width / 2


def test_appearance_reads_upright_whatever_the_page_rotation(throwaway_pfx, tmp_path: Path) -> None:
    """Same box, same text: the pixels must match the signature on an unrotated page."""
    pfx = throwaway_pfx()
    rect = Rect(30, 40, 230, 100)
    crops = {}
    for rotate in (0, 90, 180, 270):
        source = cropped_rotated_pdf(tmp_path, rotate)
        target = tmp_path / f"signed{rotate}.pdf"
        options = SignatureOptions(SignaturePlacement(0, rect), signed_at=FIXED_TIME)
        sign_file(source, target, key_for(pfx), options, ENGINE)
        image = render_rgb(target, 0).convert("L")
        crops[rotate] = image.crop((2 * rect.x0, 2 * rect.y0, 2 * rect.x1, 2 * rect.y1))
    for rotate in (90, 180, 270):
        difference = ImageChops.difference(crops[0], crops[rotate])
        mean = ImageStat.Stat(difference).mean[0]
        assert mean < 6, f"rotation {rotate} differs by {mean:.1f}"


def test_appearance_shows_non_latin_names(throwaway_pfx, signature_status, tmp_path, fixture_pdf):
    """Signers' names come in every script: the appearance is typeset by the engine."""
    source = fixture_pdf("text_multipage")
    target = tmp_path / "out.pdf"
    sign_file(
        source,
        target,
        key_for(throwaway_pfx(name="Иван Петров 山田")),
        SignatureOptions(SignaturePlacement(0, Rect(50, 50, 400, 130)), reason="Одобрено"),
        ENGINE,
    )
    assert_valid(signature_status(target))
    from pdfeditor.services.signing._pdf import SignatureAppearance, appearance_pdf

    appearance = SignatureAppearance(
        "Иван Петров 山田", ("Digitally signed by Иван Петров 山田", "Reason: Одобрено")
    )
    page = appearance_pdf(ENGINE, 350, 80, appearance)
    doc = ENGINE.open(page)
    try:
        text = doc.page(0).text_page(with_chars=False).text
    finally:
        doc.close()
    assert "Иван Петров" in text and "山田" in text and "Одобрено" in text


def test_second_signature_keeps_the_first_valid(
    fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path: Path
) -> None:
    first, second = tmp_path / "one.pdf", tmp_path / "two.pdf"
    sign_file(
        fixture_pdf("rotated_pages"), first, key_for(throwaway_pfx()), SignatureOptions(), ENGINE
    )
    sign_file(
        first,
        second,
        key_for(throwaway_pfx("ec", name="Second Signer")),
        SignatureOptions(SignaturePlacement(1, Rect(50, 50, 300, 110))),
        ENGINE,
    )
    statuses = signature_status(second)
    assert_valid(statuses, 2)
    assert statuses[0].coverage.name == "ENTIRE_REVISION"
    assert statuses[1].coverage.name == "ENTIRE_FILE"
    assert second.read_bytes().startswith(first.read_bytes())
    assert_round_trip(second)
    cms_check(second)
    with pikepdf.open(second) as pdf:
        names = [str(f.T) for f in pdf.Root.AcroForm.Fields]
    assert names == ["ElaneroSignature1", "ElaneroSignature2"]


def test_signs_a_file_signed_by_another_tool(throwaway_pfx, signature_status, tmp_path) -> None:
    target = tmp_path / "out.pdf"
    sign_file(REAL_SIGNED, target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert_valid(signature_status(target), 2)
    assert_round_trip(target)


def test_independent_check_catches_a_changed_byte(fixture_pdf, throwaway_pfx, cms_check, tmp_path):
    """The check that doesn't rely on pyHanko must itself be able to fail."""
    target = tmp_path / "o.pdf"
    sign_file(fixture_pdf("images"), target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    raw = bytearray(target.read_bytes())
    raw[20] ^= 0xFF
    tampered = tmp_path / "tampered.pdf"
    tampered.write_bytes(raw)
    with pytest.raises(AssertionError):
        cms_check(tampered)


def test_hybrid_reference_file(fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path):
    source = fixture_pdf("hybrid_xref")
    target = tmp_path / "o.pdf"
    sign_file(
        source,
        target,
        key_for(throwaway_pfx()),
        SignatureOptions(SignaturePlacement(0, Rect(10, 10, 200, 60))),
        ENGINE,
    )
    assert target.read_bytes().startswith(source.read_bytes())
    assert_valid(signature_status(target))
    assert_round_trip(target)
    cms_check(target)


@pytest.mark.parametrize(("bits", "digest"), [(3072, "sha256"), (4096, "sha256")])
def test_large_rsa_keys_still_use_sha256(
    bits, digest, fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path
) -> None:
    """pyHanko would pick SHA-384 or SHA-512, which old token software refuses after the PIN."""
    target = tmp_path / "o.pdf"
    sign_file(
        fixture_pdf("images"),
        target,
        key_for(throwaway_pfx(f"rsa{bits}")),
        SignatureOptions(),
        ENGINE,
    )
    assert signature_status(target)[0].md_algorithm == digest
    cms_check(target)


def test_document_with_xmp_metadata(throwaway_pfx, signature_status, tmp_path) -> None:
    """pyHanko rewrites XMP through lxml: a dependency only such a file reaches."""
    source = tmp_path / "xmp.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(300, 300))
        with pdf.open_metadata() as meta:
            meta["dc:title"] = "With XMP"
        pdf.save(source)
    target = tmp_path / "o.pdf"
    sign_file(source, target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert_valid(signature_status(target))
    assert_round_trip(target)


def test_signs_a_real_world_form_with_usage_rights(
    throwaway_pfx, signature_status, tmp_path
) -> None:
    """irs_fw9 has XFA, a /Perms /UR3 signature and SigFlags AppendOnly."""
    target = tmp_path / "out.pdf"
    sign_file(REAL_FW9, target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert_valid(signature_status(target))
    assert_round_trip(target)
    assert target.read_bytes().startswith(REAL_FW9.read_bytes())


# -- refusals --------------------------------------------------------------------------------
def test_wrong_pfx_password(fixture_pdf, throwaway_pfx, tmp_path: Path) -> None:
    target = tmp_path / "o.pdf"
    with pytest.raises(WrongPassword):
        sign_file(
            fixture_pdf("text_multipage"),
            target,
            PfxKey(throwaway_pfx(), "wrong"),
            SignatureOptions(),
            ENGINE,
        )
    assert not target.exists()
    with pytest.raises(WrongPassword):
        pfx_certificate(PfxKey(throwaway_pfx(), "wrong"))


def test_not_a_pfx(fixture_pdf, tmp_path: Path) -> None:
    junk = tmp_path / "junk.pfx"
    junk.write_bytes(b"not a certificate")
    with pytest.raises(WrongPassword, match=r"junk.pfx"):
        pfx_certificate(PfxKey(junk, "x"))
    missing = tmp_path / "missing.pfx"
    with pytest.raises(SigningError, match="read the certificate file"):
        pfx_certificate(PfxKey(missing, "x"))


def test_expired_and_non_signing_certificates_are_refused(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    target = tmp_path / "o.pdf"
    source = fixture_pdf("text_multipage")
    with pytest.raises(SigningRefused, match="expired"):
        sign_file(source, target, key_for(throwaway_pfx(days=-5)), SignatureOptions(), ENGINE)
    with pytest.raises(SigningRefused, match="key usage"):
        sign_file(
            source, target, key_for(throwaway_pfx(key_usage="encrypt")), SignatureOptions(), ENGINE
        )
    assert not target.exists()


def certified(source: Path, target: Path, pfx: Path, level) -> None:
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    with source.open("rb") as fh:
        writer = IncrementalPdfFileWriter(fh)
        signer = signers.SimpleSigner.load_pkcs12_data(pfx.read_bytes(), [], b"test")
        out = io.BytesIO()
        signers.sign_pdf(
            writer,
            signers.PdfSignatureMetadata(
                field_name="Author", certify=True, docmdp_permissions=level
            ),
            signer=signer,
            output=out,
        )
    target.write_bytes(out.getvalue())


def test_certification_forbidding_changes_is_respected(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    from pyhanko.sign.fields import MDPPerm

    locked = tmp_path / "locked.pdf"
    certified(fixture_pdf("text_multipage"), locked, throwaway_pfx(), MDPPerm.NO_CHANGES)
    target = tmp_path / "o.pdf"
    with pytest.raises(SigningRefused, match="certified"):
        preflight(locked)
    with pytest.raises(SigningRefused, match="certified"):
        sign_file(locked, target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert not target.exists()


def test_certification_allowing_signatures_stays_valid(
    fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path
) -> None:
    from pyhanko.sign.fields import MDPPerm

    source = tmp_path / "cert.pdf"
    certified(fixture_pdf("text_multipage"), source, throwaway_pfx(), MDPPerm.FILL_FORMS)
    assert preflight(source).certified
    target = tmp_path / "o.pdf"
    sign_file(source, target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert_valid(signature_status(target), 2)
    # A visible one is refused: strict validators reject a new visible field after certifying.
    with pytest.raises(SigningRefused, match="invisible"):
        sign_file(
            source,
            tmp_path / "visible.pdf",
            key_for(throwaway_pfx()),
            SignatureOptions(SignaturePlacement(0, Rect(50, 50, 300, 110))),
            ENGINE,
        )


def locked_by_field_mdp(source: Path, target: Path, pfx: Path) -> None:
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers

    with source.open("rb") as fh:
        writer = IncrementalPdfFileWriter(fh)
        signer = signers.SimpleSigner.load_pkcs12_data(pfx.read_bytes(), [], b"test")
        with target.open("wb") as out:
            signers.PdfSigner(
                signers.PdfSignatureMetadata(field_name="Locker"),
                signer,
                new_field_spec=fields.SigFieldSpec(
                    "Locker", field_mdp_spec=fields.FieldMDPSpec(fields.FieldMDPAction.ALL)
                ),
            ).sign_pdf(writer, output=out)


def test_field_lock_is_refused_before_the_key_is_used(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    locked = tmp_path / "locked.pdf"
    locked_by_field_mdp(fixture_pdf("text_multipage"), locked, throwaway_pfx())
    with pytest.raises(SigningRefused, match="locks this document's form fields"):
        preflight(locked)
    key = key_for(throwaway_pfx())
    with pytest.raises(SigningRefused, match="locks this document's form fields"):
        sign_file(locked, tmp_path / "o.pdf", key, SignatureOptions(), ENGINE)
    assert not (tmp_path / "o.pdf").exists()


def test_same_source_and_target_is_refused(fixture_pdf, throwaway_pfx, tmp_path: Path) -> None:
    path = tmp_path / "a.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), path)
    with pytest.raises(SigningRefused, match="different file name"):
        sign_file(path, path, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)


def test_broken_file_is_a_clear_error(throwaway_pfx, tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.7\nnot really a pdf")
    with pytest.raises(SigningError):
        preflight(junk)
    with pytest.raises(SigningError):
        sign_file(junk, tmp_path / "o.pdf", key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert not (tmp_path / "o.pdf").exists()


def test_parser_errors_while_signing_are_friendly(
    fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    from pdfeditor.services.signing import _pdf

    def trips(*_args, **_kwargs):
        raise KeyError("/Kids")

    monkeypatch.setattr(_pdf, "page_boxes", trips)
    with pytest.raises(SigningError, match="couldn't be made"):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            key_for(throwaway_pfx()),
            SignatureOptions(SignaturePlacement(0, Rect(10, 10, 200, 60))),
            ENGINE,
        )


def test_cancel_during_verification_stops_the_swap(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    token = CancelToken()

    def status(text: str) -> None:
        if text.startswith("Checking"):
            token.cancel()

    target = tmp_path / "o.pdf"
    target.write_bytes(b"keep me")
    with pytest.raises(Cancelled):
        sign_file(
            fixture_pdf("images"),
            target,
            key_for(throwaway_pfx()),
            SignatureOptions(),
            ENGINE,
            token=token,
            status=status,
        )
    assert target.read_bytes() == b"keep me"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_data_reaches_the_disk_before_the_swap(tmp_path: Path, monkeypatch) -> None:
    import os

    from pdfeditor.core import safe_output

    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1]
    )
    target = tmp_path / "t.bin"
    with safe_output.replaced_atomically(target) as tmp:
        tmp.write_bytes(b"data")
    assert calls == ["fsync", "replace"]
    assert target.read_bytes() == b"data"


def test_nothing_needing_no_key_happens_after_login(
    fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    """A wrong timestamp address, or a read-only folder, must not cost a PIN entry."""
    from pdfeditor.services.signing import _pdf

    def no_login(_key):
        raise AssertionError("logged in before finding out signing can't work")

    monkeypatch.setattr(_pdf, "_signer_for", no_login)
    pfx = key_for(throwaway_pfx())
    with pytest.raises(TimestampFailed):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            pfx,
            SignatureOptions(tsa_url="http://127.0.0.1:9/tsa"),
            ENGINE,
        )
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    with pytest.raises(SigningError, match="Couldn't write"):
        sign_file(fixture_pdf("images"), blocker / "o.pdf", pfx, SignatureOptions(), ENGINE)


def test_temp_file_is_writable_before_login(
    fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    from pdfeditor.services.signing import _pdf

    real = _pdf._signer_for
    seen: list[list[Path]] = []

    def checked(key):
        seen.append(list(tmp_path.glob(".o.pdf.*.tmp")))
        return real(key)

    monkeypatch.setattr(_pdf, "_signer_for", checked)
    sign_file(
        fixture_pdf("images"),
        tmp_path / "o.pdf",
        key_for(throwaway_pfx()),
        SignatureOptions(),
        ENGINE,
    )
    assert len(seen) == 1 and len(seen[0]) == 1, "the output is opened before the key is"


def test_failed_verification_leaves_no_file(
    fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    from pdfeditor.services.signing import _pdf

    def refuse(*_args, **_kwargs) -> None:
        raise SigningError("didn't verify")

    monkeypatch.setattr(_pdf, "verify_output", refuse)
    target = tmp_path / "o.pdf"
    existing = b"keep me"
    target.write_bytes(existing)
    with pytest.raises(SigningError):
        sign_file(
            fixture_pdf("images"), target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE
        )
    assert target.read_bytes() == existing
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


def test_verification_notices_a_broken_earlier_signature(
    fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    from pdfeditor.services.signing import _pdf

    first = tmp_path / "one.pdf"
    sign_file(fixture_pdf("images"), first, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    original = _pdf._signature_ok
    seen: list[str] = []

    def second_pass_fails(signature) -> bool:
        seen.append(signature.field_name)
        # after signing, the old signature "fails"; in the source it was fine
        return not (signature.field_name == "ElaneroSignature1" and len(seen) == 1) and original(
            signature
        )

    monkeypatch.setattr(_pdf, "_signature_ok", second_pass_fails)
    target = tmp_path / "two.pdf"
    with pytest.raises(SigningError, match="invalidated"):
        sign_file(first, target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert not target.exists()


# -- cancel ----------------------------------------------------------------------------------
def test_cancel_before_starting_writes_nothing(fixture_pdf, throwaway_pfx, tmp_path: Path) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            key_for(throwaway_pfx()),
            SignatureOptions(),
            ENGINE,
            token=token,
        )
    assert not (tmp_path / "o.pdf").exists()


def test_cancel_while_waiting_for_the_key_writes_nothing(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    """Cancel can't interrupt a token, but what it returned afterwards is thrown away."""
    token = CancelToken()
    statuses: list[str] = []

    def status(text: str) -> None:
        statuses.append(text)
        if "Waiting for your key" in text:
            token.cancel()  # the user pressed Cancel during the PIN prompt

    with pytest.raises(Cancelled):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            key_for(throwaway_pfx()),
            SignatureOptions(),
            ENGINE,
            token=token,
            status=status,
        )
    assert any("Cancel takes effect only after" in s for s in statuses)
    assert not (tmp_path / "o.pdf").exists()
    assert list(tmp_path.glob("*.tmp")) == []


# -- encrypted documents ---------------------------------------------------------------------
def test_encrypted_document_stays_encrypted(
    fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path: Path
) -> None:
    source = fixture_pdf("encrypted")
    assert preflight(source).needs_password
    assert preflight(source, "owner").encrypted
    target = tmp_path / "o.pdf"
    sign_file(
        source,
        target,
        key_for(throwaway_pfx()),
        SignatureOptions(SignaturePlacement(0, Rect(50, 50, 300, 110))),
        ENGINE,
        document_password="owner",
    )
    assert_valid(signature_status(target, "owner"))
    assert_round_trip(target, "owner")
    cms_check(target, "owner")
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(target)
    assert target.read_bytes().startswith(source.read_bytes())


def test_encrypted_document_wrong_or_missing_password(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    source = fixture_pdf("encrypted")
    with pytest.raises(WrongPassword):
        preflight(source, "nope")
    for password in (None, "nope"):
        with pytest.raises(WrongPassword):
            sign_file(
                source,
                tmp_path / "o.pdf",
                key_for(throwaway_pfx()),
                SignatureOptions(),
                ENGINE,
                document_password=password,
            )


def test_permissions_forbidding_signing_are_respected(tmp_path: Path) -> None:
    source = tmp_path / "restricted.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(200, 200))
        pdf.save(
            source,
            encryption=pikepdf.Encryption(
                user="",
                owner="owner",
                allow=pikepdf.Permissions(
                    modify_annotation=False, modify_form=False, modify_other=False
                ),
            ),
        )
    with pytest.raises(SigningRefused, match="security settings"):
        preflight(source)
    assert preflight(source, "owner").encrypted  # the owner may


# -- timestamps ------------------------------------------------------------------------------
@pytest.fixture
def local_tsa(tmp_path: Path, throwaway_pfx) -> Iterator[str]:
    """An RFC 3161 server on localhost: pyHanko's dummy TSA behind a real HTTP socket."""
    from asn1crypto import keys, tsp, x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    from pyhanko.sign.timestamps import DummyTimeStamper

    key, cert, _others = pkcs12.load_key_and_certificates(throwaway_pfx().read_bytes(), b"test")
    assert key is not None and cert is not None
    stamper = DummyTimeStamper(
        x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)),
        keys.PrivateKeyInfo.load(
            key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        ),
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = tsp.TimeStampReq.load(body)
            answer = asyncio.run(stamper.async_request_tsa_response(request)).dump()
            self.send_response(200)
            self.send_header("Content-Type", "application/timestamp-reply")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/tsa"
    server.shutdown()
    server.server_close()


@pytest.fixture
def bad_tsa(tmp_path: Path, throwaway_pfx) -> Iterator:
    """``bad_tsa(mode)``: the address of a local RFC 3161 server that misbehaves in that way."""
    import time

    from asn1crypto import keys, tsp, x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    from pyhanko.sign.timestamps import DummyTimeStamper

    key, cert, _others = pkcs12.load_key_and_certificates(throwaway_pfx().read_bytes(), b"test")
    assert key is not None and cert is not None
    stamper = DummyTimeStamper(
        x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)),
        keys.PrivateKeyInfo.load(
            key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        ),
    )
    state = {"mode": "", "requests": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = tsp.TimeStampReq.load(body)
            mode = state["mode"]
            state["requests"] += 1
            if mode == "wrong_hash_after_first" and state["requests"] > 1:
                mode = "wrong_hash"
            if mode == "wrong_hash":
                imprint = request["message_imprint"]
                request = tsp.TimeStampReq(
                    {
                        "version": 1,
                        "message_imprint": {
                            "hash_algorithm": imprint["hash_algorithm"],
                            "hashed_message": b"\x11" * len(imprint["hashed_message"].native),
                        },
                        "nonce": request["nonce"].native,
                        "cert_req": True,
                    }
                )
            elif mode == "no_nonce":
                request = tsp.TimeStampReq(
                    {"version": 1, "message_imprint": request["message_imprint"], "cert_req": True}
                )
            self.send_response(200)
            if mode == "trickle":
                self.send_header("Content-Length", "100000")
                self.end_headers()
                for _ in range(50):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
                return
            if mode == "not_asn1":
                answer = b"<html>nope</html>"
            elif mode == "too_big":
                answer = b"x" * (5 * 1024 * 1024)
            elif mode == "no_token":
                answer = bytes.fromhex("30053003020100")  # granted, and no token
            else:
                answer = asyncio.run(stamper.async_request_tsa_response(request)).dump()
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def address(mode: str) -> str:
        state["mode"] = mode
        return f"http://127.0.0.1:{server.server_port}/tsa"

    yield address
    server.shutdown()
    server.server_close()


def test_timestamp_from_a_server(
    fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path, local_tsa: str
) -> None:
    target = tmp_path / "o.pdf"
    result = sign_file(
        fixture_pdf("images"),
        target,
        key_for(throwaway_pfx()),
        SignatureOptions(tsa_url=local_tsa),
        ENGINE,
    )
    assert result.timestamped
    (status,) = signature_status(target)
    assert status.intact and status.valid
    assert status.timestamp_validity is not None and status.timestamp_validity.intact
    assert_round_trip(target)


def test_unreachable_timestamp_server_is_a_clear_error(
    fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    target = tmp_path / "o.pdf"
    with pytest.raises(TimestampFailed, match="Couldn't reach the timestamp server"):
        sign_file(
            fixture_pdf("images"),
            target,
            key_for(throwaway_pfx()),
            SignatureOptions(tsa_url="http://127.0.0.1:9/tsa"),  # discard port: nothing listens
            ENGINE,
        )
    assert not target.exists()


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://[bad", "https://", "http://exa mple/", "http://x:port/"]
)
def test_timestamp_address_must_be_http(url, fixture_pdf, throwaway_pfx, tmp_path: Path) -> None:
    with pytest.raises(TimestampFailed, match="web address"):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            key_for(throwaway_pfx()),
            SignatureOptions(tsa_url=url),
            ENGINE,
        )


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("wrong_hash", "for something else"),
        ("no_nonce", "for something else"),
        ("no_token", "didn't answer with a timestamp"),
        ("not_asn1", "didn't answer with a timestamp"),
        ("too_big", "more than"),
    ],
)
def test_bad_timestamp_server_answers_are_refused_before_the_key(
    mode, message, fixture_pdf, throwaway_pfx, tmp_path: Path, bad_tsa, monkeypatch
) -> None:
    from pdfeditor.services.signing import _pdf

    def no_login(_key):
        raise AssertionError("the bad server must be found out before the key is used")

    monkeypatch.setattr(_pdf, "_signer_for", no_login)
    target = tmp_path / "o.pdf"
    with pytest.raises(TimestampFailed, match=message):
        sign_file(
            fixture_pdf("images"),
            target,
            key_for(throwaway_pfx()),
            SignatureOptions(tsa_url=bad_tsa(mode)),
            ENGINE,
        )
    assert not target.exists()


def test_timestamp_failing_only_on_the_real_request_writes_nothing(
    fixture_pdf, throwaway_pfx, tmp_path: Path, bad_tsa
) -> None:
    """The sizing request was honest, the real one not: found out when checking the answer."""
    target = tmp_path / "o.pdf"
    with pytest.raises(TimestampFailed, match="for something else"):
        sign_file(
            fixture_pdf("images"),
            target,
            key_for(throwaway_pfx()),
            SignatureOptions(tsa_url=bad_tsa("wrong_hash_after_first")),
            ENGINE,
        )
    assert not target.exists()


def test_verification_refuses_a_timestamp_that_doesnt_match(
    fixture_pdf, throwaway_pfx, tmp_path: Path, local_tsa: str, monkeypatch
) -> None:
    """Even a token that slips past the request check isn't reported as a success."""
    from pdfeditor.services.signing import _pdf

    original = _pdf._status

    class BadStamp:
        intact, valid = False, True

    class Status:
        def __init__(self, real) -> None:
            self.real = real
            self.timestamp_validity = BadStamp()

        def __getattr__(self, name):
            return getattr(self.real, name)

    monkeypatch.setattr(_pdf, "_status", lambda s: Status(original(s)))
    target = tmp_path / "o.pdf"
    with pytest.raises(TimestampFailed, match="doesn't match"):
        sign_file(
            fixture_pdf("images"),
            target,
            key_for(throwaway_pfx()),
            SignatureOptions(tsa_url=local_tsa),
            ENGINE,
        )
    assert not target.exists()


def test_slow_timestamp_server_hits_the_deadline(
    fixture_pdf, throwaway_pfx, tmp_path: Path, bad_tsa, monkeypatch
) -> None:
    from pdfeditor.services.signing import _pdf

    monkeypatch.setattr(_pdf, "TSA_DEADLINE_SECONDS", 0.2)
    with pytest.raises(TimestampFailed, match="no complete answer"):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            key_for(throwaway_pfx()),
            SignatureOptions(tsa_url=bad_tsa("trickle")),
            ENGINE,
        )


def test_no_timestamp_means_no_network(
    fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    import urllib.request

    def forbidden(*_args, **_kwargs):
        raise AssertionError("signing without a timestamp must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    sign_file(
        fixture_pdf("images"),
        tmp_path / "o.pdf",
        key_for(throwaway_pfx()),
        SignatureOptions(),
        ENGINE,
    )


# -- the look of a visible signature: text on the left and right, a faint logo, no border ----------
def blank_pdf(path: Path) -> Path:
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(612, 792))
        pdf.save(path)
    return path


def appearance_text(page: bytes) -> str:
    doc = ENGINE.open(page)
    try:
        return " ".join(doc.page(0).text_page(with_chars=False).text.split())
    finally:
        doc.close()


def char_box(page: bytes, needle: str) -> tuple[float, float, float, float]:
    """Where the first character of ``needle`` is, (left, bottom, right, top) in PDF points."""
    doc = pdfium.PdfDocument(page)
    try:
        textpage = doc[0].get_textpage()
        return textpage.get_charbox(textpage.get_text_range().index(needle))
    finally:
        doc.close()


def test_logo_mark_is_faded_and_has_no_teal_corners() -> None:
    from pdfeditor.services.signing.appearance_mark import mark

    picture = mark()
    image = Image.open(io.BytesIO(picture.png))
    assert image.mode == "RGBA" and image.height <= 220
    alpha = image.getchannel("A")
    assert 20 < alpha.getextrema()[1] <= round(0.22 * 255) + 1  # visible, but faded
    width = image.width
    for corner in ((0, 0, 8, 8), (width - 8, 0, width, 8)):
        assert alpha.crop(corner).getextrema()[1] == 0  # the page curl and the teal are cut away
    blue = ImageStat.Stat(image.convert("RGB"), mask=alpha)
    red, green, blue_mean = (channel for channel in blue.mean)
    assert blue_mean > green + 20 > red  # the blue E, not the teal or the white page


def test_visible_signature_has_no_border_and_a_faint_blue_logo(
    throwaway_pfx, signature_status, tmp_path: Path
) -> None:
    source = blank_pdf(tmp_path / "blank.pdf")
    target = tmp_path / "out.pdf"
    rect = Rect(100, 300, 420, 380)
    sign_file(
        source,
        target,
        key_for(throwaway_pfx()),
        SignatureOptions(SignaturePlacement(0, rect), signed_at=FIXED_TIME),
        ENGINE,
    )
    assert_valid(signature_status(target))
    assert_round_trip(target)
    x0, y0, x1, y1 = (2 * v for v in (rect.x0, rect.y0, rect.x1, rect.y1))
    for image in (render_rgb(target, 0), render_pdfium(target, 0)):  # both renderers
        for band in (  # the rectangle's edges, 1 to 2 points inside: the old grey border was here
            (x0 + 2, y0 + 2, x1 - 2, y0 + 4),
            (x0 + 2, y1 - 4, x1 - 2, y1 - 2),
            (x0 + 2, y0 + 2, x0 + 4, y1 - 2),
            (x1 - 4, y0 + 2, x1 - 2, y1 - 2),
        ):
            assert image.crop(band).convert("L").getextrema()[0] > 200
        inside = image.crop((x0, y0, x1, y1))
        red, _, blue = inside.split()
        assert ImageChops.subtract(blue, red).getextrema()[1] > 25  # the logo's blue
        assert inside.convert("L").getextrema()[0] < 100  # the dark text
    image = render_rgb(target, 0)
    box = changed_box(render_rgb(source, 0), image)
    assert box is not None and box[0] >= rect.x0 - 1 and box[2] <= rect.x1 + 1
    assert box[1] >= rect.y0 - 1 and box[3] <= rect.y1 + 1


def test_appearance_names_the_signer_and_lists_the_details() -> None:
    from pdfeditor.services.signing._pdf import SignatureAppearance, appearance_pdf

    appearance = SignatureAppearance(
        "Иван Петров 山田",
        ("Digitally signed by Иван Петров 山田", "Date: 2026.01.02 03:04:05 +00'00'", "Reason: x"),
    )
    page = appearance_pdf(ENGINE, 360, 90, appearance)
    text = appearance_text(page)
    assert "Digitally signed by" in text and "Иван Петров" in text and "山田" in text
    # wide box: the name is in the left half, the details in the right half
    assert char_box(page, "Иван")[2] <= char_box(page, "Digitally")[0]
    assert char_box(page, "Digitally")[0] > 360 / 2 - 10


def test_narrow_box_uses_one_column_with_the_name_above_the_details() -> None:
    from pdfeditor.services.signing._pdf import SignatureAppearance, appearance_pdf

    appearance = SignatureAppearance(
        "Иван Петров 山田", ("Digitally signed by Иван Петров 山田", "Date: 2026.01.02")
    )
    page = appearance_pdf(ENGINE, 130, 200, appearance)
    name, details = char_box(page, "Иван"), char_box(page, "Digitally")
    assert name[1] >= details[3] - 1  # the name's bottom is above the details' top
    assert name[0] < 130 / 2 and details[0] < 130 / 2  # both start in the single column
    assert name[3] - name[1] >= details[3] - details[1]  # the name is never smaller


def test_long_names_fit_and_a_tiny_box_is_refused() -> None:
    from pdfeditor.services.signing._pdf import SignatureAppearance, appearance_pdf

    long_name = "Александра-Мария Константиновна Ivanova-Rodriguez van der Berg 山田太郎"
    for width, height in ((360, 90), (120, 200)):
        appearance = SignatureAppearance(
            long_name, (f"Digitally signed by {long_name}", "Reason: " + "very " * 20)
        )
        assert "Digitally signed by" in appearance_text(
            appearance_pdf(ENGINE, width, height, appearance)
        )
    with pytest.raises(SigningError, match="too small"):
        appearance_pdf(
            ENGINE,
            30,
            12,
            SignatureAppearance(
                "Elanero Test Signer",
                ("Digitally signed by Elanero Test Signer", "Date: 2026.01.02 03:04:05 +00'00'"),
            ),
        )


def test_date_shows_the_utc_offset_the_pdf_way() -> None:
    from pdfeditor.services.signing._pdf import _acrobat_date

    plus_two = dt.timezone(dt.timedelta(hours=2))
    assert _acrobat_date(dt.datetime(2026, 11, 5, 21, 25, 22, tzinfo=plus_two)) == (
        "2026.11.05 21:25:22 +02'00'"
    )
    minus = dt.timezone(-dt.timedelta(hours=5, minutes=30))
    assert _acrobat_date(dt.datetime(2026, 11, 5, 13, 55, 22, tzinfo=minus)) == (
        "2026.11.05 13:55:22 -05'30'"
    )


def test_visible_appearance_adds_little_to_the_file(throwaway_pfx, tmp_path: Path) -> None:
    source = blank_pdf(tmp_path / "blank.pdf")
    key = key_for(throwaway_pfx())
    invisible, visible = tmp_path / "invisible.pdf", tmp_path / "visible.pdf"
    sign_file(source, invisible, key, SignatureOptions(signed_at=FIXED_TIME), ENGINE)
    options = SignatureOptions(
        SignaturePlacement(0, Rect(100, 300, 420, 380)), signed_at=FIXED_TIME
    )
    sign_file(source, visible, key, options, ENGINE)
    assert visible.stat().st_size - invisible.stat().st_size < 150_000


def test_mark_fades_out_at_its_edges() -> None:
    """The E's tail runs past the crop: a hard cut or stray pixels would show on the document."""
    from PIL import Image

    from pdfeditor.services.signing.appearance_mark import mark

    alpha = Image.open(io.BytesIO(mark().png)).getchannel("A")
    width, height = alpha.size
    border = [alpha.getpixel((x, y)) for x in range(width) for y in (0, height - 1)]
    border += [alpha.getpixel((x, y)) for y in range(height) for x in (0, width - 1)]
    assert max(border) <= 2
    assert alpha.getextrema()[1] > 30, "the mark itself must still show"
