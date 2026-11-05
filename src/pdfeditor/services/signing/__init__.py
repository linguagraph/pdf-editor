"""Digital signatures: PAdES signing with Windows-store, PKCS#11 and PFX keys.

Importing this package stays cheap, because the main window imports it at startup: pyHanko,
cryptography and pkcs11 load only when a certificate is read or a signature is made.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from pdfeditor.services.signing.errors import (
    PinFinalTry,
    PinLocked,
    SigningError,
    SigningRefused,
    TimestampFailed,
    TokenUnavailable,
    WrongPassword,
    WrongPin,
)

if TYPE_CHECKING:
    from pdfeditor.core.jobs import CancelToken
    from pdfeditor.engine.base import Engine
    from pdfeditor.model.signing import (
        CertificateInfo,
        KeyRef,
        PfxKey,
        SignatureOptions,
        SignatureRequest,
        SignResult,
    )
    from pdfeditor.services.signing._pdf import Preflight, StatusFn

__all__ = [
    "PinFinalTry",
    "PinLocked",
    "SigningError",
    "SigningRefused",
    "TimestampFailed",
    "TokenUnavailable",
    "WrongPassword",
    "WrongPin",
    "mailto_url",
    "pfx_certificate",
    "preflight",
    "prepare_for_signing",
    "sign_file",
]


def preflight(source: Path, password: str | None = None) -> Preflight:
    from pdfeditor.services.signing import _pdf

    return _pdf.preflight(source, password)


def prepare_for_signing(
    source: Path,
    target: Path,
    requests: Sequence[SignatureRequest],
    engine: Engine,
    password: str | None = None,
    token: CancelToken | None = None,
) -> Path:
    """Write ``target`` with one empty, named signature field per request."""
    from pdfeditor.services.signing import request

    return request.prepare_for_signing(source, target, requests, engine, password, token)


def mailto_url(requests: Sequence[SignatureRequest], document_name: str) -> str:
    """An email draft to every signer. The app can't attach the file, so the body asks the
    sender to attach it. Pure text: no PDF library is loaded for this."""
    emails: list[str] = []
    seen: set[str] = set()
    for request in requests:
        address = request.email.strip()
        if address and address.casefold() not in seen:
            seen.add(address.casefold())
            emails.append(address)
    to = ",".join(quote(address, safe="@") for address in emails)
    subject = f"Please sign: {document_name}"
    body = (
        f"Please sign {document_name}.\n\n"
        "Open the attached PDF and sign your field: each signer has a box marked "
        '"Sign here" with their name, in Elanero or Acrobat.\n'
    )
    return f"mailto:{to}?subject={quote(subject, safe='')}&body={quote(body, safe='')}"


def pfx_certificate(key: PfxKey) -> CertificateInfo:
    """The certificate in a PFX file. Raises :class:`WrongPassword` if it can't be opened."""
    from pdfeditor.services.signing import _pdf

    return _pdf.pfx_certificate(key)


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
    from pdfeditor.services.signing import _pdf

    return _pdf.sign_file(
        source,
        target,
        key,
        options,
        engine,
        document_password=document_password,
        token=token,
        status=status,
    )
