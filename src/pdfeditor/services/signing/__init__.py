"""Digital signatures: PAdES signing with Windows-store, PKCS#11 and PFX keys.

Importing this package stays cheap, because the main window imports it at startup: pyHanko,
cryptography and pkcs11 load only when a certificate is read or a signature is made.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

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
        SignatureReport,
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
    "extract_revision",
    "inspect_signatures",
    "pfx_certificate",
    "preflight",
    "sign_file",
]


def preflight(source: Path, password: str | None = None) -> Preflight:
    from pdfeditor.services.signing import _pdf

    return _pdf.preflight(source, password)


def inspect_signatures(
    source: Path, password: str | None, trust_roots: list[bytes] | None = None
) -> list[SignatureReport]:
    """Check the signatures already in ``source``, offline. See ``services/signing/inspect``."""
    from pdfeditor.services.signing import inspect

    return inspect.inspect_signatures(source, password, trust_roots)


def extract_revision(source: Path, revision_end: int, target: Path) -> None:
    """Write the file as it stood when a signature was applied (its first bytes)."""
    from pdfeditor.services.signing import inspect

    inspect.extract_revision(source, revision_end, target)


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
