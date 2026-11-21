"""The ``combine.probe`` call (W6.3b slice 1): the page count, the encrypted flag and each stable
error code. The bytes travel as one ``$bin`` buffer; the call names no handle, so it gives no
events and keeps nothing in the registry."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.engine.registry import DEFAULT_ENGINE, get_engine
from pdfeditor.webapi import combine, dto, limits
from pdfeditor.webapi.errors import (
    FILE_TOO_LARGE,
    INVALID_ARGS,
    OPEN_FAILED,
    PASSWORD_REQUIRED,
    WRONG_PASSWORD,
    RpcFailure,
)
from pdfeditor.webapi.registry import REGISTRY
from tests.unit.test_webapi_jobs import call, call_raw, error, ok


def _probe(name: str, data: bytes, password: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "data": {"$bin": 0}}
    if password is not None:
        params["password"] = password
    return call("combine.probe", params, [data])


def _engine_pages(path: Path) -> int:
    document = get_engine(DEFAULT_ENGINE).open(path)
    try:
        return document.page_count
    finally:
        document.close()


def test_an_unencrypted_pdf_gives_its_page_count(fixture_pdf: Any) -> None:
    path = Path(fixture_pdf("text_multipage"))
    result = ok(_probe("text_multipage.pdf", path.read_bytes()))
    assert result == {
        "page_count": _engine_pages(path),
        "encrypted": False,
        "needed_password": False,
    }


def test_an_unencrypted_pdf_probed_with_a_password_does_not_need_it(fixture_pdf: Any) -> None:
    data = Path(fixture_pdf("text_multipage")).read_bytes()
    result = ok(_probe("text_multipage.pdf", data, "unused"))
    assert result["needed_password"] is False
    assert result["encrypted"] is False


def test_an_encrypted_pdf_that_opens_without_a_password_is_not_needed_when_none_is_sent(
    fixture_pdf: Any,
) -> None:
    data = Path(fixture_pdf("encrypted_owner_only")).read_bytes()
    result = ok(_probe("encrypted_owner_only.pdf", data))
    assert result == {"page_count": 1, "encrypted": True, "needed_password": False}


def test_an_encrypted_pdf_that_opens_without_a_password_is_not_needed_with_one(
    fixture_pdf: Any,
) -> None:
    data = Path(fixture_pdf("encrypted_owner_only")).read_bytes()
    result = ok(_probe("encrypted_owner_only.pdf", data, "anything"))
    assert result["needed_password"] is False
    assert result["encrypted"] is True


def test_an_encrypted_pdf_without_a_password_is_password_required(fixture_pdf: Any) -> None:
    data = Path(fixture_pdf("encrypted")).read_bytes()
    err = error(_probe("encrypted.pdf", data))
    assert err["code"] == PASSWORD_REQUIRED
    assert err["details"] == {}


def test_a_wrong_password_is_wrong_password(fixture_pdf: Any) -> None:
    data = Path(fixture_pdf("encrypted")).read_bytes()
    assert error(_probe("encrypted.pdf", data, "nope"))["code"] == WRONG_PASSWORD


def test_the_user_password_opens_and_says_it_was_needed(fixture_pdf: Any) -> None:
    data = Path(fixture_pdf("encrypted")).read_bytes()
    result = ok(_probe("encrypted.pdf", data, "user"))
    assert result == {"page_count": 1, "encrypted": True, "needed_password": True}


def test_the_size_is_checked_before_the_bytes_are_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_PROBE_BYTES", 100)

    def must_not_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("an over-size file was opened")

    monkeypatch.setattr(combine, "get_engine", must_not_open)
    data = b"%PDF-" + b"x" * 200
    err = error(_probe("big.pdf", data))
    assert err["code"] == FILE_TOO_LARGE
    assert err["details"] == {"max_bytes": 100, "size": len(data)}


def test_the_default_limit_is_sixty_four_mebibytes() -> None:
    assert limits.MAX_PROBE_BYTES == 64 * 1024 * 1024


def test_garbage_bytes_are_open_failed() -> None:
    assert error(_probe("junk.pdf", b"not a pdf at all"))["code"] == OPEN_FAILED


@pytest.mark.parametrize("name", ["", "x" * 256, "scan.png", "noextension"])
def test_a_name_that_is_not_a_pdf_is_invalid_args(name: str) -> None:
    err = error(_probe(name, b"%PDF-1.4"))
    assert err["code"] == INVALID_ARGS
    assert err["details"] == {"path": "params.name"}


def test_an_over_long_password_is_invalid_args_and_not_echoed(fixture_pdf: Any) -> None:
    data = Path(fixture_pdf("encrypted")).read_bytes()
    secret = "pw-Zq7!x" + "a" * 1100
    reply = _probe("encrypted.pdf", data, secret)
    assert error(reply)["code"] == INVALID_ARGS
    assert error(reply)["details"] == {"path": "params.password"}
    assert "pw-Zq7" not in str(reply)


def test_a_password_of_exactly_the_bound_is_accepted(fixture_pdf: Any) -> None:
    # An unencrypted file opens whatever the password, so a password at the bound is not refused.
    data = Path(fixture_pdf("text_multipage")).read_bytes()
    result = ok(_probe("text_multipage.pdf", data, "p" * combine.MAX_PASSWORD_CHARS))
    assert result["needed_password"] is False


def test_a_password_one_character_over_the_bound_is_invalid_args(fixture_pdf: Any) -> None:
    data = Path(fixture_pdf("text_multipage")).read_bytes()
    err = error(_probe("text_multipage.pdf", data, "p" * (combine.MAX_PASSWORD_CHARS + 1)))
    assert err["code"] == INVALID_ARGS
    assert err["details"] == {"path": "params.password"}


def test_the_call_keeps_nothing_and_leaves_the_engine_lock_free(fixture_pdf: Any) -> None:
    before = len(REGISTRY)
    data = Path(fixture_pdf("text_multipage")).read_bytes()
    ok(_probe("text_multipage.pdf", data))
    assert len(REGISTRY) == before
    free: list[bool] = []

    def try_lock() -> None:
        acquired = ENGINE_LOCK.acquire(blocking=False)
        if acquired:
            ENGINE_LOCK.release()
        free.append(acquired)

    worker = threading.Thread(target=try_lock)
    worker.start()
    worker.join()
    assert free == [True]


def test_no_password_reaches_a_log_record_or_a_reply(
    fixture_pdf: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "pw-Zq7!x"
    data = Path(fixture_pdf("encrypted")).read_bytes()
    reply, _ = call_raw(
        "combine.probe", {"name": "encrypted.pdf", "data": {"$bin": 0}, "password": secret}, [data]
    )
    assert error(reply)["code"] == WRONG_PASSWORD
    assert secret not in str(reply)
    assert all(secret not in record.getMessage() for record in caplog.records)


def test_the_password_required_reply_and_logs_carry_no_password(
    fixture_pdf: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # No password is sent here, so there is no secret to leak; the check guards that this
    # error path adds nothing password-shaped to the reply or to the log.
    caplog.set_level(logging.DEBUG)
    data = Path(fixture_pdf("encrypted")).read_bytes()
    reply, _ = call_raw("combine.probe", {"name": "encrypted.pdf", "data": {"$bin": 0}}, [data])
    assert error(reply)["code"] == PASSWORD_REQUIRED
    assert "password" not in str(error(reply)["details"]).lower()
    assert all("password" not in record.getMessage().lower() for record in caplog.records)


class _SpyEngine:
    """Wraps the real engine and records what each open was given."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.opened: list[object] = []

    def open(self, source: object, password: str | None = None) -> Any:
        self.opened.append(source)
        return self.inner.open(source, password)


