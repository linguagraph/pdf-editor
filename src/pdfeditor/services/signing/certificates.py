"""Reading X.509 certificates into :class:`CertificateInfo` (asn1crypto, no key access)."""

from __future__ import annotations

from typing import Any

from pdfeditor.model.signing import CertificateInfo
from pdfeditor.services.signing.errors import SigningRefused

_SIGNING_USAGES = {"digital_signature", "non_repudiation"}


def _name(name: Any) -> str:
    fields = name.native
    common = fields.get("common_name") or fields.get("organization_name")
    if isinstance(common, list):
        common = common[0] if common else None
    return str(common) if common else str(name.human_friendly)


def describe(der: bytes, *, provider: str = "", hardware: bool | None = None) -> CertificateInfo:
    """Summarize a DER certificate. Raises ValueError if it can't be parsed."""
    from asn1crypto import x509

    cert = x509.Certificate.load(der)
    validity = cert["tbs_certificate"]["validity"]
    usage = cert.key_usage_value
    usages = set(usage.native) if usage is not None else None
    return CertificateInfo(
        subject=_name(cert.subject),
        issuer=_name(cert.issuer),
        not_before=validity["not_before"].native,
        not_after=validity["not_after"].native,
        thumbprint=cert.sha1.hex().upper(),
        can_sign=usages is None or bool(usages & _SIGNING_USAGES),
        non_repudiation=usages is not None and "non_repudiation" in usages,
        hardware=hardware,
        provider=provider,
        der=der,
    )


def require_usable(info: CertificateInfo) -> None:
    """Refuse an expired, not yet valid or non-signing certificate before any key is touched."""
    problem = info.problem()
    if problem:
        raise SigningRefused(problem)


def chain_for(leaf_der: bytes, others: list[bytes]) -> list[bytes]:
    """Of ``others``, the certificates that issued ``leaf_der``, in order: a token can hold
    unrelated certificates, which have no business in the signature."""
    from asn1crypto import x509

    pool = [x509.Certificate.load(c) for c in others]
    current = x509.Certificate.load(leaf_der)
    chain: list[bytes] = []
    seen = {current.dump()}
    while True:
        issuer = next(
            (c for c in pool if c.subject.dump() == current.issuer.dump() and c.dump() not in seen),
            None,
        )
        if issuer is None:
            return chain
        seen.add(issuer.dump())
        chain.append(issuer.dump())
        current = issuer
