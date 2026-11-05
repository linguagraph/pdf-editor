"""PKCS#11 tokens, for USB devices whose vendor ships only a PKCS#11 module (a DLL).

PIN rules, because tokens lock after a few failures:

* Listing the tokens never sends a PIN. Tokens whose certificates stay hidden are listed anyway,
  so the user can pick the one to log in to.
* A PIN goes to the one token the user chose (slot and serial), once per user action, and
  nothing here retries a login. A key marked CKA_ALWAYS_AUTHENTICATE gets the same PIN once more
  with its signature, as such keys demand for each operation.
* A locked token isn't tried, and a token on its final try only after explicit confirmation.
* The PIN isn't logged.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pdfeditor.core.jobs import Cancelled
from pdfeditor.model.signing import PinState, Pkcs11Key, TokenCertificate, TokenInfo
from pdfeditor.services.signing.certificates import (
    chain_for,
    describe,
    digest_for,
    require_usable,
)
from pdfeditor.services.signing.errors import (
    PinFinalTry,
    PinLocked,
    SigningError,
    TokenUnavailable,
    WrongPin,
)

log = logging.getLogger(__name__)

LOAD_WITH_ALTERED_SEARCH_PATH = 0x8
DRIVE_REMOTE = 4
_LOCKED_MESSAGE = (
    "The token's PIN is locked. Unlock it with the tools from the token's vendor, then sign again."
)
_FINAL_TRY_MESSAGE = (
    "One more wrong PIN locks this token for good, so the PIN wasn't sent. Check it, and confirm "
    "when asked before trying."
)
_preloaded: dict[str, Any] = {}  # kept for the life of the process, like the loaded module
_in_use: dict[str, threading.Lock] = {}
_in_use_guard = threading.Lock()


def module_path(path: str) -> str:
    """The one spelling of a module's path: python-pkcs11 caches modules by string, and loading
    one twice under two spellings would initialize it twice. Must be a full path on this
    computer, because the module runs inside the app with the user's PIN."""
    text = path.strip()
    if not os.path.isabs(text) or text.startswith(("\\\\", "//")) or _on_network_drive(text):
        raise SigningError(
            f"“{path}” isn't a full path on this computer. Choose the token vendor's PKCS#11 "
            "DLL from a local drive."
        )
    return os.path.normcase(os.path.normpath(text))


def _on_network_drive(path: str) -> bool:
    if sys.platform != "win32":
        return False
    root = os.path.splitdrive(path)[0] + "\\"
    return bool(ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_REMOTE)


def _preload(path: str) -> None:
    """python-pkcs11 calls plain LoadLibraryW, which doesn't look in the module's own folder for
    the DLLs it depends on, and vendors keep those side by side. Loading it here first, with
    that folder searched, satisfies them; python-pkcs11 then gets this same loaded module."""
    if sys.platform == "win32" and path not in _preloaded:
        _preloaded[path] = ctypes.WinDLL(path, winmode=LOAD_WITH_ALTERED_SEARCH_PATH)


def _load_module(path: str) -> Any:
    """Load (and keep loaded) the vendor's module."""
    import pkcs11

    normal = module_path(path)
    try:
        _preload(normal)
        return pkcs11.lib(normal)
    except (OSError, RuntimeError) as exc:
        raise SigningError(
            f"Couldn't load the PKCS#11 module “{path}”. Check that this is the token vendor's "
            f"PKCS#11 DLL and that it's the 64-bit version ({exc})."
        ) from exc


@contextmanager
def _exclusive(module: str) -> Iterator[None]:
    """One login at a time per module: a token's login belongs to the whole process, so a second
    signature could ride on the first one's login (the PIN typed for it never checked), and
    whichever session closes last would log the other out."""
    key = os.path.normcase(os.path.abspath(module))
    with _in_use_guard:
        lock = _in_use.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise SigningError(
            "Another signature is using this token. Wait until it has finished, then try again."
        )
    try:
        yield
    finally:
        lock.release()


