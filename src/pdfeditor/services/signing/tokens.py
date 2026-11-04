"""PKCS#11 tokens, for USB devices whose vendor ships only a PKCS#11 module (a DLL).

A PIN is sent to a token at most once per user action. Nothing here ever retries a login: tokens
lock after a few failures. The PIN isn't logged, and a locked token isn't even tried.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pdfeditor.core.jobs import Cancelled
from pdfeditor.model.signing import PinState, Pkcs11Key, TokenCertificate, TokenInfo
from pdfeditor.services.signing.certificates import chain_for, describe, require_usable
from pdfeditor.services.signing.errors import (
    PinLocked,
    SigningError,
    TokenUnavailable,
    WrongPin,
)

log = logging.getLogger(__name__)


def _load_module(path: str) -> Any:
    """Load (and keep loaded) the vendor's module; python-pkcs11 caches it per path."""
    import pkcs11

    try:
        return pkcs11.lib(path)
    except (OSError, RuntimeError) as exc:
        raise SigningError(
            f"Couldn't load the PKCS#11 module “{path}”. Check that this is the token vendor's "
            f"PKCS#11 DLL and that it's the 64-bit version ({exc})."
        ) from exc


def translate(exc: Exception) -> Exception:
    """What a PKCS#11 error means to the user. The message never contains the PIN."""
    from pkcs11 import exceptions as e

    if isinstance(exc, e.PinIncorrect):
        return WrongPin(
            "The token rejected the PIN. Tokens lock after a few failed attempts, so check the "
            "PIN before trying again."
        )
    if isinstance(exc, e.PinLocked):
        return PinLocked(
            "The token's PIN is locked. Unlock it with the tools from the token's vendor, then "
            "sign again."
        )
    if isinstance(exc, e.PinExpired):
        return SigningError("The token's PIN has expired. Change it with the vendor's tools.")
    if isinstance(exc, e.PinInvalid | e.PinLenRange):
        return WrongPin("The token doesn't accept a PIN of this form. Check the PIN you typed.")
    if isinstance(exc, e.FunctionCancelled):
        return Cancelled()
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


def list_tokens(module: str, pin: str | None = None) -> list[TokenInfo]:
    """The tokens plugged in and their signing certificates.

    Without ``pin`` this only opens a public session, which most tokens answer. With one (the
    user pressed List after typing it, because the certificates stayed hidden), it logs in once.
    """
    from pkcs11 import PKCS11Error
    from pkcs11.constants import TokenFlag

    lib = _load_module(module)
    tokens: list[TokenInfo] = []
    try:
        for slot in lib.get_slots(token_present=True):
            token = slot.get_token()
            flags = int(token.flags)
            state = _pin_state(flags)
            protected = bool(flags & TokenFlag.PROTECTED_AUTHENTICATION_PATH)
            login = pin if pin and state is not PinState.LOCKED and not protected else None
            with token.open(user_pin=login) as session:
                rows = _certificates(session)
            certificates = []
            for der, cert_id, label in rows:
                try:
                    info = describe(der, hardware=True)
                except ValueError:
                    log.info("skipped an unreadable certificate on a token")
                    continue
                if info.can_sign:
                    certificates.append(TokenCertificate(info, cert_id, label))
            tokens.append(
                TokenInfo(
                    slot_id=int(slot.slot_id),
                    label=_text(token.label),
                    manufacturer=_text(token.manufacturer_id),
                    serial=_text(token.serial),
                    protected_auth=protected,
                    pin_state=state,
                    certificates=tuple(certificates),
                )
            )
    except PKCS11Error as exc:
        raise translate(exc) from exc
    return tokens


def _find_token(lib: Any, key: Pkcs11Key) -> Any:
    for slot in lib.get_slots(token_present=True):
        if int(slot.slot_id) == key.slot_id:
            token = slot.get_token()
            if _text(token.serial) == key.token_serial:
                return slot, token
    raise TokenUnavailable(
        "The token you chose isn't plugged in any more. Plug it in and sign again."
    )


def _needs_raw_mechanism(slot: Any, cert: Any) -> bool:
    """Some tokens only do the bare RSA or ECDSA operation, not hash-and-sign."""
    from pyhanko.sign.pkcs11 import ECDSA_MECH_MAP, RSA_MECH_MAP
    from pyhanko.sign.signers.pdf_cms import select_suitable_signing_md

    table = RSA_MECH_MAP if cert.public_key.algorithm == "rsa" else ECDSA_MECH_MAP
    wanted = table.get(select_suitable_signing_md(cert.public_key))
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
    from pkcs11 import exceptions as e
    from pyhanko.sign.pkcs11 import PKCS11Signer

    lib = _load_module(key.module)
    try:
        slot, token = _find_token(lib, key)
        if _pin_state(int(token.flags)) is PinState.LOCKED:  # don't even try
            raise PinLocked(
                "The token's PIN is locked. Unlock it with the tools from the token's vendor, "
                "then sign again."
            )
        pin: Any = PROTECTED_AUTH if key.protected_auth else key.pin
        try:
            session = token.open(user_pin=pin)
        except e.UserAlreadyLoggedIn:  # another session did; don't send the PIN again
            session = token.open()
    except PKCS11Error as exc:
        raise translate(exc) from exc
    try:
        rows = _certificates(session)
        wanted = [
            r for r in rows if r[1] == key.cert_id and (key.cert_id or r[2] == key.cert_label)
        ]
        if not wanted:
            raise TokenUnavailable("The chosen certificate isn't on this token any more.")
        der = wanted[0][0]
        require_usable(describe(der, hardware=True))
        cert = x509.Certificate.load(der)
        chain = chain_for(der, [r[0] for r in rows])
        ids = {"key_id": key.cert_id} if key.cert_id else {"key_label": key.cert_label}
        sign_kwargs: dict[str, Any] = {}
        handles = list(
            session.get_objects(
                {Attribute.CLASS: ObjectClass.PRIVATE_KEY}
                | (
                    {Attribute.ID: key.cert_id}
                    if key.cert_id
                    else {Attribute.LABEL: key.cert_label}
                )
            )
        )
        if len(handles) == 1 and not key.protected_auth and _always_authenticate(handles[0]):
            sign_kwargs["pin"] = key.pin  # qualified keys ask again for each signature
        signer = PKCS11Signer(
            session,
            signing_cert=cert,
            ca_chain=[x509.Certificate.load(c) for c in chain],
            use_raw_mechanism=_needs_raw_mechanism(slot, cert),
            base_sign_kwargs=sign_kwargs,
            **ids,
        )
        yield signer
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
