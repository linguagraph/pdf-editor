"""PKCS#11 tokens, with a fake module behind python-pkcs11's interface and pyHanko's real signer.

There's no real token or SoftHSM in these tests: what a real device does is on the manual
checklist in PLAN.md. The fake signs with a throwaway key, so results validate for real.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from asn1crypto.algos import DigestInfo
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature
from cryptography.hazmat.primitives.serialization import pkcs12
from pkcs11 import PROTECTED_AUTH, Attribute, Mechanism, ObjectClass
from pkcs11 import exceptions as p11e
from pkcs11.constants import TokenFlag

from pdfeditor.core.jobs import Cancelled
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.signing import PinState, Pkcs11Key, SignatureOptions
from pdfeditor.services.signing import (
    PinLocked,
    SigningError,
    SigningRefused,
    TokenUnavailable,
    WrongPin,
    sign_file,
    tokens,
)

ENGINE = get_engine()
PIN = "s3cret-pin"


class FakeObject:
    def __init__(self, attrs: dict[Any, Any], sign: Any = None) -> None:
        self.attrs = attrs
        self._sign = sign

    def __getitem__(self, key: Any) -> Any:
        return self.attrs[key]

    def sign(self, data: bytes, **kwargs: Any) -> bytes:
        return self._sign(data, **kwargs)


class FakeSession:
    def __init__(self, token: FakeToken) -> None:
        self.token = token
        self.closed = False

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def get_objects(self, query: dict[Any, Any]) -> list[FakeObject]:
        return [o for o in self.token.objects if all(o.attrs.get(k) == v for k, v in query.items())]


class FakeToken:
    def __init__(
        self,
        pfx: Path | None = None,
        *,
        flags: int = 0,
        open_error: Exception | None = None,
        sign_error: Exception | None = None,
        hides_certs: bool = False,
        always_authenticate: bool = False,
    ) -> None:
        self.label = "Test Token"
        self.manufacturer_id = "Fake Inc"
        self.serial = b"SER123"
        self.flags = flags
        self.open_error = open_error
        self.sign_error = sign_error
        self.hides_certs = hides_certs
        self.logins: list[Any] = []
        self.opens = 0
        self.mechanisms_used: list[Any] = []
        self.sign_kwargs: list[dict[str, Any]] = []
        self.objects: list[FakeObject] = []
        self.key: Any = None
        if pfx is not None:
            key, cert, _other = pkcs12.load_key_and_certificates(pfx.read_bytes(), b"test")
            assert cert is not None
            self.key = key
            self.objects = [
                FakeObject(
                    {
                        Attribute.CLASS: ObjectClass.CERTIFICATE,
                        Attribute.VALUE: cert.public_bytes(serialization.Encoding.DER),
                        Attribute.ID: b"\x01",
                        Attribute.LABEL: "signing",
                    }
                ),
                FakeObject(
                    {
                        Attribute.CLASS: ObjectClass.PRIVATE_KEY,
                        Attribute.ID: b"\x01",
                        Attribute.LABEL: "signing",
                        Attribute.SIGN: True,
                        Attribute.ALWAYS_AUTHENTICATE: always_authenticate,
                    },
                    self._sign,
                ),
            ]

    def _sign(self, data: bytes, mechanism: Any = None, **kwargs: Any) -> bytes:
        self.mechanisms_used.append(mechanism)
        self.sign_kwargs.append(kwargs)
        if self.sign_error is not None:
            raise self.sign_error
        if mechanism == Mechanism.RSA_PKCS:  # raw: data is an encoded DigestInfo
            digest = DigestInfo.load(data)["digest"].native
            return self.key.sign(digest, padding.PKCS1v15(), Prehashed(hashes.SHA256()))
        if mechanism == Mechanism.SHA256_RSA_PKCS:
            return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        if mechanism == Mechanism.ECDSA:  # raw: data is the digest, the result r||s
            der = self.key.sign(data, ec.ECDSA(Prehashed(hashes.SHA256())))
            r, s = decode_dss_signature(der)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        if mechanism == Mechanism.ECDSA_SHA256:
            der = self.key.sign(data, ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        raise AssertionError(f"unexpected mechanism {mechanism}")

    def open(self, user_pin: Any = None) -> FakeSession:
        self.opens += 1
        if user_pin is not None:
            self.logins.append(user_pin)
        if self.open_error is not None:
            raise self.open_error
        session = FakeSession(self)
        if self.hides_certs and user_pin is None:
            session.get_objects = lambda _q: []  # type: ignore[method-assign]
        return session


class FakeSlot:
    slot_id = 7

    def __init__(self, token: FakeToken, mechanisms: set[Any] | None = None) -> None:
        self.token = token
        self.mechanisms = mechanisms

    def get_token(self) -> FakeToken:
        return self.token

    def get_mechanisms(self) -> set[Any]:
        if self.mechanisms is None:
            raise p11e.FunctionFailed()
        return self.mechanisms


class FakeLib:
    def __init__(self, *slots: FakeSlot) -> None:
        self.slots = list(slots)

    def get_slots(self, token_present: bool = False) -> list[FakeSlot]:
        return self.slots


@pytest.fixture
def module(monkeypatch):
    """``module(FakeToken(...))`` installs a fake PKCS#11 module holding that token."""

    def install(token: FakeToken | None, mechanisms: set[Any] | None = None) -> FakeLib:
        lib = FakeLib(*([FakeSlot(token, mechanisms)] if token is not None else []))
        monkeypatch.setattr(tokens, "_load_module", lambda _path: lib)
        return lib

    return install