def test_the_document_is_opened_from_bytes_not_a_path(
    fixture_pdf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyEngine(get_engine(DEFAULT_ENGINE))
    monkeypatch.setattr(combine, "get_engine", lambda name: spy)
    data = Path(fixture_pdf("text_multipage")).read_bytes()
    ok(_probe("text_multipage.pdf", data))
    assert len(spy.opened) == 1
    assert isinstance(spy.opened[0], bytes)
    assert spy.opened[0] == data


class _FakeDocument:
    def __init__(self, pages: int | BaseException) -> None:
        self._pages = pages
        self.closed = False

    @property
    def page_count(self) -> int:
        if isinstance(self._pages, BaseException):
            raise self._pages
        return self._pages

    def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self, document: _FakeDocument) -> None:
        self.document = document

    def open(self, source: object, password: str | None = None) -> _FakeDocument:
        return self.document


def test_the_document_is_closed_when_reading_its_pages_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _FakeDocument(RuntimeError("engine blew up"))
    monkeypatch.setattr(combine, "get_engine", lambda name: _FakeEngine(document))
    params = dto.CombineProbeParams(name="x.pdf", data=b"%PDF-1.4")
    with pytest.raises(RuntimeError, match="engine blew up"):
        combine.probe(params)
    assert document.closed is True


def test_the_document_is_closed_on_the_zero_page_open_failed_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _FakeDocument(0)
    monkeypatch.setattr(combine, "get_engine", lambda name: _FakeEngine(document))
    params = dto.CombineProbeParams(name="x.pdf", data=b"%PDF-1.4")
    with pytest.raises(RpcFailure) as failure:
        combine.probe(params)
    assert failure.value.code == OPEN_FAILED
    assert document.closed is True
