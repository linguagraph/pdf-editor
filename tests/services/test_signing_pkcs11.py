"""PKCS#11 tokens, with a fake module behind python-pkcs11's interface and pyHanko's real signer.

There's no real token or SoftHSM in these tests: what a real device does is on the manual
checklist in PLAN.md. The fake signs with a throwaway key, so results validate for real. It
counts every PIN it receives, per token, because a PIN sent to the wrong place can lock a token.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from asn1crypto.algos import DigestInfo
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from pkcs11 import PROTECTED_AUTH, Attribute, Mechanism, ObjectClass
from pkcs11 import exceptions as p11e
from pkcs11.constants import TokenFlag

from pdfeditor.core.jobs import Cancelled
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.signing import PinState, Pkcs11Key, SignatureOptions
from pdfeditor.services.signing import (
    PinFinalTry,
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
DER = serialization.Encoding.DER


class FakeObject:
    def __init__(self, attrs: dict[Any, Any], sign: Any = None) -> None:
        self.attrs = attrs
        self._sign = sign

    def __getitem__(self, key: Any) -> Any:
        return self.attrs[key]

    def sign(self, data: bytes, **kwargs: Any) -> bytes:
        return self._sign(data, **kwargs)


class FakeSession:
    def __init__(self, token: FakeToken, logged_in: bool) -> None:
        self.token = token
        self.closed = False
        self.logged_in = logged_in

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def get_objects(self, query: dict[Any, Any]) -> list[FakeObject]:
        if self.token.hides_certs and not self.logged_in:
            return []
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
        serial: bytes = b"SER123",
        right_pin: str | None = None,
    ) -> None:
        self.label = serial.decode()
        self.manufacturer_id = "Fake Inc"
        self.serial = serial
        self.flags = flags
        self.open_error = open_error
        self.sign_error = sign_error
        self.hides_certs = hides_certs
        self.right_pin = right_pin
        self.logins: list[Any] = []
        self.opens = 0
        self.mechanisms_used: list[Any] = []
        self.sign_kwargs: list[dict[str, Any]] = []
        self.objects: list[FakeObject] = []
        self.key: Any = None
        self.der = b""
        if pfx is not None:
            key, cert, _other = pkcs12.load_key_and_certificates(pfx.read_bytes(), b"test")
            assert cert is not None
            self.key = key
            self.der = cert.public_bytes(DER)
            self.objects = [
                self.certificate_object(self.der),
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

    @staticmethod
    def certificate_object(der: bytes) -> FakeObject:
        return FakeObject(
            {
                Attribute.CLASS: ObjectClass.CERTIFICATE,
                Attribute.VALUE: der,
                Attribute.ID: b"\x01",
                Attribute.LABEL: "signing",
            }
        )

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
        if self.open_error is not None and (user_pin is not None or self.right_pin is None):
            raise self.open_error
        if user_pin is not None and self.right_pin is not None and user_pin != self.right_pin:
            raise p11e.PinIncorrect()
        return FakeSession(self, logged_in=user_pin is not None)


class FakeSlot:
    def __init__(self, token: FakeToken, mechanisms: set[Any] | None = None, slot_id: int = 7):
        self.token = token
        self.mechanisms = mechanisms
        self.slot_id = slot_id

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


@pytest.fixture
def module_with(monkeypatch):
    """A fake module with several slots."""

    def install(*slots: FakeSlot) -> FakeLib:
        lib = FakeLib(*slots)
        monkeypatch.setattr(tokens, "_load_module", lambda _path: lib)
        return lib

    return install


def key_ref(token: FakeToken, pin: str | None = PIN, **kwargs: Any) -> Pkcs11Key:
    fields = {
        "module": "vendor.dll",
        "slot_id": 7,
        "token_serial": token.serial.decode(),
        "cert_id": b"\x01",
        "certificate": token.der,
    }
    return Pkcs11Key(pin=pin, **(fields | kwargs))


def sign(fixture_pdf, tmp_path: Path, key: Pkcs11Key) -> Path:
    target = tmp_path / "signed.pdf"
    sign_file(fixture_pdf("images"), target, key, SignatureOptions(), ENGINE)
    return target


def test_pin_never_in_repr_or_logs(
    fixture_pdf, throwaway_pfx, tmp_path: Path, module, caplog, signature_status
) -> None:
    caplog.set_level(logging.DEBUG)
    token = FakeToken(throwaway_pfx())
    module(token)
    key = key_ref(token)
    assert PIN not in repr(key)
    target = sign(fixture_pdf, tmp_path, key)
    assert signature_status(target)[0].valid
    assert PIN not in caplog.text


def test_listing_uses_no_pin(throwaway_pfx, module) -> None:
    token = FakeToken(throwaway_pfx())
    module(token)
    (info,) = tokens.list_tokens("vendor.dll")
    assert token.logins == [], "listing must not log in"
    assert info.label == "SER123" and info.serial == "SER123" and info.manufacturer == "Fake Inc"
    assert info.pin_state is PinState.OK and not info.protected_auth
    (cert,) = info.certificates
    assert cert.cert_id == b"\x01" and cert.label == "signing"
    assert cert.info.subject == "Elanero Test Signer" and cert.info.hardware is True


def test_listing_skips_certificates_that_cannot_sign(throwaway_pfx, module) -> None:
    module(FakeToken(throwaway_pfx(key_usage="encrypt")))
    (info,) = tokens.list_tokens("vendor.dll")
    assert info.certificates == ()


# -- whose PIN goes where ---------------------------------------------------------------------
def test_listing_several_tokens_sends_no_pin_to_any(throwaway_pfx, module_with) -> None:
    a = FakeToken(throwaway_pfx(), serial=b"A", hides_certs=True, right_pin="1111")
    b = FakeToken(throwaway_pfx(), serial=b"B", hides_certs=True, right_pin="2222")
    module_with(FakeSlot(a, slot_id=0), FakeSlot(b, slot_id=1))
    listed = tokens.list_tokens("vendor.dll")
    assert [t.serial for t in listed] == ["A", "B"], "hidden tokens are listed, to be chosen"
    assert all(t.certificates == () for t in listed)
    assert a.logins == [] and b.logins == []


def test_login_goes_to_the_chosen_token_only(throwaway_pfx, module_with) -> None:
    a = FakeToken(throwaway_pfx(), serial=b"A", hides_certs=True, right_pin="1111")
    b = FakeToken(throwaway_pfx(), serial=b"B", hides_certs=True, right_pin="2222")
    module_with(FakeSlot(a, slot_id=0), FakeSlot(b, slot_id=1))
    info = tokens.login_and_list("vendor.dll", 0, "A", "1111")
    assert info.serial == "A" and len(info.certificates) == 1
    assert a.logins == ["1111"]
    assert b.logins == [], "token A's PIN must never reach token B"


def test_several_pins_on_one_device_are_separate_slots(throwaway_pfx, module_with) -> None:
    """A card with an authentication PIN and a signing PIN: same serial, two slots."""
    auth = FakeToken(throwaway_pfx(), serial=b"CARD", hides_certs=True, right_pin="1111")
    signature = FakeToken(throwaway_pfx(), serial=b"CARD", hides_certs=True, right_pin="9999")
    module_with(FakeSlot(auth, slot_id=0), FakeSlot(signature, slot_id=1))
    tokens.login_and_list("vendor.dll", 1, "CARD", "9999")
    assert signature.logins == ["9999"] and auth.logins == []


def test_a_slot_with_another_token_gets_no_pin(throwaway_pfx, module_with) -> None:
    other = FakeToken(throwaway_pfx(), serial=b"OTHER")
    module_with(FakeSlot(other, slot_id=0))
    with pytest.raises(TokenUnavailable):
        tokens.login_and_list("vendor.dll", 0, "A", "1111")
    assert other.logins == []


def test_final_try_needs_confirmation_to_log_in(throwaway_pfx, module_with) -> None:
    token = FakeToken(
        throwaway_pfx(),
        serial=b"A",
        hides_certs=True,
        right_pin="1111",
        flags=int(TokenFlag.USER_PIN_FINAL_TRY),
    )
    module_with(FakeSlot(token, slot_id=0))
    with pytest.raises(PinFinalTry):
        tokens.login_and_list("vendor.dll", 0, "A", "1111")
    assert token.logins == [], "not sent without an explicit yes"
    tokens.login_and_list("vendor.dll", 0, "A", "1111", allow_final_try=True)
    assert token.logins == ["1111"]


def test_final_try_needs_confirmation_to_sign(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx(), flags=int(TokenFlag.USER_PIN_FINAL_TRY))
    module(token)
    with pytest.raises(PinFinalTry):
        sign(fixture_pdf, tmp_path, key_ref(token))
    assert token.logins == []
    sign(fixture_pdf, tmp_path, key_ref(token, allow_final_try=True))
    assert token.logins == [PIN]


def test_login_to_a_locked_token_is_not_tried(throwaway_pfx, module_with) -> None:
    token = FakeToken(throwaway_pfx(), serial=b"A", flags=int(TokenFlag.USER_PIN_LOCKED))
    module_with(FakeSlot(token, slot_id=0))
    with pytest.raises(PinLocked):
        tokens.login_and_list("vendor.dll", 0, "A", "1111", allow_final_try=True)
    assert token.opens == 0


def test_pin_pad_token_can_list_after_logging_in_on_the_device(throwaway_pfx, module_with) -> None:
    token = FakeToken(
        throwaway_pfx(),
        serial=b"A",
        hides_certs=True,
        flags=int(TokenFlag.PROTECTED_AUTHENTICATION_PATH),
    )
    module_with(FakeSlot(token, slot_id=0))
    (listed,) = tokens.list_tokens("vendor.dll")
    assert listed.protected_auth and listed.certificates == ()
    assert token.logins == []
    info = tokens.login_and_list("vendor.dll", 0, "A", None, protected_auth=True)
    assert token.logins == [PROTECTED_AUTH] and len(info.certificates) == 1


def test_one_bad_slot_doesnt_hide_the_other_tokens(throwaway_pfx, module_with) -> None:
    class BrokenSlot(FakeSlot):
        def get_token(self) -> FakeToken:
            raise p11e.TokenNotRecognised()

    good = FakeToken(throwaway_pfx(), serial=b"A")
    module_with(BrokenSlot(good, slot_id=0), FakeSlot(good, slot_id=1))
    assert [t.slot_id for t in tokens.list_tokens("vendor.dll")] == [1]


def test_public_session_refused_lists_the_token_without_certificates(
    throwaway_pfx, module_with
) -> None:
    token = FakeToken(throwaway_pfx(), serial=b"A", open_error=p11e.GeneralError(), right_pin="1")
    token.open_error = None
    original = token.open

    def refuse_public(user_pin: Any = None) -> FakeSession:
        if user_pin is None:
            raise p11e.UserNotLoggedIn()
        return original(user_pin)

    token.open = refuse_public  # type: ignore[method-assign]
    module_with(FakeSlot(token, slot_id=0))
    (listed,) = tokens.list_tokens("vendor.dll")
    assert listed.serial == "A" and listed.certificates == ()


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


# -- loading the module -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "path", ["vendor.dll", r"..\vendor.dll", r"\\server\share\vendor.dll", "//server/share/v.dll"]
)
def test_module_must_be_a_full_local_path(path: str) -> None:
    with pytest.raises(SigningError, match="full path on this computer"):
        tokens.list_tokens(path)


def test_module_spellings_are_one_module(monkeypatch, tmp_path: Path) -> None:
    """python-pkcs11 caches by string: two spellings would call C_Initialize twice."""
    import pkcs11

    loaded: list[str] = []
    monkeypatch.setattr(pkcs11, "lib", lambda path: loaded.append(path) or FakeLib())
    monkeypatch.setattr(tokens, "_preload", lambda _path: None)
    base = tmp_path.resolve()
    tokens.list_tokens(str(base / "a" / ".." / "vendor.dll"))
    tokens.list_tokens(str(base / "vendor.dll"))
    assert len(set(loaded)) == 1


def test_missing_module_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(SigningError, match="64-bit"):
        tokens.list_tokens(str(tmp_path / "does-not-exist-pkcs11.dll"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL loading")
def test_dll_that_isnt_a_module_is_a_clear_error() -> None:
    from pathlib import Path as P

    with pytest.raises(SigningError, match="64-bit"):
        tokens.list_tokens(str(P(r"C:\Windows\System32\version.dll")))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL loading")
def test_module_is_preloaded_with_its_own_folder_searched(monkeypatch, tmp_path: Path) -> None:
    """python-pkcs11 uses plain LoadLibraryW, which misses a vendor's side-by-side DLLs."""
    import ctypes

    import pkcs11

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(ctypes, "WinDLL", lambda p, winmode=0: calls.append((p, winmode)))
    monkeypatch.setattr(pkcs11, "lib", lambda _p: FakeLib())
    path = tmp_path.resolve() / "vendor.dll"
    tokens.list_tokens(str(path))
    assert calls[0][1] == tokens.LOAD_WITH_ALTERED_SEARCH_PATH
    assert calls[0][0] == tokens.module_path(str(path))
    tokens._preloaded.pop(calls[0][0], None)


