"""Digital signing: certificates, key references and placement, as plain data.

Secrets (PIN, password) are excluded from ``repr`` so they can't reach a log or a report by way
of a dataclass being formatted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from pdfeditor.model.geometry import Rect


@dataclass(frozen=True, slots=True)
class CertificateInfo:
    """What the UI shows about a signing certificate. Never holds key material."""

    subject: str  # the common name, else the full subject
    issuer: str
    not_before: datetime
    not_after: datetime
    thumbprint: str  # SHA-1 of the DER certificate, upper-case hex (how Windows finds it)
    can_sign: bool  # key usage allows document signatures
    non_repudiation: bool = False  # the usage qualified signing certificates carry
    hardware: bool | None = None  # token or smart card; None when the provider doesn't tell
    provider: str = ""  # Windows key storage provider, if any
    der: bytes = field(default=b"", repr=False)

    def problem(self, now: datetime | None = None) -> str:
        """Why this certificate can't sign right now ("" if it can)."""
        now = now or datetime.now(UTC)
        if not self.can_sign:
            return "This certificate isn't meant for signing documents (see its key usage)."
        if now > self.not_after:
            return f"This certificate expired on {self.not_after:%Y-%m-%d}."
        if now < self.not_before:
            return f"This certificate isn't valid until {self.not_before:%Y-%m-%d}."
        return ""


class PinState(Enum):
    """What a token says about its PIN counter (PKCS#11 token flags)."""

    OK = "ok"
    COUNT_LOW = "count_low"  # a wrong PIN was entered since the last good one
    FINAL_TRY = "final_try"  # one more wrong PIN locks the token
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class TokenCertificate:
    info: CertificateInfo
    cert_id: bytes  # CKA_ID, shared with the private key
    label: str


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """A PKCS#11 token, read without a PIN unless the user chose to give one."""

    slot_id: int
    label: str
    manufacturer: str
    serial: str
    protected_auth: bool  # the PIN is entered on the device, not in the app
    pin_state: PinState
    certificates: tuple[TokenCertificate, ...] = ()


@dataclass(frozen=True, slots=True)
class WindowsStoreKey:
    """A certificate in CurrentUser\\My; Windows or the token's middleware asks for any PIN."""

    thumbprint: str
    parent_window: int = 0  # HWND for that PIN prompt, so it doesn't open behind the app


@dataclass(frozen=True, slots=True)
class Pkcs11Key:
    module: str  # path of the vendor's PKCS#11 DLL
    slot_id: int
    token_serial: str
    cert_id: bytes  # shared by a certificate's renewals on the same key: doesn't identify it
    certificate: bytes = field(repr=False)  # the chosen certificate (DER), which does
    cert_label: str = ""
    pin: str | None = field(default=None, repr=False)
    protected_auth: bool = False
    allow_final_try: bool = False  # the user confirmed: one more wrong PIN would lock the token

    @property
    def thumbprint(self) -> str:
        return hashlib.sha1(self.certificate, usedforsecurity=False).hexdigest().upper()


@dataclass(frozen=True, slots=True)
class PfxKey:
    path: Path
    password: str = field(default="", repr=False)


KeyRef = WindowsStoreKey | Pkcs11Key | PfxKey


@dataclass(frozen=True, slots=True)
class SignaturePlacement:
    """Where a visible signature goes, in visible page space (see AGENTS.md)."""

    page: int
    rect: Rect


@dataclass(frozen=True, slots=True)
class SignatureOptions:
    placement: SignaturePlacement | None = None  # None: an invisible signature
    reason: str = ""
    location: str = ""
    tsa_url: str = ""  # RFC 3161 server; empty: no timestamp and no network access
    signed_at: datetime | None = None  # shown in the appearance; defaults to now


@dataclass(frozen=True, slots=True)
class SignResult:
    path: Path
    signer: str
    timestamped: bool


@dataclass(frozen=True, slots=True)
class PageBoxes:
    """The page attributes that relate visible space to PDF user space."""

    media: tuple[float, float, float, float]
    crop: tuple[float, float, float, float] | None = None
    rotate: int = 0
    user_unit: float = 1.0

    @property
    def visible_box(self) -> tuple[float, float, float, float]:
        """The CropBox clipped to the MediaBox, as a viewer shows it (an empty intersection
        falls back on the MediaBox)."""
        mx0, my0, mx1, my1 = _normal(self.media)
        if self.crop is None:
            return mx0, my0, mx1, my1
        cx0, cy0, cx1, cy1 = _normal(self.crop)
        x0, y0, x1, y1 = max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1)
        if x1 <= x0 or y1 <= y0:
            return mx0, my0, mx1, my1
        return x0, y0, x1, y1

    def visible_to_user(self, rect: Rect) -> tuple[float, float, float, float]:
        """``rect`` in visible space as (llx, lly, urx, ury) in user space."""
        x0, y0, x1, y1 = self.visible_box
        unit = self.user_unit or 1.0
        rotate = self.rotate % 360

        def convert(vx: float, vy: float) -> tuple[float, float]:
            vx, vy = vx / unit, vy / unit  # visible space is in points, with UserUnit applied
            if rotate == 90:
                return x0 + vy, y0 + vx
            if rotate == 180:
                return x1 - vx, y0 + vy
            if rotate == 270:
                return x1 - vy, y1 - vx
            return x0 + vx, y1 - vy

        ax, ay = convert(rect.x0, rect.y0)
        bx, by = convert(rect.x1, rect.y1)
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)


def _normal(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])
