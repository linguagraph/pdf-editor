"""Signing a PDF with pyHanko: PAdES, always as an incremental update.

Imported only when signing happens: pyHanko and cryptography take a noticeable fraction of a
second to load, which must not be paid at application start.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import logging
import math
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from asn1crypto import tsp, x509
from pyhanko.pdf_utils import generic, misc
from pyhanko.pdf_utils.crypt import AuthStatus, StandardSecurityHandler
from pyhanko.pdf_utils.crypt.permissions import StandardPermissions
from pyhanko.pdf_utils.generic import pdf_name
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.fields import MDPPerm, SigFieldSpec, SigSeedSubFilter, enumerate_sig_fields
from pyhanko.sign.general import SigningError as HankoSigningError
from pyhanko.sign.signers import PdfSignatureMetadata, PdfSigner, Signer, SimpleSigner
from pyhanko.sign.timestamps.api import TimeStamper
from pyhanko.sign.timestamps.common_utils import TimestampRequestError
from pyhanko.sign.validation import read_certification_data, validate_pdf_signature
from pyhanko.sign.validation.errors import SignatureValidationError
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko.stamp.base import BaseStampStyle
from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator.registry import SimpleCertificateStore

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.core.jobs import CancelToken
from pdfeditor.core.safe_output import replaced_atomically
from pdfeditor.engine.base import Engine, EngineError, SaveOptions
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Rect
from pdfeditor.model.objects import ShapeKind, ShapeSpec, TextStyle
from pdfeditor.model.signing import (
    CertificateInfo,
    KeyRef,
    PageBoxes,
    PfxKey,
    Pkcs11Key,
    SignatureOptions,
    SignResult,
    WindowsStoreKey,
)
from pdfeditor.services.signing.certificates import describe, require_usable
from pdfeditor.services.signing.errors import (
    SigningError,
    SigningRefused,
    TimestampFailed,
    WrongPassword,
)

log = logging.getLogger(__name__)

StatusFn = Callable[[str], None]
TSA_TIMEOUT_SECONDS = 30
FIELD_PREFIX = "ElaneroSignature"
# ISO 32000 lets a certified document be signed, but strict validators (pyHanko's default among
# them) don't accept a new visible field after certification, so its signature would look broken.
VISIBLE_AFTER_CERTIFICATION = (
    "This document was certified by its author, so a new visible signature could make that "
    "certification look invalid in some readers. Use Sign Document without placing a box: an "
    "invisible signature is allowed."
)
WAITING_FOR_KEY = (
    "Waiting for your key. If your token asks for a PIN or a touch, answer it now. "
    "Cancel takes effect only after the device responds."
)


# What a malformed file can make pyHanko's parser raise.
_PARSE_ERRORS = (misc.PdfError, ValueError, KeyError, IndexError, TypeError, AttributeError)


@dataclass(frozen=True, slots=True)
class Preflight:
    needs_password: bool = False  # encrypted, and the empty password doesn't open it
    certified: bool = False  # by an author signature that allows further signatures
    signatures: int = 0
    encrypted: bool = False


# -- reading the document ------------------------------------------------------------------------
def _open_writer(fh: BinaryIO) -> IncrementalPdfFileWriter:
    """An incremental writer on the original bytes. Hybrid cross-reference files (written by
    some office suites) and a direct /Encrypt dictionary (written by MuPDF, so by our own Save)
    need the non-strict reader; MuPDF has opened them all along."""
    try:
        writer = IncrementalPdfFileWriter(fh, strict=True)
        if not writer.prev.xrefs.hybrid_xrefs_present:
            return writer
    except _PARSE_ERRORS as exc:
        log.info("the file needs non-strict reading: %s", exc)
    fh.seek(0)
    try:
        return IncrementalPdfFileWriter(fh, strict=False)
    except _PARSE_ERRORS as exc:
        raise SigningError(f"This PDF's structure can't be read for signing: {exc}") from exc


def _authenticate(writer: IncrementalPdfFileWriter, password: str | None) -> bool:
    """Unlock an encrypted file; False if a password is still needed. The update is encrypted
    the same way: an incremental update can't remove encryption."""
    prev = writer.prev
    if not prev.encrypted:
        return True
    if not isinstance(prev.security_handler, StandardSecurityHandler):
        raise SigningRefused(
            "This document is encrypted for specific certificates, which isn't supported for "
            "signing yet."
        )
    result = writer.encrypt(password or "")
    if result.status == AuthStatus.FAILED:
        if password is None:
            return False
        raise WrongPassword("That password doesn't open this document.")
    settings = prev.trailer.raw_get("/Encrypt")
    if not isinstance(settings, generic.IndirectObject):
        # pyHanko's writer tells the settings apart from what it encrypts by reference.
        writer._encrypt = writer.add_object(settings)
    if result.status == AuthStatus.USER and result.permission_flags is not None:
        allowed = (
            StandardPermissions.ALLOW_ANNOTS_FORM_FILLING | StandardPermissions.ALLOW_FORM_FILLING
        )
        if not (result.permission_flags & allowed):
            raise SigningRefused(
                "This document's security settings don't allow signing. Enter its permissions "
                "(owner) password instead, or ask its author for a copy that allows signing."
            )
    return True