# -- signing ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_signs_on_the_token_and_validates(
    kind: str, fixture_pdf, throwaway_pfx, tmp_path: Path, module, signature_status, cms_check
) -> None:
    token = FakeToken(throwaway_pfx(kind))
    module(token)
    target = sign(fixture_pdf, tmp_path, key_ref(token))
    (status,) = signature_status(target)
    assert status.intact and status.valid
    cms_check(target)
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
    (status,) = signature_status(sign(fixture_pdf, tmp_path, key_ref(token)))
    assert status.intact and status.valid
    assert token.mechanisms_used == [used]


def test_always_authenticate_key_gets_the_pin_with_the_signature(
    fixture_pdf, throwaway_pfx, tmp_path, module, signature_status
) -> None:
    token = FakeToken(throwaway_pfx(), always_authenticate=True)
    module(token)
    assert signature_status(sign(fixture_pdf, tmp_path, key_ref(token)))[0].valid
    assert token.sign_kwargs == [{"pin": PIN}]
    assert token.logins == [PIN]


def test_protected_auth_sends_no_pin(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx())
    module(token)
    sign(fixture_pdf, tmp_path, key_ref(token, pin=None, protected_auth=True))
    assert token.logins == [PROTECTED_AUTH]


def test_wrong_pin_is_reported_and_never_retried(
    fixture_pdf, throwaway_pfx, tmp_path, module
) -> None:
    token = FakeToken(throwaway_pfx(), open_error=p11e.PinIncorrect())
    module(token)
    with pytest.raises(WrongPin, match="lock"):
        sign(fixture_pdf, tmp_path, key_ref(token))
    assert token.opens == 1, "a wrong PIN burns an attempt: exactly one, and no other"
    assert not (tmp_path / "signed.pdf").exists()