def key_ref(pin: str | None = PIN, **kwargs: Any) -> Pkcs11Key:
    fields = {"module": "vendor.dll", "slot_id": 7, "token_serial": "SER123", "cert_id": b"\x01"}
    return Pkcs11Key(pin=pin, **fields, **kwargs)


def sign(fixture_pdf, tmp_path: Path, key: Pkcs11Key) -> Path:
    target = tmp_path / "signed.pdf"
    sign_file(fixture_pdf("images"), target, key, SignatureOptions(), ENGINE)
    return target


def test_pin_never_in_repr_or_logs(
    fixture_pdf, throwaway_pfx, tmp_path: Path, module, caplog, signature_status
) -> None:
    caplog.set_level(logging.DEBUG)
    module(FakeToken(throwaway_pfx()))
    key = key_ref()
    assert PIN not in repr(key)
    target = sign(fixture_pdf, tmp_path, key)
    assert signature_status(target)[0].valid
    assert PIN not in caplog.text


def test_listing_uses_no_pin(throwaway_pfx, module) -> None:
    token = FakeToken(throwaway_pfx())
    module(token)
    (info,) = tokens.list_tokens("vendor.dll")
    assert token.logins == [], "listing must not log in"
    assert (
        info.label == "Test Token" and info.serial == "SER123" and info.manufacturer == "Fake Inc"
    )
    assert info.pin_state is PinState.OK and not info.protected_auth
    (cert,) = info.certificates
    assert cert.cert_id == b"\x01" and cert.label == "signing"
    assert cert.info.subject == "Elanero Test Signer" and cert.info.hardware is True


def test_listing_skips_certificates_that_cannot_sign(throwaway_pfx, module) -> None:
    module(FakeToken(throwaway_pfx(key_usage="encrypt")))
    (info,) = tokens.list_tokens("vendor.dll")
    assert info.certificates == ()


def test_listing_logs_in_only_when_given_a_pin(throwaway_pfx, module) -> None:
    token = FakeToken(throwaway_pfx(), hides_certs=True)
    module(token)
    assert tokens.list_tokens("vendor.dll")[0].certificates == ()
    assert token.logins == []
    assert len(tokens.list_tokens("vendor.dll", PIN)[0].certificates) == 1
    assert token.logins == [PIN]


@pytest.mark.parametrize(
    ("flags", "state"),
    [
        (TokenFlag.USER_PIN_COUNT_LOW, PinState.COUNT_LOW),
        (TokenFlag.USER_PIN_FINAL_TRY, PinState.FINAL_TRY),
        (TokenFlag.USER_PIN_LOCKED, PinState.LOCKED),
    ],
)
def test_pin_counter_flags(flags: int, state: PinState, throwaway_pfx, module) -> None:
    module(FakeToken(throwaway_pfx(), flags=int(flags)))
    assert tokens.list_tokens("vendor.dll")[0].pin_state is state


def test_no_token_lists_nothing(module) -> None:
    module(None)
    assert tokens.list_tokens("vendor.dll") == []