def _check_not_locked(reader: PdfFileReader) -> None:
    """A certification (DocMDP) that forbids all changes also forbids signing."""
    message = (
        "This document was certified by its author with a signature that forbids any further "
        "change, including new signatures. Signing it would invalidate that certification."
    )
    data = read_certification_data(reader)
    if data is not None and data.permission == MDPPerm.NO_CHANGES:
        raise SigningRefused(message)
    for signature in reader.embedded_signatures:
        if signature.docmdp_level == MDPPerm.NO_CHANGES:
            raise SigningRefused(message)


def preflight(source: Path, password: str | None = None) -> Preflight:
    """Can this document be signed? Raises if not; asks for nothing, touches no key."""
    try:
        with source.open("rb") as fh:
            writer = _open_writer(fh)
            encrypted = bool(writer.prev.encrypted)
            if not _authenticate(writer, password):
                return Preflight(needs_password=True, encrypted=True)
            _check_not_locked(writer.prev)
            return Preflight(
                certified=read_certification_data(writer.prev) is not None,
                signatures=len(writer.prev.embedded_signatures),
                encrypted=encrypted,
            )
    except OSError as exc:
        raise SigningError(f"Couldn't read {source.name}: {exc}") from exc
    except _PARSE_ERRORS as exc:
        raise SigningError(f"This PDF's structure can't be read for signing: {exc}") from exc


def _inherited(page: Any, key: str) -> Any:
    node = page
    for _level in range(64):
        if key in node:
            return node[key]
        parent = node.get("/Parent")
        if parent is None:
            return None
        node = parent.get_object() if hasattr(parent, "get_object") else parent
    return None