def test_locked_token_is_not_even_tried(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx(), flags=int(TokenFlag.USER_PIN_LOCKED))
    module(token)
    with pytest.raises(PinLocked, match="vendor"):
        sign(fixture_pdf, tmp_path, key_ref(token))
    assert token.opens == 0


def test_token_locking_during_login(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx(), open_error=p11e.PinLocked())
    module(token)
    with pytest.raises(PinLocked):
        sign(fixture_pdf, tmp_path, key_ref(token))


def test_unplugged_before_signing(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx())
    module(None)
    with pytest.raises(TokenUnavailable, match="plugged in"):
        sign(fixture_pdf, tmp_path, key_ref(token))


def test_other_token_in_the_slot(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx())
    wanted = key_ref(token, token_serial="WANTED")
    module(token)
    with pytest.raises(TokenUnavailable):
        sign(fixture_pdf, tmp_path, wanted)
    assert token.logins == [], "the PIN must never go to a different token"


def test_unplugged_while_signing(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx(), sign_error=p11e.DeviceRemoved())
    module(token)
    with pytest.raises(TokenUnavailable, match="Plug it in"):
        sign(fixture_pdf, tmp_path, key_ref(token))
    assert not (tmp_path / "signed.pdf").exists()


def test_cancelled_on_the_device(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx(), sign_error=p11e.FunctionCancelled())
    module(token)
    with pytest.raises(Cancelled):
        sign(fixture_pdf, tmp_path, key_ref(token))


