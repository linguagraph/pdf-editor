"""The Combine probe of the web build (W6.3b, slice 1): ``combine.probe`` counts a PDF's pages and
says whether it is encrypted, so the Combine dialog can show each row's count and the result's
preview before anything is merged.

It is read-only. The bytes are opened from memory (never a path), no handle or session is made, and
nothing is kept: the engine lock is held only around the open, the reads and the close. The
password, when one is sent, is passed to the open once and is never put in a reply, an error or a
log record.
"""

from __future__ import annotations

from pathlib import Path

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.engine.base import Document, Engine, EngineError, PasswordRequired
from pdfeditor.engine.registry import DEFAULT_ENGINE, get_engine
from pdfeditor.model.metadata import EncryptionMethod
from pdfeditor.webapi import dto, limits
from pdfeditor.webapi.errors import (
    FILE_TOO_LARGE,
    INVALID_ARGS,
    OPEN_FAILED,
    PASSWORD_REQUIRED,
    WRONG_PASSWORD,
    RpcFailure,
)

MAX_NAME_CHARS = 255
MAX_PASSWORD_CHARS = 1024  # the bound a source password takes too (W6.3b section 3)


def probe(params: dto.CombineProbeParams) -> dto.CombineProbeResult:
    """Count the pages of one PDF from its bytes, read-only (W6.3b section 2).

    The checks that need no engine (name, suffix, password length, size) run first, and the size
    check comes before anything reads the bytes. A file that will not open is OPEN_FAILED; one
    that needs a password the caller did not send is PASSWORD_REQUIRED, and one that a sent
    password does not open is WRONG_PASSWORD.
    """
    _check_name(params.name)
    if params.password is not None and len(params.password) > MAX_PASSWORD_CHARS:
        raise RpcFailure(
            INVALID_ARGS,
            f"params.password: at most {MAX_PASSWORD_CHARS} characters",
            {"path": "params.password"},
        )
    size = len(params.data)  # checked before anything reads the bytes
    if size > limits.MAX_PROBE_BYTES:
        raise RpcFailure(
            FILE_TOO_LARGE,
            "the file is too large to count its pages here",
            {"max_bytes": limits.MAX_PROBE_BYTES, "size": size},
        )
    engine = get_engine(DEFAULT_ENGINE)
    with ENGINE_LOCK:
        document = _open(engine, params)
        try:
            pages = document.page_count
            if pages == 0:
                raise RpcFailure(OPEN_FAILED, "the file has no readable pages", {"pages": 0})
            encrypted = document.info().encryption is not EncryptionMethod.NONE
            needed = encrypted and params.password is not None and _needs_password(engine, params)
        finally:
            document.close()
    return dto.CombineProbeResult(page_count=pages, encrypted=encrypted, needed_password=needed)


def _needs_password(engine: Engine, params: dto.CombineProbeParams) -> bool:
    """Whether the file will not open without a password, asked of a second open with none. The
    caller holds the lock. Only an encrypted file that was opened with a password gets here: the
    password was sent, so it may have been needed or not."""
    try:
        document = engine.open(params.data, None)
    except PasswordRequired:
        return True
    except EngineError:
        return False
    document.close()
    return False


def _check_name(name: str) -> None:
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise RpcFailure(
            INVALID_ARGS,
            f"params.name: 1 to {MAX_NAME_CHARS} characters",
            {"path": "params.name"},
        )
    if Path(name).suffix.lower() != ".pdf":
        raise RpcFailure(INVALID_ARGS, "params.name: not a PDF file", {"path": "params.name"})


def _open(engine: Engine, params: dto.CombineProbeParams) -> Document:
    """Open the bytes; the caller holds the lock and closes the document. The password is passed
    once, and the errors never carry it (``from None`` also drops the engine's chained cause)."""
    try:
        return engine.open(params.data, params.password)
    except PasswordRequired:
        if params.password is None:
            raise RpcFailure(PASSWORD_REQUIRED, "the file is password protected") from None
        raise RpcFailure(WRONG_PASSWORD, "the password is wrong") from None
    except EngineError:
        raise RpcFailure(OPEN_FAILED, "this file is not a readable PDF") from None