def _box(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    return x0, y0, x1, y1


def page_boxes(reader: PdfFileReader, index: int) -> PageBoxes:
    page_ref, _resources = reader.find_page_for_modification(index)
    page = page_ref.get_object()
    media = _box(_inherited(page, "/MediaBox")) or (0.0, 0.0, 612.0, 792.0)
    rotate = _inherited(page, "/Rotate")
    unit = page.get("/UserUnit")
    return PageBoxes(
        media=media,
        crop=_box(_inherited(page, "/CropBox")),
        rotate=int(rotate or 0) % 360,
        user_unit=float(unit) if unit else 1.0,
    )


def _new_field_name(reader: PdfFileReader) -> str:
    used = {name for name, _value, _ref in enumerate_sig_fields(reader, filled_status=None)}
    n = 1
    while f"{FIELD_PREFIX}{n}" in used:
        n += 1
    return f"{FIELD_PREFIX}{n}"


# -- the appearance of a visible signature -------------------------------------------------------
def _appearance_text(signer: str, options: SignatureOptions, when: datetime) -> str:
    lines = [f"Digitally signed by {signer}", f"Date: {when:%Y-%m-%d %H:%M:%S %z}".strip()]
    if options.reason:
        lines.append(f"Reason: {options.reason}")
    if options.location:
        lines.append(f"Location: {options.location}")
    return "\n".join(lines)


def appearance_pdf(engine: Engine, width: float, height: float, text: str) -> bytes:
    """A page of the signature's size with its text, typeset by the engine.

    pyHanko's own text boxes only cover Latin without extra native libraries, while signers'
    names are in every script; the engine already picks fonts for any text and embeds them.
    The caller imports this page as the appearance's form XObject.
    """
    with ENGINE_LOCK:
        doc = engine.new_document()
        try:
            doc.insert_blank_page(0, width, height)
            page = doc.page(0)
            page.add_shape(
                ShapeSpec(
                    ShapeKind.RECTANGLE,
                    Rect(0.5, 0.5, width - 0.5, height - 0.5),
                    stroke=Color(0.35, 0.35, 0.35),
                    width=0.75,
                )
            )
            margin = min(4.0, height / 8)
            box = Rect(margin, margin, width - margin, height - margin)
            count = text.count("\n") + 1
            size = min(12.0, box.height / (count * 1.25))
            while True:
                try:
                    page.add_text(box, text, TextStyle(size=size))
                    break
                except EngineError:  # lines wrapped, so it doesn't fit: use a smaller size
                    if size < 3.0:
                        raise SigningError(
                            "The signature rectangle is too small for its text. Draw a larger one."
                        ) from None
                    size *= 0.85
            return doc.to_bytes(SaveOptions())
        finally:
            doc.close()


# Rotates the appearance against the page's /Rotate, so it reads upright on screen.
_ROTATE_MATRIX = {
    90: (0, 1, -1, 0, 0, 0),
    180: (-1, 0, 0, -1, 0, 0),
    270: (0, -1, 1, 0, 0, 0),
}


class _PageStamp:
    def __init__(self, writer: Any, style: _PageStampStyle) -> None:
        self.writer = writer
        self.style = style

    def apply_appearance(self, annot: Any) -> None:
        form = self.writer.import_page_as_xobject(
            PdfFileReader(io.BytesIO(self.style.pdf), strict=False)
        )
        matrix = _ROTATE_MATRIX.get(self.style.rotate)
        if matrix is not None:
            form.get_object()[pdf_name("/Matrix")] = generic.ArrayObject(
                generic.NumberObject(v) for v in matrix
            )
        annot[pdf_name("/AP")] = generic.DictionaryObject({pdf_name("/N"): form})


@dataclass(frozen=True)
class _PageStampStyle(BaseStampStyle):
    """A signature appearance that is a ready-made PDF page, kept upright on rotated pages."""

    pdf: bytes = field(default=b"", repr=False)
    rotate: int = 0

    def create_stamp(self, writer: Any, box: Any, text_params: dict[str, Any]) -> Any:
        return _PageStamp(writer, self)


# -- keys ----------------------------------------------------------------------------------------
class DigestSigner(Signer):
    """A pyHanko signer over a function that signs a digest, for keys held by Windows."""

    def __init__(
        self, cert_der: bytes, chain: list[bytes], sign_digest: Callable[[bytes, str], bytes]
    ) -> None:
        cert = x509.Certificate.load(cert_der)
        if cert.public_key.algorithm not in ("rsa", "ec"):
            raise SigningRefused("Only RSA and ECDSA certificates can sign in this route.")
        registry = SimpleCertificateStore()
        registry.register_multiple([x509.Certificate.load(c) for c in chain])
        super().__init__(signing_cert=cert, cert_registry=registry)
        self._sign_digest = sign_digest

    async def async_sign_raw(
        self, data: bytes, digest_algorithm: str, dry_run: bool = False
    ) -> bytes:
        if dry_run:  # pyHanko sizes the signature container first: the key isn't involved
            return bytes(self._size_estimate())
        name = digest_algorithm.lower()
        return self._sign_digest(hashlib.new(name, data).digest(), name)

    def _size_estimate(self) -> int:
        cert = self.signing_cert
        assert cert is not None
        bits = cert.public_key.bit_size
        if cert.public_key.algorithm == "rsa":
            return math.ceil(bits / 8)
        return 2 * (math.ceil(bits / 8) + 3) + 4  # DER SEQUENCE of two INTEGERs


def _pfx_signer(key: PfxKey) -> SimpleSigner:
    try:
        data = key.path.read_bytes()
    except OSError as exc:
        raise SigningError(f"Couldn't read the certificate file: {exc}") from exc
    try:
        return SimpleSigner.load_pkcs12_data(data, [], passphrase=key.password.encode() or None)
    except (ValueError, TypeError, AttributeError) as exc:
        raise WrongPassword(
            f"“{key.path.name}” can't be opened: the password is wrong, or it isn't a PFX/P12 "
            "file with a private key."
        ) from exc


def pfx_certificate(key: PfxKey) -> CertificateInfo:
    cert = _pfx_signer(key).signing_cert
    assert cert is not None
    return describe(cert.dump())


@contextmanager
def _signer_for(key: KeyRef) -> Iterator[Signer]:
    if isinstance(key, PfxKey):
        yield _pfx_signer(key)
    elif isinstance(key, WindowsStoreKey):
        from pdfeditor.services.signing import windows_store

        with windows_store.open_certificate(key.thumbprint, key.parent_window) as store_key:
            require_usable(describe(store_key.der))
            yield DigestSigner(store_key.der, store_key.chain(), store_key.sign_digest)
    elif isinstance(key, Pkcs11Key):
        from pdfeditor.services.signing.tokens import token_signer

        with token_signer(key) as signer:
            yield signer
    else:
        raise SigningError(f"Unknown key type: {type(key).__name__}")


def _before_key_use(signer: Signer, hook: Callable[[], None]) -> None:
    """Run ``hook`` right before the real key operation (not pyHanko's sizing dry run)."""
    original = signer.async_sign_raw

    async def wrapped(data: bytes, digest_algorithm: str, dry_run: bool = False) -> bytes:
        if not dry_run:
            hook()
        return await original(data, digest_algorithm, dry_run=dry_run)

    signer.async_sign_raw = wrapped  # type: ignore[method-assign]


# -- timestamps ----------------------------------------------------------------------------------
class HttpTimeStamper(TimeStamper):
    """RFC 3161 over HTTP with the standard library: it honors the system's proxy settings and
    gives one clear error, and only the signature's hash is ever sent."""

    def __init__(self, url: str) -> None:
        super().__init__()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise TimestampFailed(f"“{url}” isn't a web address of a timestamp server.")
        self.url = url

    async def async_request_tsa_response(self, req: tsp.TimeStampReq) -> tsp.TimeStampResp:
        request = urllib.request.Request(
            self.url,
            data=req.dump(),
            headers={"Content-Type": "application/timestamp-query"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TSA_TIMEOUT_SECONDS) as response:
                body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise TimestampFailed(
                f"Couldn't reach the timestamp server {self.url}: {exc}. The document wasn't "
                "signed. Check the address and your connection, or sign without a timestamp."
            ) from exc
        try:
            parsed = tsp.TimeStampResp.load(body)
            parsed["status"]["status"].native  # noqa: B018 - parse it now, while errors are clear
        except ValueError as exc:
            raise TimestampFailed(
                f"The server {self.url} didn't answer with a timestamp. Check its address."
            ) from exc
        return parsed


# -- signing and checking the result -------------------------------------------------------------
def _status(signature: Any) -> Any:
    """The signature's validation status, or None if it can't even be evaluated. Whether its
    certificate is trusted is the reader's question: trust isn't what's being checked here."""
    try:
        context = ValidationContext(trust_roots=[signature.signer_cert])
        return validate_pdf_signature(signature, context)
    except (SignatureValidationError, ValueError, KeyError) as exc:
        log.info("a signature couldn't be validated: %s", exc)
        return None


def _signature_ok(signature: Any) -> bool:
    """Is it mathematically sound and not broken by later changes?"""
    status = _status(signature)
    return status is not None and bool(
        status.intact and status.valid and status.docmdp_ok is not False
    )


def _open_reader(fh: BinaryIO, password: str | None) -> PdfFileReader:
    reader = PdfFileReader(fh, strict=False)
    if reader.encrypted:
        reader.decrypt(password or "")
    return reader


def verify_output(path: Path, source: Path, password: str | None, engine: Engine) -> None:
    """Refuse to deliver anything but a correct signature: the new one must cover the whole
    file, and signatures that held before must still hold."""
    with path.open("rb") as fh:
        signatures = list(_open_reader(fh, password).embedded_signatures)
        status = _status(signatures[-1]) if signatures else None
        if status is None or not (status.intact and status.valid):
            raise SigningError("The new signature didn't verify. The file was not written.")
        if status.coverage != SignatureCoverageLevel.ENTIRE_FILE:
            raise SigningError("The new signature doesn't cover the whole file.")
        broken = [s.field_name for s in signatures[:-1] if not _signature_ok(s)]
    if broken:
        with source.open("rb") as fh:
            before = _open_reader(fh, password).embedded_signatures
            regressed = [
                s.field_name for s in before if s.field_name in broken and _signature_ok(s)
            ]
        if regressed:
            raise SigningError(
                "Signing would have invalidated the existing signature "
                f"“{regressed[0]}”. The file was not written."
            )
    with ENGINE_LOCK:
        doc = engine.open(path, password)
        try:
            if not doc.info().has_signatures or doc.page_count < 1:
                raise SigningError("The signed file didn't open correctly.")
        finally:
            doc.close()


def sign_file(
    source: Path,
    target: Path,
    key: KeyRef,
    options: SignatureOptions,
    engine: Engine,
    *,
    document_password: str | None = None,
    token: CancelToken | None = None,
    status: StatusFn | None = None,
) -> SignResult:
    """Write ``target``: ``source``'s exact bytes plus an incremental update with the signature.

    Runs on a worker thread and may wait a long time inside the key call for a PIN prompt, so
    the engine lock is held only for the short steps that use the engine.
    """
    token = token or CancelToken()
    report = status or (lambda _text: None)
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise SigningRefused("Choose a different file name: the original is kept as it is.")
    try:
        with source.open("rb") as fh:
            return _sign_open_file(
                fh, source, target, key, options, engine, document_password, token, report
            )
    except HankoSigningError as exc:
        raise SigningError(f"The signature couldn't be made: {exc}") from exc
    except misc.PdfError as exc:
        raise SigningError(f"This PDF can't be signed: {exc}") from exc
    except TimestampRequestError as exc:  # an OSError: keep before the file errors below
        raise TimestampFailed(f"The timestamp server refused the request: {exc}") from exc
    except OSError as exc:
        raise SigningError(f"Couldn't write {target.name}: {exc}") from exc


def _sign_open_file(
    fh: BinaryIO,
    source: Path,
    target: Path,
    key: KeyRef,
    options: SignatureOptions,
    engine: Engine,
    password: str | None,
    token: CancelToken,
    report: StatusFn,
) -> SignResult:
    report("Preparing the signature…")
    writer = _open_writer(fh)
    if not _authenticate(writer, password):
        raise WrongPassword("This document needs its password to be signed.")
    reader = writer.prev
    _check_not_locked(reader)
    if options.placement is not None and read_certification_data(reader) is not None:
        raise SigningRefused(VISIBLE_AFTER_CERTIFICATION)
    token.check()
    timestamper = HttpTimeStamper(options.tsa_url) if options.tsa_url else None
    with _signer_for(key) as signer:
        cert = signer.signing_cert
        assert cert is not None
        info = describe(cert.dump())
        require_usable(info)

        def about_to_use_key() -> None:
            token.check()
            report(WAITING_FOR_KEY)

        _before_key_use(signer, about_to_use_key)
        field_name = _new_field_name(reader)
        spec = SigFieldSpec(field_name, on_page=0)
        style: BaseStampStyle | None = None
        if options.placement is not None:
            boxes = page_boxes(reader, options.placement.page)
            rect = options.placement.rect
            spec = SigFieldSpec(
                field_name,
                on_page=options.placement.page,
                box=boxes.visible_to_user(rect),  # type: ignore[arg-type]  # floats work
            )
            when = options.signed_at or datetime.now().astimezone()
            text = _appearance_text(info.subject, options, when)
            style = _PageStampStyle(
                pdf=appearance_pdf(engine, rect.width, rect.height, text), rotate=boxes.rotate
            )
        pdf_signer = PdfSigner(
            PdfSignatureMetadata(
                field_name=field_name,
                reason=options.reason or None,
                location=options.location or None,
                subfilter=SigSeedSubFilter.PADES,
            ),
            signer=signer,
            timestamper=timestamper,
            stamp_style=style,
            new_field_spec=spec,
        )
        with replaced_atomically(target) as tmp:
            with tmp.open("wb") as out:
                pdf_signer.sign_pdf(writer, output=out)
            token.check()
            report("Checking the signature…")
            verify_output(tmp, source, password, engine)
    return SignResult(target, info.subject, timestamper is not None)