def test_expired_certificate_is_refused_before_login(
    fixture_pdf, throwaway_pfx, tmp_path, module
) -> None:
    token = FakeToken(throwaway_pfx(days=-3))
    module(token)
    with pytest.raises(SigningRefused, match="expired"):
        sign(fixture_pdf, tmp_path, key_ref(token))
    assert token.logins == [] and token.mechanisms_used == []


def test_certificate_gone_from_the_token(fixture_pdf, throwaway_pfx, tmp_path, module) -> None:
    token = FakeToken(throwaway_pfx())
    other = FakeToken(throwaway_pfx())
    module(token)
    with pytest.raises(TokenUnavailable, match="isn't on this token"):
        sign(fixture_pdf, tmp_path, key_ref(other))


def test_renewed_certificate_on_the_same_key_signs_as_the_one_chosen(
    fixture_pdf, throwaway_pfx, tmp_path, module, signature_status
) -> None:
    """Old and new certificate share a CKA_ID, because they share the key. Picking by CKA_ID
    would sign under whichever comes first."""
    token = FakeToken(throwaway_pfx())
    now = dt.datetime.now(dt.UTC)

    def certificate(name: str, days: int) -> bytes:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(token.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=400))
            .not_valid_after(now + dt.timedelta(days=days))
            .sign(token.key, hashes.SHA256())
            .public_bytes(DER)
        )

    other_valid = certificate("Other Identity", 300)
    chosen = certificate("Chosen Identity", 300)
    old = certificate("Old Expired", -10)
    token.objects[:1] = [FakeToken.certificate_object(der) for der in (old, other_valid, chosen)]
    module(token)
    assert [c.info.subject for c in tokens.list_tokens("x")[0].certificates] == [
        "Old Expired",
        "Other Identity",
        "Chosen Identity",
    ]
    target = sign(fixture_pdf, tmp_path, key_ref(token, certificate=chosen))
    (status,) = signature_status(target)
    assert status.valid
    assert status.signing_cert.subject.native["common_name"] == "Chosen Identity"


