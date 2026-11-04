"""The Windows certificate store route.

SAFETY: this machine may hold real qualified certificates and USB tokens. These tests may read
the store, but they sign only with keys they create themselves, and those keys never enter a
certificate store on disk. A listing test even proves no key call happens at all. Keys are:

* ephemeral: PKCS12_NO_PERSIST_KEY, gone with the process, or
* persisted under a unique throwaway container name (the only way to reach the real
  CryptAcquireCertificatePrivateKey path) and deleted in a ``finally``.
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import POINTER, byref, c_void_p, cast, pointer, wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from asn1crypto import x509
from asn1crypto.algos import DSASignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.serialization import pkcs12

from pdfeditor.core.jobs import Cancelled
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Rect
from pdfeditor.model.signing import SignatureOptions, SignaturePlacement, WindowsStoreKey
from pdfeditor.services.signing import (
    PinLocked,
    SigningError,
    SigningRefused,
    TokenUnavailable,
    WrongPin,
    sign_file,
    windows_store,
)
from pdfeditor.services.signing.windows_store import (
    BCRYPT_PAD_PKCS1,
    CERT_NCRYPT_KEY_SPEC,
    StoreKey,
    _CertContext,
    _Pkcs1PaddingInfo,
    ecdsa_raw_to_der,
    is_hardware_provider,
    raise_for_status,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows CNG")
ENGINE = get_engine()
DIGEST = hashlib.sha256(b"document").digest()


def der_and_key(pfx: Path) -> tuple[bytes, Any]:
    from cryptography.hazmat.primitives.serialization import Encoding

    key, cert, _others = pkcs12.load_key_and_certificates(pfx.read_bytes(), b"test")
    assert cert is not None
    return cert.public_bytes(Encoding.DER), key


def context_for(der: bytes) -> tuple[Any, Any]:
    buffer = ctypes.create_string_buffer(der, len(der))
    context = _CertContext(1, cast(buffer, c_void_p), len(der), None, None)
    return pointer(context), buffer  # the caller keeps the buffer alive


# -- pure logic ------------------------------------------------------------------------------
def test_ecdsa_der_encoding() -> None:
    r = b"\x80" + b"\x01" * 31  # high bit set: DER needs a leading zero
    s = b"\x00" * 31 + b"\x05"  # small value: DER drops the leading zeros
    der = ecdsa_raw_to_der(r + s)
    parsed = DSASignature.load(der)
    assert parsed["r"].native == int.from_bytes(r, "big")
    assert parsed["s"].native == 5
    assert der[0] == 0x30 and der[4] == 0x00  # SEQUENCE, then INTEGER 0x00 0x80...


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Microsoft Smart Card Key Storage Provider", True),
        ("SafeNet Smart Card Key Storage Provider", True),
        ("eToken Base Cryptographic Provider", True),
        ("Microsoft Platform Crypto Provider", True),
        ("Microsoft Software Key Storage Provider", False),
        ("Microsoft Enhanced RSA and AES Cryptographic Provider", False),
        ("Some Vendor Provider", None),
        ("", None),
    ],
)
def test_hardware_provider_detection(name: str, expected: bool | None) -> None:
    assert is_hardware_provider(name) is expected


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (0x8010006B, WrongPin),  # SCARD_W_WRONG_CHV
        (0x80090033, WrongPin),  # NTE_INCORRECT_PASSWORD
        (0x8010006C, PinLocked),  # SCARD_W_CHV_BLOCKED
        (0x8010000C, TokenUnavailable),  # SCARD_E_NO_SMARTCARD: unplugged
        (0x80100069, TokenUnavailable),  # SCARD_W_REMOVED_CARD
        (0x80090016, TokenUnavailable),  # NTE_BAD_KEYSET
        (0x80090036, Cancelled),  # NTE_USER_CANCELLED
        (0x8010006E, Cancelled),  # SCARD_W_CANCELLED_BY_USER
        (0x80090008, SigningError),  # NTE_BAD_ALGID
        (1223, Cancelled),  # ERROR_CANCELLED, bare from GetLastError
        (0x800704C7, Cancelled),  # the same as an HRESULT
        (86, WrongPin),  # ERROR_INVALID_PASSWORD, bare
        (1326, WrongPin),  # ERROR_LOGON_FAILURE, bare
        (0x80100068, TokenUnavailable),  # SCARD_W_RESET_CARD
        (0x12345678, SigningError),
        (ctypes.c_long(0x80090036).value, Cancelled),  # a signed LONG, as ctypes returns it
    ],
)
def test_windows_errors_mean_something(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        raise_for_status(status, "signing")


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (0x8010006F, "wasn't given a PIN"),  # SCARD_W_CARD_NOT_AUTHENTICATED
        (0x80090010, "refused access"),  # NTE_PERM
    ],
)
def test_windows_errors_with_their_own_wording(status: int, message: str) -> None:
    with pytest.raises(SigningError, match=message):
        raise_for_status(status, "signing")


class FakeApi:
    """Stands in for the Win32 calls, so both key paths run anywhere."""

    def __init__(self, sign: Any, key_spec: int = CERT_NCRYPT_KEY_SPEC) -> None:
        self.sign = sign
        self.key_spec = key_spec
        self.calls: list[tuple[Any, ...]] = []
        self.acquired = 0
        self.freed: list[str] = []
        self.hash_set: bytes = b""
        self.hash_alg = 0

    def CryptAcquireCertificatePrivateKey(self, _ctx, flags, parameter, handle, spec, free):
        self.acquired += 1
        self.acquire_flags = flags
        self.acquire_param = parameter
        handle._obj.value = 77
        spec._obj.value = self.key_spec
        free._obj.value = 1
        return 1

    def CryptSetProvParam(self, *args):
        self.calls.append(("CryptSetProvParam", args[1]))
        return 1

    def NCryptSetProperty(self, handle, name, value, size, flags):
        self.calls.append(("NCryptSetProperty", name, value._obj.value, size))
        return 0

    def NCryptSignHash(self, handle, padding_info, data, length, out, out_size, result, flags):
        digest = bytes(data.raw[:length])
        self.flags = flags
        self.padding = (
            cast(padding_info, POINTER(_Pkcs1PaddingInfo)).contents.pszAlgId
            if padding_info is not None
            else None
        )
        signature = self.sign(digest)
        result._obj.value = len(signature)
        if out is not None:
            ctypes.memmove(out, signature, len(signature))
        return 0

    def NCryptFreeObject(self, handle):
        self.freed.append("ncrypt")
        return 0

    def CryptCreateHash(self, _prov, alg, _key, _flags, handle):
        self.hash_alg = alg
        handle._obj.value = 5
        return 1

    def CryptSetHashParam(self, _hash, _param, data, _flags):
        self.hash_set = bytes(data.raw)
        return 1

    def CryptSignHashW(self, _hash, _spec, _desc, _flags, out, size):
        signature = self.sign(self.hash_set)
        size._obj.value = len(signature)
        if out is not None:
            ctypes.memmove(out, signature, len(signature))
        return 1

    def CryptDestroyHash(self, _hash):
        self.freed.append("hash")
        return 1

    def CryptReleaseContext(self, _prov, _flags):
        self.freed.append("capi")
        return 1

    def CertFreeCertificateContext(self, _ctx):
        return 1


def test_cng_rsa_path_with_fakes(throwaway_pfx) -> None:
    der, _key = der_and_key(throwaway_pfx())
    context, keep = context_for(der)
    api = FakeApi(lambda digest: b"S" * 256)
    key = StoreKey(api, context, parent_window=1234)
    assert api.acquired == 0, "nothing touches the key before the signature is due"
    assert key.sign_digest(DIGEST, "sha256") == b"S" * 256
    assert api.acquired == 1
    assert api.padding == "SHA256" and api.flags == BCRYPT_PAD_PKCS1
    # the PIN dialog's parent: for legacy CSPs before acquiring, for CNG on the key
    assert api.calls[0] == ("CryptSetProvParam", 1)
    name, hwnd = api.calls[1][1:3]
    assert name == "HWND Handle" and hwnd == 1234
    assert api.acquire_flags & windows_store.CRYPT_ACQUIRE_WINDOW_HANDLE_FLAG
    assert api.freed == ["ncrypt"]
    del keep


def test_cng_ecdsa_path_with_fakes(throwaway_pfx) -> None:
    der, _key = der_and_key(throwaway_pfx("ec"))
    context, keep = context_for(der)
    raw = bytes(range(1, 65))
    api = FakeApi(lambda digest: raw)
    signature = StoreKey(api, context).sign_digest(DIGEST, "sha256")
    assert api.padding is None and api.flags == 0
    assert DSASignature.load(signature)["r"].native == int.from_bytes(raw[:32], "big")
    assert api.calls == [], "no parent window, no property set"
    del keep


def test_legacy_capi_path_with_fakes(throwaway_pfx) -> None:
    der, _key = der_and_key(throwaway_pfx())
    context, keep = context_for(der)
    api = FakeApi(lambda digest: bytes(range(256)), key_spec=2)  # AT_SIGNATURE
    signature = StoreKey(api, context).sign_digest(DIGEST, "sha256")
    assert signature == bytes(range(256))[::-1], "CAPI signatures come little-endian"
    assert api.hash_alg == 0x800C and api.hash_set == DIGEST
    assert api.freed == ["hash", "capi"]
    del keep


def test_legacy_capi_refuses_ecdsa(throwaway_pfx) -> None:
    der, _key = der_and_key(throwaway_pfx("ec"))
    context, keep = context_for(der)
    api = FakeApi(lambda digest: b"", key_spec=2)
    with pytest.raises(SigningError, match="only sign with RSA"):
        StoreKey(api, context).sign_digest(DIGEST, "sha256")
    assert api.freed == ["capi"]
    del keep


def test_failed_acquire_reports_an_unplugged_token(throwaway_pfx) -> None:
    der, _key = der_and_key(throwaway_pfx())
    context, keep = context_for(der)
    api = FakeApi(lambda digest: b"")
    api.CryptAcquireCertificatePrivateKey = lambda *_a: 0  # type: ignore[method-assign]
    api.last_error = lambda: 0x8010000C  # type: ignore[attr-defined]
    with pytest.raises(TokenUnavailable, match="Plug it in"):
        StoreKey(api, context).sign_digest(DIGEST, "sha256")
    del keep


class SoftStoreKey:
    """What ``open_certificate`` returns, backed by a throwaway software key."""

    def __init__(self, pfx: Path) -> None:
        self.der, self.key = der_and_key(pfx)
        self.signed = 0

    def __enter__(self) -> SoftStoreKey:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def chain(self) -> list[bytes]:
        return []

    def sign_digest(self, digest: bytes, name: str) -> bytes:
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

        self.signed += 1
        assert name == "sha256"
        if isinstance(self.key, ec.EllipticCurvePrivateKey):
            return self.key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        return self.key.sign(digest, padding.PKCS1v15(), Prehashed(hashes.SHA256()))


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_store_route_signs_once_and_validates(
    kind: str, fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path: Path, monkeypatch
) -> None:
    soft = SoftStoreKey(throwaway_pfx(kind))
    monkeypatch.setattr(windows_store, "open_certificate", lambda _t, _w=0: soft)
    target = tmp_path / "o.pdf"
    sign_file(
        fixture_pdf("rotated_pages"),
        target,
        WindowsStoreKey("00" * 20),
        SignatureOptions(SignaturePlacement(1, Rect(50, 50, 300, 110))),
        ENGINE,
    )
    (status,) = signature_status(target)
    assert status.intact and status.valid
    cms_check(target)
    # the size estimate doesn't touch the key: one signature, so at most one PIN prompt
    assert soft.signed == 1


def test_store_route_refuses_an_expired_certificate_before_touching_the_key(
    fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    soft = SoftStoreKey(throwaway_pfx(days=-3))
    monkeypatch.setattr(windows_store, "open_certificate", lambda _t, _w=0: soft)
    with pytest.raises(SigningRefused, match="expired"):
        sign_file(
            fixture_pdf("images"),
            tmp_path / "o.pdf",
            WindowsStoreKey("00" * 20),
            SignatureOptions(),
            ENGINE,
        )
    assert soft.signed == 0


def test_listing_off_windows_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(windows_store, "available", lambda: False)
    assert windows_store.list_certificates() == []


# -- real CNG, with throwaway keys only ------------------------------------------------------
@windows_only
def test_listing_never_touches_a_key(monkeypatch) -> None:
    """Reads the real store, so it must not be able to raise a PIN prompt on a real token."""
    api = windows_store._api()

    def forbidden(*_args: object) -> int:
        raise AssertionError("listing tried to use a private key")

    for name in ("CryptAcquireCertificatePrivateKey", "NCryptSignHash", "CryptSignHashW"):
        monkeypatch.setattr(api, name, forbidden)
    for info in windows_store.list_certificates():
        assert info.can_sign and info.thumbprint and info.not_after


@windows_only
def test_unknown_thumbprint_is_a_clear_error() -> None:
    with pytest.raises(TokenUnavailable, match="no longer in the Windows certificate store"):
        windows_store.open_certificate("00" * 20)


@windows_only
@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_real_cng_signature_with_an_ephemeral_key(kind: str, throwaway_pfx) -> None:
    pfx = throwaway_pfx(kind)
    _der, private = der_and_key(pfx)
    with windows_store.import_ephemeral_pfx(pfx.read_bytes(), "test") as key:
        signature = key.sign_digest(DIGEST, "sha256")
        assert key.chain() == []  # self-signed: nothing above it
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

    if kind == "rsa":
        private.public_key().verify(
            signature, DIGEST, padding.PKCS1v15(), Prehashed(hashes.SHA256())
        )
    else:
        private.public_key().verify(signature, DIGEST, ec.ECDSA(Prehashed(hashes.SHA256())))


@windows_only
@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_real_cng_end_to_end_pdf(
    kind: str, fixture_pdf, throwaway_pfx, signature_status, cms_check, tmp_path: Path, monkeypatch
) -> None:
    pfx = throwaway_pfx(kind, name="ELANERO THROWAWAY TEST")
    monkeypatch.setattr(
        windows_store,
        "open_certificate",
        lambda _t, _w=0: windows_store.import_ephemeral_pfx(pfx.read_bytes(), "test"),
    )
    target = tmp_path / "o.pdf"
    result = sign_file(
        fixture_pdf("images"), target, WindowsStoreKey("00" * 20), SignatureOptions(), ENGINE
    )
    assert result.signer == "ELANERO THROWAWAY TEST"
    (status,) = signature_status(target)
    assert status.intact and status.valid
    assert status.coverage.name == "ENTIRE_FILE"
    cms_check(target)


@contextmanager
def persisted_key(pfx: Path, *, cng: bool) -> Iterator[tuple[Any, Any]]:
    """Import a throwaway key under its unique container name, and delete it again. The
    certificate context stays in a memory store: CurrentUser\\My isn't touched."""
    assert sys.platform == "win32"
    api = windows_store._api()
    data = pfx.read_bytes()
    payload = ctypes.create_string_buffer(data, len(data))
    blob = windows_store._Blob(len(data), cast(payload, c_void_p))
    crypt_user_keyset = 0x1000
    flags = crypt_user_keyset | (windows_store.PKCS12_ALWAYS_CNG_KSP if cng else 0)
    store = api.PFXImportCertStore(byref(blob), "test", flags)
    assert store
    context = api.CertEnumCertificatesInStore(store, None)
    assert context
    size = wintypes.DWORD()
    prop = windows_store.CERT_KEY_PROV_INFO_PROP_ID
    assert api.CertGetCertificateContextProperty(context, prop, None, byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    assert api.CertGetCertificateContextProperty(context, prop, buffer, byref(size))
    info = cast(buffer, POINTER(windows_store._KeyProvInfo)).contents
    container, provider, kind = info.pwszContainerName, info.pwszProvName, info.dwProvType
    assert container.startswith("elanero-test-")
    try:
        yield api, context, container
    finally:
        _delete_key(provider, container, kind)
        api.CertCloseStore(store, 0)


def _delete_key(provider: str, container: str, kind: int) -> None:
    if kind == 0:  # a CNG key
        ncrypt = ctypes.WinDLL("ncrypt")  # type: ignore[attr-defined, unused-ignore]
        ulong = ctypes.c_size_t
        ncrypt.NCryptOpenStorageProvider.argtypes = (
            POINTER(ulong),
            wintypes.LPCWSTR,
            wintypes.DWORD,
        )
        ncrypt.NCryptOpenKey.argtypes = (
            ulong,
            POINTER(ulong),
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        ncrypt.NCryptDeleteKey.argtypes = (ulong, wintypes.DWORD)
        ncrypt.NCryptFreeObject.argtypes = (ulong,)
        prov, key = ulong(), ulong()
        assert ncrypt.NCryptOpenStorageProvider(byref(prov), provider, 0) == 0
        assert ncrypt.NCryptOpenKey(prov, byref(key), container, 0, 0) == 0
        assert ncrypt.NCryptDeleteKey(key, 0) == 0
        ncrypt.NCryptFreeObject(prov)
    else:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
        ulong = ctypes.c_size_t
        advapi.CryptAcquireContextW.argtypes = (
            POINTER(ulong),
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        handle = ulong()
        crypt_deletekeyset = 0x10
        assert advapi.CryptAcquireContextW(
            byref(handle), container, provider, kind, crypt_deletekeyset
        )


def _verify(private: Any, signature: bytes) -> None:
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

    private.public_key().verify(signature, DIGEST, padding.PKCS1v15(), Prehashed(hashes.SHA256()))


@windows_only
def test_real_acquire_with_a_persisted_cng_key(throwaway_pfx) -> None:
    """The exact path a real store certificate takes: CryptAcquireCertificatePrivateKey on a
    key provider's key, then NCryptSignHash."""
    friendly = f"elanero-test-{uuid.uuid4().hex}".encode()
    pfx = throwaway_pfx(name="ELANERO THROWAWAY TEST", friendly=friendly)
    _der, private = der_and_key(pfx)
    with persisted_key(pfx, cng=True) as (api, context, _container):
        key = StoreKey(api, api.CertDuplicateCertificateContext(context))
        assert key._acquire().key_spec == CERT_NCRYPT_KEY_SPEC
        _verify(private, key.sign_digest(DIGEST, "sha256"))
        key.close()
    thumbprint = x509.Certificate.load(der_and_key(pfx)[0]).sha1.hex().upper()
    assert thumbprint not in {c.thumbprint for c in windows_store.list_certificates()}


@windows_only
def test_real_legacy_capi_signature(throwaway_pfx) -> None:
    """A legacy CryptoAPI key, signing through CryptSignHash: the path old token middleware
    takes. Pointed at the Microsoft CSP that can do SHA-256, which shares its containers."""
    friendly = f"elanero-test-{uuid.uuid4().hex}".encode()
    pfx = throwaway_pfx(name="ELANERO THROWAWAY TEST", friendly=friendly)
    _der, private = der_and_key(pfx)
    with persisted_key(pfx, cng=False) as (api, context, container):
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
        crypt32.CertSetCertificateContextProperty.argtypes = (
            windows_store._PCertContext,
            wintypes.DWORD,
            wintypes.DWORD,
            c_void_p,
        )
        info = windows_store._KeyProvInfo(
            container, "Microsoft Enhanced RSA and AES Cryptographic Provider", 24, 0, 0, None, 1
        )
        assert crypt32.CertSetCertificateContextProperty(
            context, windows_store.CERT_KEY_PROV_INFO_PROP_ID, 0, byref(info)
        )
        key = StoreKey(api, api.CertDuplicateCertificateContext(context))
        key.prefer_ncrypt = False
        assert key._acquire().key_spec != CERT_NCRYPT_KEY_SPEC
        _verify(private, key.sign_digest(DIGEST, "sha256"))
        key.close()


def test_api_is_windows_only() -> None:
    if sys.platform == "win32":
        assert isinstance(windows_store._api(), SimpleNamespace)
    else:
        with pytest.raises(SigningError, match="only available on Windows"):
            windows_store._api()