def translate(exc: Exception) -> Exception:
    """What a PKCS#11 error means to the user. The message never contains the PIN."""
    from pkcs11 import exceptions as e

    if isinstance(exc, e.PinIncorrect):
        return WrongPin(
            "The token rejected the PIN. Tokens lock after a few failed attempts, so check the "
            "PIN before trying again."
        )
    if isinstance(exc, e.PinLocked):
        return PinLocked(_LOCKED_MESSAGE)
    if isinstance(exc, e.PinExpired):
        return SigningError("The token's PIN has expired. Change it with the vendor's tools.")
    if isinstance(exc, e.PinInvalid | e.PinLenRange):
        return WrongPin("The token doesn't accept a PIN of this form. Check the PIN you typed.")
    if isinstance(exc, e.FunctionCancelled):
        return Cancelled()
    if isinstance(exc, e.UserAlreadyLoggedIn):
        return SigningError(
            "The token is already logged in from another signature in this app, so the PIN "
            "couldn't be checked. Wait until that has finished, then try again."
        )
    gone = (
        e.TokenNotPresent,
        e.TokenNotRecognised,
        e.DeviceRemoved,
        e.DeviceError,
        e.NoSuchToken,
        e.SlotIDInvalid,
        e.SessionClosed,
        e.SessionHandleInvalid,
    )
    if isinstance(exc, gone):
        return TokenUnavailable(
            "The token isn't available any more. Plug it in, wait until its software sees it, "
            "and sign again."
        )
    return SigningError(f"The token reported an error: {type(exc).__name__}.")


def _pin_state(flags: int) -> PinState:
    from pkcs11.constants import TokenFlag

    if flags & TokenFlag.USER_PIN_LOCKED:
        return PinState.LOCKED
    if flags & TokenFlag.USER_PIN_FINAL_TRY:
        return PinState.FINAL_TRY
    if flags & TokenFlag.USER_PIN_COUNT_LOW:
        return PinState.COUNT_LOW
    return PinState.OK


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value or "").strip()


def _certificates(session: Any) -> list[tuple[bytes, bytes, str]]:
    from pkcs11 import Attribute, ObjectClass

    found = []
    for obj in list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE})):
        found.append(
            (
                bytes(obj[Attribute.VALUE]),
                bytes(obj[Attribute.ID] or b""),
                _text(obj[Attribute.LABEL]),
            )
        )
    return found


def _token_info(slot: Any, token: Any, rows: list[tuple[bytes, bytes, str]]) -> TokenInfo:
    from pkcs11.constants import TokenFlag

    flags = int(token.flags)
    certificates = []
    for der, cert_id, label in rows:
        try:
            info = describe(der, hardware=True)
        except ValueError:
            log.info("skipped an unreadable certificate on a token")
            continue
        if info.can_sign:
            certificates.append(TokenCertificate(info, cert_id, label))
    return TokenInfo(
        slot_id=int(slot.slot_id),
        label=_text(token.label),
        manufacturer=_text(token.manufacturer_id),
        serial=_text(token.serial),
        protected_auth=bool(flags & TokenFlag.PROTECTED_AUTHENTICATION_PATH),
        pin_state=_pin_state(flags),
        certificates=tuple(certificates),
    )


def list_tokens(module: str) -> list[TokenInfo]:
    """Every token plugged in, read without any PIN. A token that hides its certificates until
    login is listed with none, its PIN counter state still showing."""
    lib = _load_module(module)
    with _exclusive(module):  # a signature may be waiting on a PIN prompt in another thread
        return _read_tokens(lib)


def _read_tokens(lib: Any) -> list[TokenInfo]:
    from pkcs11 import PKCS11Error

    tokens: list[TokenInfo] = []
    try:
        slots = lib.get_slots(token_present=True)
    except PKCS11Error as exc:
        raise translate(exc) from exc
    problem: PKCS11Error | None = None
    for slot in slots:
        try:
            token = slot.get_token()
            try:
                with token.open() as session:
                    rows = _certificates(session)
            except PKCS11Error as exc:  # a public session refused: its certificates stay hidden
                log.info("a token wouldn't list publicly: %s", type(exc).__name__)
                rows = []
            tokens.append(_token_info(slot, token, rows))
        except PKCS11Error as exc:  # one bad slot shouldn't hide the other tokens
            log.info("skipped a slot: %s", type(exc).__name__)
            problem = exc
    if problem is not None and not tokens:
        raise translate(problem) from problem
    return tokens


def _find_token(lib: Any, slot_id: int, serial: str) -> Any:
    for slot in lib.get_slots(token_present=True):
        if int(slot.slot_id) == slot_id:
            token = slot.get_token()
            if _text(token.serial) == serial:
                return slot, token
    raise TokenUnavailable(
        "The token you chose isn't plugged in any more. Plug it in and sign again."
    )


