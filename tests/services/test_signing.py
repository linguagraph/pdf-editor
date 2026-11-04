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
    box = ImageChops.difference(before, after).getbbox()
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
    kind: str, fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path
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
    assert box[2] - box[0] > rect.width - 4 and box[3] - box[1] > rect.height - 4
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
    assert box == pytest.approx((rect.x0, rect.y0, rect.x1, rect.y1), abs=1.5)


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
    from pdfeditor.services.signing._pdf import appearance_pdf

    page = appearance_pdf(ENGINE, 350, 80, "Digitally signed by Иван Петров 山田\nReason: Одобрено")
    doc = ENGINE.open(page)
    try:
        text = doc.page(0).text_page(with_chars=False).text
    finally:
        doc.close()
    assert "Иван Петров" in text and "山田" in text and "Одобрено" in text


def test_second_signature_keeps_the_first_valid(
    fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path
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
    with pikepdf.open(second) as pdf:
        names = [str(f.T) for f in pdf.Root.AcroForm.Fields]
    assert names == ["ElaneroSignature1", "ElaneroSignature2"]


def test_signs_a_file_signed_by_another_tool(throwaway_pfx, signature_status, tmp_path) -> None:
    target = tmp_path / "out.pdf"
    sign_file(REAL_SIGNED, target, key_for(throwaway_pfx()), SignatureOptions(), ENGINE)
    assert_valid(signature_status(target), 2)
    assert_round_trip(target)


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
    fixture_pdf, throwaway_pfx, signature_status, tmp_path: Path
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


def test_timestamp_address_must_be_http(fixture_pdf, throwaway_pfx, tmp_path: Path) -> None:
    with pytest.raises(TimestampFailed, match="web address"):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            key_for(throwaway_pfx()),
            SignatureOptions(tsa_url="file:///etc/passwd"),
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
