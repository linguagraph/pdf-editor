"""Checking the signatures already in a file, for the Signatures panel.

Everything here is offline. Validation never fetches a certificate, CRL or OCSP response, so a
result can say what was checked and what wasn't (revocation, in particular). Imported at call
time only, like the rest of the signing code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from asn1crypto import x509 as asn1_x509
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.crypt import AuthStatus
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext

from pdfeditor.core.safe_output import replaced_atomically
from pdfeditor.model.geometry import Rect
from pdfeditor.model.signing import (
    CertificateDetails,
    PageBoxes,
    SignatureReport,
    SignatureVerdict,
)
from pdfeditor.services.signing import windows_store
from pdfeditor.services.signing._pdf import _PARSE_ERRORS, page_boxes
from pdfeditor.services.signing.errors import SigningError

log = logging.getLogger(__name__)

SIGNER_CLOCK = "signer's computer clock"
TRUSTED_TIMESTAMP = "trusted timestamp"
UNCONFIRMED_TIMESTAMP = "unconfirmed timestamp"
PROTECTED_MESSAGE = "Can't check signatures in this protected document."
_UNREADABLE_MESSAGE = "This file's signatures can't be read."
_UNCHANGED_UNTRUSTED = (
    "The document hasn't changed since this signature, but the signer's identity couldn't be "
    "confirmed."
)
_CHANGED_AFTER = "The signed part of the document is unchanged, but it was changed after signing."
_VALID = (
    "The signature is valid: the document hasn't changed since it was signed, and the "
    "signer's identity is trusted."
)
_ADDITIONS = "Only signatures, form data or validation data were added after this signature."
_VALID_WITH_ADDITIONS = f"The signature is valid and the signer's identity is trusted. {_ADDITIONS}"
_UNTRUSTED_WITH_ADDITIONS = (
    f"The signed part hasn't changed, but the signer's identity couldn't be confirmed. {_ADDITIONS}"
)
_UNTRUSTED_TIMESTAMP = (
    "The signer's identity is trusted and the document hasn't changed, but the timestamp "
    "server's identity couldn't be confirmed."
)
# What may follow a signature without casting doubt on it: another signer's signature counts as
# form filling. Anything else (annotations, content) keeps the "changed after signing" verdict.
_ALLOWED_LATER_CHANGES = frozenset({"LTA_UPDATES", "FORM_FILLING"})


def inspect_signatures(
    source: Path, password: str | None, trust_roots: list[bytes] | None = None
) -> list[SignatureReport]:
    """Check every signature in ``source``, in file order.

    ``trust_roots`` are DER certificates that anchor trust. None means the Windows root stores
    (and nothing off Windows). A certificate embedded in the PDF is never an anchor: anyone can
    put one there. Raises :class:`SigningError` when the file can't be read at all; a single bad
    signature gets an INVALID report of its own instead.
    """
    anchors = _anchors(trust_roots)
    checked_at = datetime.now(UTC)
    with source.open("rb") as fh:
        reader = _open(fh, password)
        try:
            signatures = list(reader.embedded_signatures)
        except _PARSE_ERRORS as exc:  # the signature dictionaries themselves are damaged
            raise SigningError(_UNREADABLE_MESSAGE) from exc
        return [
            _report(reader, number, signature, anchors, checked_at)
            for number, signature in enumerate(signatures, start=1)
        ]


def extract_revision(source: Path, revision_end: int, target: Path) -> None:
    """Write the first ``revision_end`` bytes of ``source``: the file as it was when that
    signature was applied. Written atomically, so ``target`` is never half a file."""
    with replaced_atomically(target) as tmp, source.open("rb") as src, tmp.open("wb") as out:
        out.write(src.read(revision_end))


# -- helpers -------------------------------------------------------------------------------------
def _anchors(trust_roots: list[bytes] | None) -> list[asn1_x509.Certificate]:
    ders = windows_store.root_certificates() if trust_roots is None else trust_roots
    anchors: list[asn1_x509.Certificate] = []
    for der in ders:
        try:
            anchors.append(asn1_x509.Certificate.load(der))
        except ValueError:
            log.info("skipped a trust root that isn't a certificate")
    return anchors


def _open(fh: BinaryIO, password: str | None) -> PdfFileReader:
    try:
        reader = PdfFileReader(fh, strict=False)
    except _PARSE_ERRORS as exc:
        raise SigningError(_UNREADABLE_MESSAGE) from exc
    # The panel never asks for a password; the owner-only case still opens with "".
    if reader.encrypted and reader.decrypt(password or "").status == AuthStatus.FAILED:
        raise SigningError(PROTECTED_MESSAGE)
    return reader


def _resolve(value: Any) -> Any:
    return value.get_object() if isinstance(value, generic.IndirectObject) else value


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    """A value from a damaged or unusual file, or ``default``: a broken detail must not hide
    the verdict."""
    try:
        return fn()
    except Exception as exc:  # pyHanko raises a range of types on malformed input
        log.info("a signature detail couldn't be read: %s", exc)
        return default


def _report(
    reader: PdfFileReader,
    number: int,
    signature: Any,
    anchors: list[asn1_x509.Certificate],
    checked_at: datetime,
) -> SignatureReport:
    details = _describe(reader, signature)
    try:
        status = validate_pdf_signature(
            signature,
            ValidationContext(
                trust_roots=anchors, allow_fetching=False, revocation_mode="soft-fail"
            ),
        )
    except Exception as exc:  # one bad signature must not stop the others
        log.warning("a signature couldn't be checked: %s", exc)
        return _unchecked(
            number, details, checked_at, f"Elanero couldn't check this signature. {exc}"
        )

    stamp = status.timestamp_validity
    modified = status.coverage != SignatureCoverageLevel.ENTIRE_FILE
    trusted = bool(status.trusted and (stamp is None or stamp.trusted))
    verdict, summary, reason = _judge(status, stamp, modified, trusted)
    signing_time: datetime | None = status.signer_reported_dt
    time_source = _time_source(stamp)
    if stamp is not None and time_source != SIGNER_CLOCK:
        signing_time = stamp.timestamp
    return SignatureReport(
        revision=number,
        field_name=details["field_name"],
        signer_name=details["signer_name"],
        signer_email=details["signer_email"],
        verdict=verdict,
        summary=summary,
        intact=bool(status.intact),
        trusted=trusted,
        modified_after=modified,
        coverage=_coverage_text(status),
        signing_time=signing_time,
        time_source=time_source,
        has_ltv_data=details["has_ltv_data"],
        reason=reason,
        location=details["location"],
        certificate=details["certificate"],
        digest_algorithm=str(status.md_algorithm or ""),
        subfilter=details["subfilter"],
        page=details["page"],
        rect=details["rect"],
        invisible=details["invisible"],
        revision_end=details["revision_end"],
        checked_at=checked_at,
    )


def _time_source(stamp: Any) -> str:
    """Where the signing time comes from. "Trusted" only if the server's chain was confirmed."""
    if stamp is None or not (stamp.intact and stamp.valid):
        return SIGNER_CLOCK
    return TRUSTED_TIMESTAMP if stamp.trusted else UNCONFIRMED_TIMESTAMP


