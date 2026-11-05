"""The Windows certificate store: listing certificates and signing with CNG or legacy CAPI.

This is the route most USB tokens and smart cards take: the vendor's minidriver or CSP puts the
certificate in CurrentUser\\My, and the operating system or the middleware shows the PIN prompt
from the thread that makes the key call.

Listing only reads certificates and their stored properties, never the key, so it can't raise a
PIN prompt. ``CryptAcquireCertificatePrivateKey`` runs only for the one certificate the user
chose, at the moment the signature is due.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import POINTER, Structure, byref, c_void_p, c_wchar_p, cast, sizeof, string_at, wintypes
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace, TracebackType
from typing import Any, NoReturn

from pdfeditor.core.jobs import Cancelled
from pdfeditor.model.signing import CertificateInfo
from pdfeditor.services.signing.certificates import describe
from pdfeditor.services.signing.errors import (
    PinLocked,
    SigningError,
    TokenUnavailable,
    WrongPin,
)

log = logging.getLogger(__name__)

X509_ASN_ENCODING = 0x1
PKCS_7_ASN_ENCODING = 0x10000
ENCODING = X509_ASN_ENCODING | PKCS_7_ASN_ENCODING
CERT_STORE_PROV_SYSTEM_W = 10
CERT_SYSTEM_STORE_CURRENT_USER = 0x00010000
CERT_SYSTEM_STORE_LOCAL_MACHINE = 0x00020000
CERT_STORE_READONLY_FLAG = 0x00008000
CERT_KEY_PROV_INFO_PROP_ID = 2
CERT_NCRYPT_KEY_HANDLE_PROP_ID = 78
CERT_FIND_SHA1_HASH = 0x00010000
CRYPT_ACQUIRE_WINDOW_HANDLE_FLAG = 0x80
CRYPT_ACQUIRE_PREFER_NCRYPT_KEY_FLAG = 0x00020000
CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF
BCRYPT_PAD_PKCS1 = 0x2
PP_CLIENT_HWND = 1
HP_HASHVAL = 2
CERT_CHAIN_CACHE_ONLY_URL_RETRIEVAL = 0x4  # never fetch anything while building the chain
CERT_CHAIN_DISABLE_AUTH_ROOT_AUTO_UPDATE = 0x100
PKCS12_NO_PERSIST_KEY = 0x8000
PKCS12_ALWAYS_CNG_KSP = 0x200
NCRYPT_WINDOW_HANDLE_PROPERTY = "HWND Handle"
_CAPI_HASH = {"sha256": 0x800C, "sha384": 0x800D, "sha512": 0x800E}
_CNG_HASH = {"sha256": "SHA256", "sha384": "SHA384", "sha512": "SHA512"}

# Providers that keep the key on disk; anything naming a card or token keeps it on the device.
_SOFTWARE_PROVIDERS = {
    "microsoft software key storage provider",
    "microsoft base cryptographic provider v1.0",
    "microsoft enhanced cryptographic provider v1.0",
    "microsoft strong cryptographic provider",
    "microsoft enhanced rsa and aes cryptographic provider",
    "microsoft base dss and diffie-hellman cryptographic provider",
    "microsoft enhanced dss and diffie-hellman cryptographic provider",
    "microsoft rsa schannel cryptographic provider",
    "microsoft dh schannel cryptographic provider",
}

_USER_CANCELLED = {
    0x80090036,  # NTE_USER_CANCELLED
    0x8010006E,  # SCARD_W_CANCELLED_BY_USER
    0x80100002,  # SCARD_E_CANCELLED
    0x800704C7,  # ERROR_CANCELLED
}
_WRONG_PIN = {
    0x8010006B,  # SCARD_W_WRONG_CHV
    0x80090033,  # NTE_INCORRECT_PASSWORD
    0x8007052B,  # ERROR_WRONG_PASSWORD
    0x8007052E,  # ERROR_LOGON_FAILURE
    0x80070056,  # ERROR_INVALID_PASSWORD
}
_LOCKED = {0x8010006C, 0x80070775}  # SCARD_W_CHV_BLOCKED, ERROR_ACCOUNT_LOCKED_OUT
_NO_PIN = {0x8010006F}  # SCARD_W_CARD_NOT_AUTHENTICATED
_DENIED = {0x80090010, 0x80070005}  # NTE_PERM, E_ACCESSDENIED
_NO_DEVICE = {
    0x80090016,  # NTE_BAD_KEYSET: key container gone, the usual sign of an unplugged token
    0x8009000D,  # NTE_NO_KEY
    0x8009200B,  # CRYPT_E_NO_KEY_PROPERTY
    0x80090030,  # NTE_DEVICE_NOT_READY
    0x80090035,  # NTE_DEVICE_NOT_FOUND
    0x80100030,  # SCARD_E_NO_KEY_CONTAINER
    0x8010000C,  # SCARD_E_NO_SMARTCARD
    0x80100017,  # SCARD_E_READER_UNAVAILABLE
    0x8010002E,  # SCARD_E_NO_READERS_AVAILABLE
    0x8010001D,  # SCARD_E_NO_SERVICE
    0x8010001E,  # SCARD_E_SERVICE_STOPPED
    0x80100069,  # SCARD_W_REMOVED_CARD
    0x80100068,  # SCARD_W_RESET_CARD
    0x80100066,  # SCARD_W_UNRESPONSIVE_CARD
    0x80100067,  # SCARD_W_UNPOWERED_CARD
    0x8010000A,  # SCARD_E_TIMEOUT
}
_BAD_ALGORITHM = {0x80090008, 0x80090029}  # NTE_BAD_ALGID, NTE_NOT_SUPPORTED


class _Blob(Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", c_void_p))


class _CertContext(Structure):
    _fields_ = (
        ("dwCertEncodingType", wintypes.DWORD),
        ("pbCertEncoded", c_void_p),
        ("cbCertEncoded", wintypes.DWORD),
        ("pCertInfo", c_void_p),
        ("hCertStore", c_void_p),
    )


_PCertContext = POINTER(_CertContext)


class _KeyProvInfo(Structure):
    _fields_ = (
        ("pwszContainerName", c_wchar_p),
        ("pwszProvName", c_wchar_p),
        ("dwProvType", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("cProvParam", wintypes.DWORD),
        ("rgProvParam", c_void_p),
        ("dwKeySpec", wintypes.DWORD),
    )


class _Pkcs1PaddingInfo(Structure):
    _fields_ = (("pszAlgId", c_wchar_p),)


class _EnhKeyUsage(Structure):
    _fields_ = (("cUsageIdentifier", wintypes.DWORD), ("rgpszUsageIdentifier", c_void_p))


class _UsageMatch(Structure):
    _fields_ = (("dwType", wintypes.DWORD), ("Usage", _EnhKeyUsage))


class _ChainPara(Structure):
    _fields_ = (("cbSize", wintypes.DWORD), ("RequestedUsage", _UsageMatch))


class _TrustStatus(Structure):
    _fields_ = (("dwErrorStatus", wintypes.DWORD), ("dwInfoStatus", wintypes.DWORD))


# The chain structures continue past these fields; only ever read through pointers from Windows.
class _ChainElement(Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("pCertContext", _PCertContext),
        ("TrustStatus", _TrustStatus),
    )


class _SimpleChain(Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("TrustStatus", _TrustStatus),
        ("cElement", wintypes.DWORD),
        ("rgpElement", POINTER(POINTER(_ChainElement))),
    )


class _ChainContext(Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("TrustStatus", _TrustStatus),
        ("cChain", wintypes.DWORD),
        ("rgpChain", POINTER(POINTER(_SimpleChain))),
    )


def available() -> bool:
    return sys.platform == "win32"


@lru_cache(maxsize=1)
def _api() -> Any:
    """The Win32 calls, with their signatures declared (handles are 64 bits wide)."""
    if sys.platform != "win32":
        raise SigningError("The Windows certificate store is only available on Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    ncrypt = ctypes.WinDLL("ncrypt", use_last_error=True)
    dword, boolean, pdword = wintypes.DWORD, wintypes.BOOL, POINTER(wintypes.DWORD)
    handle, status = ctypes.c_size_t, ctypes.c_long

    def bind(lib: Any, name: str, restype: Any, *argtypes: Any) -> Any:
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
        return fn

    cert = _PCertContext
    return SimpleNamespace(
        CertOpenStore=bind(
            crypt32, "CertOpenStore", c_void_p, c_void_p, dword, handle, dword, c_wchar_p
        ),
        CertCloseStore=bind(crypt32, "CertCloseStore", boolean, c_void_p, dword),
        CertEnumCertificatesInStore=bind(
            crypt32, "CertEnumCertificatesInStore", cert, c_void_p, cert
        ),
        CertFindCertificateInStore=bind(
            crypt32,
            "CertFindCertificateInStore",
            cert,
            c_void_p,
            dword,
            dword,
            dword,
            c_void_p,
            cert,
        ),
        CertDuplicateCertificateContext=bind(
            crypt32, "CertDuplicateCertificateContext", cert, cert
        ),
        CertFreeCertificateContext=bind(crypt32, "CertFreeCertificateContext", boolean, cert),
        CertGetCertificateContextProperty=bind(
            crypt32, "CertGetCertificateContextProperty", boolean, cert, dword, c_void_p, pdword
        ),
        CryptAcquireCertificatePrivateKey=bind(
            crypt32,
            "CryptAcquireCertificatePrivateKey",
            boolean,
            cert,
            dword,
            c_void_p,
            POINTER(handle),
            pdword,
            POINTER(boolean),
        ),
        CertGetCertificateChain=bind(
            crypt32,
            "CertGetCertificateChain",
            boolean,
            c_void_p,
            cert,
            c_void_p,
            c_void_p,
            POINTER(_ChainPara),
            dword,
            c_void_p,
            POINTER(POINTER(_ChainContext)),
        ),
        CertFreeCertificateChain=bind(
            crypt32, "CertFreeCertificateChain", None, POINTER(_ChainContext)
        ),
        PFXImportCertStore=bind(
            crypt32, "PFXImportCertStore", c_void_p, POINTER(_Blob), c_wchar_p, dword
        ),
        NCryptSetProperty=bind(
            ncrypt, "NCryptSetProperty", status, handle, c_wchar_p, c_void_p, dword, dword
        ),
        NCryptSignHash=bind(
            ncrypt,
            "NCryptSignHash",
            status,
            handle,
            c_void_p,
            c_void_p,
            dword,
            c_void_p,
            dword,
            pdword,
            dword,
        ),
        NCryptFreeObject=bind(ncrypt, "NCryptFreeObject", status, handle),
        CryptSetProvParam=bind(
            advapi32, "CryptSetProvParam", boolean, handle, dword, c_void_p, dword
        ),
        CryptCreateHash=bind(
            advapi32, "CryptCreateHash", boolean, handle, dword, handle, dword, POINTER(handle)
        ),
        CryptSetHashParam=bind(
            advapi32, "CryptSetHashParam", boolean, handle, dword, c_void_p, dword
        ),
        CryptSignHashW=bind(
            advapi32, "CryptSignHashW", boolean, handle, dword, c_void_p, dword, c_void_p, pdword
        ),
        CryptDestroyHash=bind(advapi32, "CryptDestroyHash", boolean, handle),
        CryptReleaseContext=bind(advapi32, "CryptReleaseContext", boolean, handle, dword),
        last_error=ctypes.get_last_error,
    )


def is_hardware_provider(name: str) -> bool | None:
    """True for a token, smart card or TPM provider, False for a software key, None if unclear
    (third-party providers don't say)."""
    lowered = name.lower().strip()
    if not lowered:
        return None
    if lowered in _SOFTWARE_PROVIDERS:
        return False
    if any(word in lowered for word in ("smart card", "smartcard", "token", "platform crypto")):
        return True
    return None


def raise_for_status(status: int, doing: str) -> NoReturn:
    """Turn a Windows crypto error into what the user needs to know."""
    code = status & 0xFFFFFFFF
    if code < 0x10000:  # a bare Win32 code from GetLastError: HRESULT_FROM_WIN32
        code |= 0x80070000
    if code in _USER_CANCELLED:
        raise Cancelled()
    if code in _WRONG_PIN:
        raise WrongPin(
            "The token or smart card rejected the PIN. It locks after a few failed attempts, "
            "so check the PIN before trying again."
        )
    if code in _LOCKED:
        raise PinLocked(
            "The PIN of this token or smart card is locked. Unlock it with the tools from its "
            "vendor, then sign again."
        )
    if code in _NO_PIN:
        raise SigningError(
            "The token or smart card wasn't given a PIN, so it didn't sign. Sign again and enter "
            "the PIN when asked."
        )
    if code in _DENIED:
        raise SigningError(
            "The token or key provider refused access to this key. A wrong PIN can cause this "
            "too: tokens lock after a few failures, so check the PIN before trying again."
        )
    if code in _NO_DEVICE:
        raise TokenUnavailable(
            "The token or smart card holding this key isn't available. Plug it in, wait until "
            "its software sees it, and sign again."
        )
    if code in _BAD_ALGORITHM:
        raise SigningError(
            "This key's provider can't make the required signature (SHA-256 or stronger, with "
            f"RSA or ECDSA). Its software may be too old (error 0x{code:08X})."
        )
    raise SigningError(f"Windows couldn't sign while {doing} (error 0x{code:08X}).")


def ecdsa_raw_to_der(raw: bytes) -> bytes:
    """NCrypt returns ECDSA as r||s; CMS wants a DER SEQUENCE of two INTEGERs."""
    from asn1crypto.algos import DSASignature

    half = len(raw) // 2
    return bytes(
        DSASignature(
            {"r": int.from_bytes(raw[:half], "big"), "s": int.from_bytes(raw[half:], "big")}
        ).dump()
    )


@dataclass(frozen=True, slots=True)
class _Acquired:
    handle: int
    key_spec: int
    caller_free: bool


class StoreKey:
    """A certificate context, and how to sign with its key.

    Holds only the certificate until :meth:`sign_digest`: the key is acquired for that one call
    and released, so nothing touches a token before the user's signature is due.
    """

    prefer_ncrypt = True  # tests turn it off to reach the legacy CAPI path with a software key

    def __init__(
        self,
        api: Any,
        context: Any,
        parent_window: int = 0,
        store: Any = None,
        ephemeral_key: int = 0,
    ) -> None:
        self._api = api
        self._ephemeral_key = ephemeral_key  # owned by the certificate context
        self._context = context
        self._store = store
        self.parent_window = parent_window
        c = context.contents
        self.der = string_at(c.pbCertEncoded, c.cbCertEncoded)

    def __enter__(self) -> StoreKey:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._context:
            self._api.CertFreeCertificateContext(self._context)
            self._context = None
        if self._store:
            self._api.CertCloseStore(self._store, 0)
            self._store = None

    def chain(self) -> list[bytes]:
        """The certificates above this one, from what's on this machine (no network, no key)."""
        api = self._api
        para = _ChainPara()
        para.cbSize = sizeof(_ChainPara)
        result = POINTER(_ChainContext)()
        flags = CERT_CHAIN_CACHE_ONLY_URL_RETRIEVAL | CERT_CHAIN_DISABLE_AUTH_ROOT_AUTO_UPDATE
        ok = api.CertGetCertificateChain(
            None, self._context, None, None, byref(para), flags, None, byref(result)
        )
        if not ok:  # the signature is fine without intermediates, if less convenient to check
            log.info("couldn't build the certificate chain: 0x%08X", api.last_error())
            return []
        try:
            certs: list[bytes] = []
            if result.contents.cChain:
                simple = result.contents.rgpChain[0].contents
                for i in range(simple.cElement):
                    c = simple.rgpElement[i].contents.pCertContext.contents
                    certs.append(string_at(c.pbCertEncoded, c.cbCertEncoded))
            return [c for c in certs if c != self.der]
        finally:
            api.CertFreeCertificateChain(result)

    def _acquire(self) -> _Acquired:
        if self._ephemeral_key:
            return _Acquired(self._ephemeral_key, CERT_NCRYPT_KEY_SPEC, False)
        api = self._api
        flags = CRYPT_ACQUIRE_PREFER_NCRYPT_KEY_FLAG if self.prefer_ncrypt else 0
        hwnd = wintypes.HWND(self.parent_window or 0)
        parameter = None
        if self.parent_window:
            flags |= CRYPT_ACQUIRE_WINDOW_HANDLE_FLAG
            parameter = byref(hwnd)
            # Legacy CSPs take their dialog parent per process, before the key is acquired.
            api.CryptSetProvParam(0, PP_CLIENT_HWND, byref(hwnd), 0)
        handle = ctypes.c_size_t()
        spec = wintypes.DWORD()
        caller_free = wintypes.BOOL()
        if not api.CryptAcquireCertificatePrivateKey(
            self._context, flags, parameter, byref(handle), byref(spec), byref(caller_free)
        ):
            raise_for_status(api.last_error(), "opening the private key")
        return _Acquired(handle.value, spec.value, bool(caller_free.value))

    def sign_digest(self, digest: bytes, digest_name: str) -> bytes:
        """The raw signature over ``digest``, as CMS expects it. A PIN prompt can appear here."""
        rsa = _is_rsa(self.der)
        key = self._acquire()
        try:
            if key.key_spec == CERT_NCRYPT_KEY_SPEC:
                return self._sign_cng(key.handle, digest, digest_name, rsa)
            if not rsa:
                raise SigningError("This key's legacy provider can only sign with RSA.")
            return self._sign_capi(key, digest, digest_name)
        finally:
            if key.caller_free:
                if key.key_spec == CERT_NCRYPT_KEY_SPEC:
                    self._api.NCryptFreeObject(key.handle)
                else:
                    self._api.CryptReleaseContext(key.handle, 0)

    def _sign_cng(self, handle: int, digest: bytes, digest_name: str, rsa: bool) -> bytes:
        api = self._api
        if self.parent_window:
            hwnd = wintypes.HWND(self.parent_window)
            api.NCryptSetProperty(
                handle, NCRYPT_WINDOW_HANDLE_PROPERTY, byref(hwnd), sizeof(hwnd), 0
            )
        padding = None
        flags = 0
        if rsa:
            info = _Pkcs1PaddingInfo(_CNG_HASH[digest_name])
            padding, flags = byref(info), BCRYPT_PAD_PKCS1
        data = ctypes.create_string_buffer(digest, len(digest))
        size = wintypes.DWORD()
        status = api.NCryptSignHash(handle, padding, data, len(digest), None, 0, byref(size), flags)
        if status:
            raise_for_status(status, "asking for the signature size")
        signature = ctypes.create_string_buffer(size.value)
        status = api.NCryptSignHash(
            handle, padding, data, len(digest), signature, size.value, byref(size), flags
        )
        if status:
            raise_for_status(status, "signing")
        raw = signature.raw[: size.value]
        return raw if rsa else ecdsa_raw_to_der(raw)

    def _sign_capi(self, key: _Acquired, digest: bytes, digest_name: str) -> bytes:
        """Legacy CSPs, which old token middleware still ships."""
        api = self._api
        hash_handle = ctypes.c_size_t()
        if not api.CryptCreateHash(key.handle, _CAPI_HASH[digest_name], 0, 0, byref(hash_handle)):
            raise_for_status(api.last_error(), "starting the hash")
        try:
            data = ctypes.create_string_buffer(digest, len(digest))
            if not api.CryptSetHashParam(hash_handle.value, HP_HASHVAL, data, 0):
                raise_for_status(api.last_error(), "setting the hash")
            size = wintypes.DWORD()
            if not api.CryptSignHashW(hash_handle.value, key.key_spec, None, 0, None, byref(size)):
                raise_for_status(api.last_error(), "asking for the signature size")
            signature = ctypes.create_string_buffer(size.value)
            if not api.CryptSignHashW(
                hash_handle.value, key.key_spec, None, 0, signature, byref(size)
            ):
                raise_for_status(api.last_error(), "signing")
            # CryptoAPI returns the integer little-endian, unlike CNG and CMS.
            return signature.raw[: size.value][::-1]
        finally:
            api.CryptDestroyHash(hash_handle.value)


def _is_rsa(der: bytes) -> bool:
    from asn1crypto import x509

    return bool(x509.Certificate.load(der).public_key.algorithm == "rsa")


def _open_my_store(api: Any) -> Any:
    store = api.CertOpenStore(
        CERT_STORE_PROV_SYSTEM_W,
        0,
        0,
        CERT_SYSTEM_STORE_CURRENT_USER | CERT_STORE_READONLY_FLAG,
        "MY",
    )
    if not store:
        raise SigningError(
            f"Couldn't open the Windows certificate store (error 0x{api.last_error():08X})."
        )
    return store


def list_certificates() -> list[CertificateInfo]:
    """Certificates in CurrentUser\\My that have a private key and may sign.

    Only reads the certificate and its stored properties. No key is opened, so no PIN prompt
    can appear, whichever token the certificates belong to.
    """
    if not available():
        return []
    api = _api()
    store = _open_my_store(api)
    found: list[CertificateInfo] = []
    try:
        context = api.CertEnumCertificatesInStore(store, None)
        while context:
            provider = _key_provider(api, context)
            if provider is not None:  # None: no private key behind this certificate
                c = context.contents
                der = string_at(c.pbCertEncoded, c.cbCertEncoded)
                try:
                    info = describe(der, provider=provider, hardware=is_hardware_provider(provider))
                except ValueError:
                    log.info("skipped an unreadable certificate in the Windows store")
                else:
                    if info.can_sign:
                        found.append(info)
            context = api.CertEnumCertificatesInStore(store, context)
    finally:
        api.CertCloseStore(store, 0)
    found.sort(key=lambda i: (not i.non_repudiation, i.subject.lower(), i.not_after))
    return found


def root_certificates() -> list[bytes]:
    """DER certificates in the CurrentUser and LocalMachine ROOT stores: the anchors Windows
    trusts. Only certificate bytes are read; no key is opened, so no PIN prompt can appear."""
    if not available():
        return []
    api = _api()
    found: list[bytes] = []
    for location in (CERT_SYSTEM_STORE_CURRENT_USER, CERT_SYSTEM_STORE_LOCAL_MACHINE):
        store = api.CertOpenStore(
            CERT_STORE_PROV_SYSTEM_W, 0, 0, location | CERT_STORE_READONLY_FLAG, "ROOT"
        )
        if not store:
            log.info("couldn't open a Windows root store (error 0x%08X)", api.last_error())
            continue
        try:
            context = api.CertEnumCertificatesInStore(store, None)
            while context:
                c = context.contents
                found.append(string_at(c.pbCertEncoded, c.cbCertEncoded))
                context = api.CertEnumCertificatesInStore(store, context)
        finally:
            api.CertCloseStore(store, 0)
    return found


def _key_provider(api: Any, context: Any) -> str | None:
    """The key's provider name ("" if unnamed), or None for a certificate without a key."""
    size = wintypes.DWORD()
    if not api.CertGetCertificateContextProperty(
        context, CERT_KEY_PROV_INFO_PROP_ID, None, byref(size)
    ):
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if not api.CertGetCertificateContextProperty(
        context, CERT_KEY_PROV_INFO_PROP_ID, buffer, byref(size)
    ):
        return None
    return cast(buffer, POINTER(_KeyProvInfo)).contents.pwszProvName or ""


def open_certificate(thumbprint: str, parent_window: int = 0) -> StoreKey:
    """Find a certificate in CurrentUser\\My by thumbprint. Touches the certificate only."""
    api = _api()
    store = _open_my_store(api)
    digest = ctypes.create_string_buffer(bytes.fromhex(thumbprint))
    blob = _Blob(len(digest) - 1, cast(digest, c_void_p))
    context = api.CertFindCertificateInStore(
        store, ENCODING, 0, CERT_FIND_SHA1_HASH, byref(blob), None
    )
    if not context:
        api.CertCloseStore(store, 0)
        raise TokenUnavailable(
            "This certificate is no longer in the Windows certificate store. Plug its token "
            "in, or refresh the list."
        )
    return StoreKey(api, context, parent_window, store)


def import_ephemeral_pfx(data: bytes, password: str) -> StoreKey:
    """A key that exists only in this process, imported with PKCS12_NO_PERSIST_KEY.

    No store on disk and no key container gets written, so this is how the self-test and the
    tests exercise real CNG signing without leaving anything behind or going near the user's
    own certificates. The app doesn't sign documents with it.
    """
    api = _api()
    payload = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), cast(payload, c_void_p))
    store = api.PFXImportCertStore(
        byref(blob), password, PKCS12_NO_PERSIST_KEY | PKCS12_ALWAYS_CNG_KSP
    )
    if not store:
        raise SigningError(f"Windows couldn't import the PFX (error 0x{api.last_error():08X}).")
    context = api.CertEnumCertificatesInStore(store, None)
    while context:
        handle = ctypes.c_size_t()
        size = wintypes.DWORD(sizeof(handle))
        if api.CertGetCertificateContextProperty(
            context, CERT_NCRYPT_KEY_HANDLE_PROP_ID, byref(handle), byref(size)
        ):
            duplicate = api.CertDuplicateCertificateContext(context)
            return StoreKey(api, duplicate, 0, store, handle.value)
        context = api.CertEnumCertificatesInStore(store, context)
    api.CertCloseStore(store, 0)
    raise SigningError("The PFX holds no certificate with a private key.")