def test_thumbprint_identifies_the_certificate(throwaway_pfx) -> None:
    token = FakeToken(throwaway_pfx())
    from asn1crypto import x509 as ax509

    assert key_ref(token).thumbprint == ax509.Certificate.load(token.der).sha1.hex().upper()


# -- one login at a time ----------------------------------------------------------------------
def test_second_signature_on_a_token_in_use_is_refused(
    fixture_pdf, throwaway_pfx, tmp_path, module
) -> None:
    """A login belongs to the whole process: the second would ride on the first one's login,
    with its own PIN unchecked, and closing either session could log the other out."""
    token = FakeToken(throwaway_pfx())
    module(token)
    entered, release = threading.Event(), threading.Event()
    errors: list[Exception] = []
    original = token._sign

    def slow_sign(data: bytes, **kwargs: Any) -> bytes:
        entered.set()
        assert release.wait(20)
        return original(data, **kwargs)

    token.objects[1]._sign = slow_sign

    def first() -> None:
        try:
            sign_file(
                fixture_pdf("images"),
                tmp_path / "1.pdf",
                key_ref(token),
                SignatureOptions(),
                ENGINE,
            )
        except Exception as exc:  # pragma: no cover - reported by the assertion below
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(20)
    try:
        with pytest.raises(SigningError, match="Another signature"):
            sign_file(
                fixture_pdf("images"),
                tmp_path / "2.pdf",
                key_ref(token),
                SignatureOptions(),
                ENGINE,
            )
        with pytest.raises(SigningError, match="Another signature"):
            tokens.login_and_list("vendor.dll", 7, "SER123", PIN)
    finally:
        release.set()
        thread.join(20)
    assert errors == []
    assert token.logins == [PIN], "only the first signature's PIN was sent"
    assert (tmp_path / "1.pdf").exists() and not (tmp_path / "2.pdf").exists()
    # free again afterwards
    sign_file(fixture_pdf("images"), tmp_path / "3.pdf", key_ref(token), SignatureOptions(), ENGINE)


def test_already_logged_in_doesnt_skip_checking_the_pin(
    fixture_pdf, throwaway_pfx, tmp_path, module
) -> None:
    token = FakeToken(throwaway_pfx(), open_error=p11e.UserAlreadyLoggedIn())
    module(token)
    with pytest.raises(SigningError, match="couldn't be checked"):
        sign(fixture_pdf, tmp_path, key_ref(token))
    assert token.opens == 1, "no second session without the PIN"