def _only_allowed_additions(status: Any) -> bool:
    level = _safe(lambda: status.modification_level, None)
    return level is not None and level.name in _ALLOWED_LATER_CHANGES


def _judge(
    status: Any, stamp: Any, modified: bool, trusted: bool
) -> tuple[SignatureVerdict, str, str]:
    """(verdict, validity sentence, reason). Order matters: the first failing check decides."""
    if not (status.intact and status.valid):
        text = "The signature doesn't match the document, so the signed content may have changed."
        return SignatureVerdict.INVALID, text, text
    if status.docmdp_ok is False:
        text = "The document was changed after signing in a way the signature doesn't allow."
        return SignatureVerdict.INVALID, text, text
    if stamp is not None and not (stamp.intact and stamp.valid):
        text = "The timestamp doesn't match the signature."
        return SignatureVerdict.INVALID, text, text
    allowed = modified and _only_allowed_additions(status)
    if modified and not allowed:
        return (
            SignatureVerdict.UNTRUSTED,
            _CHANGED_AFTER,
            "The document was changed after this signature was applied.",
        )
    if status.trusted and not trusted:
        return SignatureVerdict.UNTRUSTED, _UNTRUSTED_TIMESTAMP, ""
    if not trusted and allowed:
        return (
            SignatureVerdict.UNTRUSTED,
            _UNTRUSTED_WITH_ADDITIONS,
            "The signer's certificate doesn't chain to a trusted root.",
        )
    if not trusted:
        return (
            SignatureVerdict.UNTRUSTED,
            _UNCHANGED_UNTRUSTED,
            "The signer's certificate doesn't chain to a trusted root.",
        )
    return SignatureVerdict.VALID, _VALID_WITH_ADDITIONS if allowed else _VALID, ""


def _coverage_text(status: Any) -> str:
    coverage = status.coverage
    if coverage == SignatureCoverageLevel.ENTIRE_FILE:
        return "The whole file is covered by this signature."
    if _only_allowed_additions(status):
        return _ADDITIONS
    return "Later changes were added after this signature."