def _require_may_log_in(token: Any, allow_final_try: bool) -> None:
    """The counter as the token reports it now, not as it was when the list was read."""
    state = _pin_state(int(token.flags))
    if state is PinState.LOCKED:
        raise PinLocked(_LOCKED_MESSAGE)
    if state is PinState.FINAL_TRY and not allow_final_try:
        raise PinFinalTry(_FINAL_TRY_MESSAGE)


def login_and_list(
    module: str,
    slot_id: int,
    serial: str,
    pin: str | None,
    *,
    protected_auth: bool = False,
    allow_final_try: bool = False,
) -> TokenInfo:
    """Log in to one chosen token, once, and read the certificates it was hiding."""
    from pkcs11 import PROTECTED_AUTH, PKCS11Error

    lib = _load_module(module)
    with _exclusive(module):
        try:
            slot, token = _find_token(lib, slot_id, serial)
            _require_may_log_in(token, allow_final_try)
            with token.open(user_pin=PROTECTED_AUTH if protected_auth else pin) as session:
                return _token_info(slot, token, _certificates(session))
        except PKCS11Error as exc:
            raise translate(exc) from exc


def _needs_raw_mechanism(slot: Any, cert: Any) -> bool:
    """Some tokens only do the bare RSA or ECDSA operation, not hash-and-sign."""
    from pyhanko.sign.pkcs11 import ECDSA_MECH_MAP, RSA_MECH_MAP

    table = RSA_MECH_MAP if cert.public_key.algorithm == "rsa" else ECDSA_MECH_MAP
    wanted = table.get(digest_for(cert.public_key))
    try:
        return wanted not in slot.get_mechanisms()
    except Exception:  # listing mechanisms is optional: go with the ordinary way
        log.info("the token didn't list its mechanisms")
        return False


@contextmanager
def token_signer(key: Pkcs11Key) -> Iterator[Any]:
    """A pyHanko signer on the chosen token, logged in once with the user's PIN."""
    from asn1crypto import x509
    from pkcs11 import PROTECTED_AUTH, Attribute, ObjectClass, PKCS11Error
    from pyhanko.sign.pkcs11 import PKCS11Signer

    lib = _load_module(key.module)
    with _exclusive(key.module):
        try:
            slot, token = _find_token(lib, key.slot_id, key.token_serial)
            _require_may_log_in(token, key.allow_final_try)
            pin: Any = PROTECTED_AUTH if key.protected_auth else key.pin
            session = token.open(user_pin=pin)
        except PKCS11Error as exc:
            raise translate(exc) from exc
        try:
            rows = _certificates(session)
            # By the whole certificate: a renewed one shares its CKA_ID and key with the old.
            if not any(r[0] == key.certificate for r in rows):
                raise TokenUnavailable("The chosen certificate isn't on this token any more.")
            require_usable(describe(key.certificate, hardware=True))
            cert = x509.Certificate.load(key.certificate)
            chain = chain_for(key.certificate, [r[0] for r in rows])
            ids = {"key_id": key.cert_id} if key.cert_id else {"key_label": key.cert_label}
            sign_kwargs: dict[str, Any] = {}
            lookup = (
                {Attribute.ID: key.cert_id} if key.cert_id else {Attribute.LABEL: key.cert_label}
            )
            handles = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY} | lookup))
            if len(handles) == 1 and not key.protected_auth and _always_authenticate(handles[0]):
                sign_kwargs["pin"] = key.pin  # CKA_ALWAYS_AUTHENTICATE: once more for the signature
            yield PKCS11Signer(
                session,
                signing_cert=cert,
                ca_chain=[x509.Certificate.load(c) for c in chain],
                use_raw_mechanism=_needs_raw_mechanism(slot, cert),
                base_sign_kwargs=sign_kwargs,
                **ids,
            )
        except PKCS11Error as exc:
            raise translate(exc) from exc
        finally:
            try:
                session.close()
            except PKCS11Error:  # the token may have been unplugged meanwhile
                log.info("the token session was already gone")


def _always_authenticate(handle: Any) -> bool:
    from pkcs11 import Attribute, PKCS11Error

    try:
        return bool(handle[Attribute.ALWAYS_AUTHENTICATE])
    except (PKCS11Error, KeyError, NotImplementedError):
        return False