def test_module_load_failure_is_clear() -> None:
    with pytest.raises(SigningError, match="64-bit"):
        tokens.list_tokens("does-not-exist-pkcs11.dll")


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_signs_on_the_token_and_validates(
    kind: str, fixture_pdf, throwaway_pfx, tmp_path: Path, module, signature_status
) -> None:
    token = FakeToken(throwaway_pfx(kind))
    module(token)
    (status,) = signature_status(sign(fixture_pdf, tmp_path, key_ref()))
    assert status.intact and status.valid
    assert token.logins == [PIN], "one login"
    expected = Mechanism.SHA256_RSA_PKCS if kind == "rsa" else Mechanism.ECDSA_SHA256
    assert token.mechanisms_used == [expected]


@pytest.mark.parametrize(
    ("kind", "mechanisms", "used"),
    [
        ("rsa", {Mechanism.RSA_PKCS}, Mechanism.RSA_PKCS),
        ("ec", {Mechanism.ECDSA}, Mechanism.ECDSA),
    ],
)
def test_tokens_without_hash_and_sign_get_the_raw_mechanism(
    kind, mechanisms, used, fixture_pdf, throwaway_pfx, tmp_path, module, signature_status
) -> None:
    token = FakeToken(throwaway_pfx(kind))
    module(token, mechanisms)
    (status,) = signature_status(sign(fixture_pdf, tmp_path, key_ref()))
    assert status.intact and status.valid
    assert token.mechanisms_used == [used]


def test_always_authenticate_key_gets_the_pin_with_the_signature(
    fixture_pdf, throwaway_pfx, tmp_path, module, signature_status
) -> None:
    token = FakeToken(throwaway_pfx(), always_authenticate=True)
    module(token)
    assert signature_status(sign(fixture_pdf, tmp_path, key_ref()))[0].valid
    assert token.sign_kwargs == [{"pin": PIN}]


def test_protected_auth_sends_no_pin(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx())
    module(token)
    sign(fixture_pdf, tmp_path, key_ref(pin=None, protected_auth=True))
    assert token.logins == [PROTECTED_AUTH]


def test_wrong_pin_is_reported_and_never_retried(
    fixture_pdf, throwaway_pfx, tmp_path, module
) -> None:
    token = FakeToken(throwaway_pfx(), open_error=p11e.PinIncorrect())
    module(token)
    with pytest.raises(WrongPin, match="lock"):
        sign(fixture_pdf, tmp_path, key_ref())
    assert token.opens == 1, "a wrong PIN burns an attempt: exactly one, and no other"
    assert not (tmp_path / "signed.pdf").exists()


def test_locked_token_is_not_even_tried(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx(), flags=int(TokenFlag.USER_PIN_LOCKED))
    module(token)
    with pytest.raises(PinLocked, match="vendor"):
        sign(fixture_pdf, tmp_path, key_ref())
    assert token.opens == 0


def test_token_locking_during_login(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    module(FakeToken(throwaway_pfx(), open_error=p11e.PinLocked()))
    with pytest.raises(PinLocked):
        sign(fixture_pdf, tmp_path, key_ref())


def test_unplugged_before_signing(fixture_pdf, tmp_path, module) -> None:
    module(None)
    with pytest.raises(TokenUnavailable, match="plugged in"):
        sign(fixture_pdf, tmp_path, key_ref())


def test_other_token_in_the_slot(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx())
    token.serial = b"OTHER"
    module(token)
    with pytest.raises(TokenUnavailable):
        sign(fixture_pdf, tmp_path, key_ref())
    assert token.logins == [], "the PIN must never go to a different token"


def test_unplugged_while_signing(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    module(FakeToken(throwaway_pfx(), sign_error=p11e.DeviceRemoved()))
    with pytest.raises(TokenUnavailable, match="Plug it in"):
        sign(fixture_pdf, tmp_path, key_ref())
    assert not (tmp_path / "signed.pdf").exists()


def test_cancelled_on_the_device(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    module(FakeToken(throwaway_pfx(), sign_error=p11e.FunctionCancelled()))
    with pytest.raises(Cancelled):
        sign(fixture_pdf, tmp_path, key_ref())


def test_expired_certificate_on_the_token(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx(days=-3))
    module(token)
    with pytest.raises(SigningRefused, match="expired"):
        sign(fixture_pdf, tmp_path, key_ref())
    assert token.mechanisms_used == []


def test_certificate_gone_from_the_token(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    module(FakeToken(throwaway_pfx()))
    with pytest.raises(TokenUnavailable, match="isn't on this token"):
        sign(
            fixture_pdf,
            tmp_path,
            Pkcs11Key("vendor.dll", 7, "SER123", b"\x09", pin=PIN),
        )