def _unchecked(
    number: int, details: dict[str, Any], checked_at: datetime, reason: str
) -> SignatureReport:
    return SignatureReport(
        revision=number,
        field_name=details["field_name"],
        signer_name=details["signer_name"],
        signer_email=details["signer_email"],
        verdict=SignatureVerdict.INVALID,
        summary=reason,
        intact=False,
        trusted=False,
        modified_after=False,
        coverage="",
        signing_time=None,
        time_source=SIGNER_CLOCK,
        has_ltv_data=details["has_ltv_data"],
        reason=reason,
        location=details["location"],
        certificate=details["certificate"],
        digest_algorithm="",
        subfilter=details["subfilter"],
        page=details["page"],
        rect=details["rect"],
        invisible=details["invisible"],
        revision_end=details["revision_end"],
        checked_at=checked_at,
    )


def _describe(reader: PdfFileReader, signature: Any) -> dict[str, Any]:
    """The parts of a report that come from the file's structure, not from validation."""
    sig_object = _resolve(signature.sig_object)
    field = _resolve(signature.sig_field)
    cert = _safe(lambda: signature.signer_cert, None)
    subject = _safe(lambda: cert.subject.native, {}) if cert is not None else {}
    page, rect, invisible = _placement(reader, field)
    name = str(subject.get("common_name") or "")
    if not name and cert is not None:
        name = _safe(lambda: cert.subject.human_friendly, "")
    return {
        "field_name": str(signature.field_name),
        "signer_name": name,
        "signer_email": str(subject.get("email_address") or ""),
        "has_ltv_data": _safe(lambda: reader.root.get("/DSS") is not None, False),
        "location": _safe(lambda: str(_resolve(sig_object.get("/Location", ""))), ""),
        "subfilter": _safe(lambda: str(_resolve(sig_object.get("/SubFilter", ""))).lstrip("/"), ""),
        "certificate": _certificate(cert) if cert is not None else None,
        "revision_end": _safe(
            lambda: int(signature.byte_range[2]) + int(signature.byte_range[3]), 0
        ),
        "page": page,
        "rect": rect,
        "invisible": invisible,
    }


def _certificate(cert: asn1_x509.Certificate) -> CertificateDetails | None:
    return _safe(
        lambda: CertificateDetails(
            subject=cert.subject.human_friendly,
            issuer=cert.issuer.human_friendly,
            serial=f"{cert.serial_number:X}",
            not_before=cert.not_valid_before,
            not_after=cert.not_valid_after,
        ),
        None,
    )


def _placement(reader: PdfFileReader, field: Any) -> tuple[int | None, Rect | None, bool]:
    """(page index, rect in visible page space, invisible?). An invisible signature has an
    empty /Rect and no page."""

    def read() -> tuple[int | None, Rect | None, bool]:
        kids = _resolve(field.get("/Kids"))  # a widget may be a child rather than the field itself
        widget = _resolve(kids[0]) if kids else field
        box = _resolve(widget.get("/Rect"))
        x0, y0, x1, y1 = (float(v) for v in box) if box is not None else (0.0, 0.0, 0.0, 0.0)
        if abs(x1 - x0) <= 0 or abs(y1 - y0) <= 0:
            return None, None, True
        index = _page_index(reader, widget)
        if index is None:
            return None, None, False
        boxes = page_boxes(reader, index)
        ax, ay = _to_visible(boxes, x0, y0)
        bx, by = _to_visible(boxes, x1, y1)
        return index, Rect(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)), False

    return _safe(read, (None, None, False))


def _page_index(reader: PdfFileReader, field: Any) -> int | None:
    """The page whose object is the field's /P (the page the signature appears on)."""
    target = field.get("/P")
    if not isinstance(target, generic.IndirectObject):
        return None
    for index in range(int(reader.root["/Pages"]["/Count"])):
        page_ref, _resources = reader.find_page_for_modification(index)
        if page_ref.idnum == target.idnum:
            return index
    return None


def _to_visible(boxes: PageBoxes, x: float, y: float) -> tuple[float, float]:
    """A user-space point in visible space: the inverse of ``PageBoxes.visible_to_user``."""
    x0, y0, x1, y1 = boxes.visible_box
    unit = boxes.user_unit or 1.0
    rotate = boxes.rotate % 360
    if rotate == 90:
        vx, vy = y - y0, x - x0
    elif rotate == 180:
        vx, vy = x1 - x, y - y0
    elif rotate == 270:
        vx, vy = y1 - y, x1 - x
    else:
        vx, vy = x - x0, y1 - y
    return vx * unit, vy * unit
