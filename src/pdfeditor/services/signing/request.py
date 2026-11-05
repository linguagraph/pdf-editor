"""Signature fields for recipients: the sender places them, and each recipient signs their own.

Nothing here sends anything. The sender's file gets empty, named signature fields with a "Sign
here" placeholder, and the sender emails it. Each recipient signs their field in Elanero or in
Acrobat, which replaces the placeholder with the signature.

Like signing, this is an incremental update, so existing signatures keep working. Imported only
when used: pyHanko loads at call time, not at application start.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.misc import PdfError
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.fields import SigFieldSpec, append_signature_field, enumerate_sig_fields
from pyhanko.sign.validation import read_certification_data

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.core.jobs import CancelToken
from pdfeditor.core.safe_output import replaced_atomically
from pdfeditor.engine.base import Engine, SaveOptions
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Rect
from pdfeditor.model.objects import ShapeKind, ShapeSpec
from pdfeditor.model.signing import EmptySignatureField, SignatureRequest
from pdfeditor.services.signing._pdf import (
    _PARSE_ERRORS,
    _authenticate,
    _check_not_locked,
    _fit_text,
    _open_reader,
    _open_writer,
    _PageStamp,
    _PageStampStyle,
    _signature_ok,
    empty_signature_fields,
    page_boxes,
)
from pdfeditor.services.signing.errors import SigningError, SigningRefused, WrongPassword

REQUEST_PREFIX = "SignatureRequest"  # field names: no dots, and no signer's name in them
NOT_PREPARED = "The file was not written."
PLACEMENT_TOLERANCE = 1.0  # points: how far a field may land from the box it was asked for
PLACEHOLDER_TEXT = "Sign here: {name}"
PLACEHOLDER_SIZE = 14.0
CERTIFIED = (
    "This document was certified by its author. A new signature field could make that "
    "certification look invalid in some readers, so the document can't be prepared for "
    "signing. Ask the author for an uncertified copy."
)


def _new_names(reader: PdfFileReader, count: int) -> list[str]:
    """``count`` field names that no existing field uses."""
    used = {name for name, _value, _ref in enumerate_sig_fields(reader, filled_status=None)}
    names: list[str] = []
    n = 1
    while len(names) < count:
        name = f"{REQUEST_PREFIX}{n}"
        if name not in used:
            names.append(name)
        n += 1
    return names


def placeholder_pdf(engine: Engine, width: float, height: float, name: str) -> bytes:
    """The "Sign here" box shown until the field is signed: a thin frame and the signer's name,
    typeset by the engine so that any script works (the same as the signature's appearance)."""
    with ENGINE_LOCK:
        doc = engine.new_document()
        try:
            doc.insert_blank_page(0, width, height)
            page = doc.page(0)
            margin = min(2.0, height / 8)
            frame = Rect(margin, margin, width - margin, height - margin)
            page.add_shape(
                ShapeSpec(
                    kind=ShapeKind.RECTANGLE,
                    rect=frame,
                    stroke=Color(0.55, 0.55, 0.55),
                    width=0.75,
                )
            )
            inner = Rect(frame.x0 + 4, frame.y0 + 2, frame.x1 - 4, frame.y1 - 2)
            _fit_text(engine, page, inner, PLACEHOLDER_TEXT.format(name=name), PLACEHOLDER_SIZE)
            return doc.to_bytes(SaveOptions())
        finally:
            doc.close()


def _write_fields(
    writer: IncrementalPdfFileWriter,
    engine: Engine,
    requests: Sequence[SignatureRequest],
    names: Sequence[str],
) -> None:
    for request, name in zip(requests, names, strict=True):
        boxes = page_boxes(writer.prev, request.page)
        box = boxes.visible_to_user(request.rect)
        append_signature_field(
            writer,
            SigFieldSpec(name, on_page=request.page, box=box),  # type: ignore[arg-type]
        )
        widget = _field_object(writer, name)
        widget[generic.pdf_name("/TU")] = generic.TextStringObject(f"Signature of {request.name}")
        placeholder = _PageStampStyle(
            pdf=placeholder_pdf(engine, request.rect.width, request.rect.height, request.name),
            rotate=boxes.rotate,
        )
        _PageStamp(writer, placeholder).apply_appearance(widget)


def _field_object(writer: IncrementalPdfFileWriter, name: str) -> Any:
    for field_name, _value, ref in enumerate_sig_fields(writer, filled_status=False):
        if field_name == name:
            return ref.get_object()
    raise SigningError(f"The signature field “{name}” couldn't be added. {NOT_PREPARED}")


def _prepare_open_file(
    fh: BinaryIO,
    target: Path,
    requests: Sequence[SignatureRequest],
    engine: Engine,
    password: str | None,
    token: CancelToken,
) -> list[str]:
    writer = _open_writer(fh)
    if not _authenticate(writer, password):
        raise WrongPassword("This document needs its password to be prepared for signing.")
    reader = writer.prev
    if read_certification_data(reader) is not None:
        raise SigningRefused(CERTIFIED)
    names = _new_names(reader, len(requests))
    for name in names:
        _check_not_locked(reader, name)
    earlier = [s.field_name for s in reader.embedded_signatures if _signature_ok(s)]
    token.check()
    _write_fields(writer, engine, requests, names)
    token.check()
    with replaced_atomically(target) as tmp:
        with tmp.open("wb") as out:
            writer.write(out)
        token.check()
        _verify_prepared(tmp, password, engine, requests, names, earlier)
        token.check()  # the last moment a cancel can still stop the file from appearing
    return names


def _verify_prepared(
    path: Path,
    password: str | None,
    engine: Engine,
    requests: Sequence[SignatureRequest],
    names: Sequence[str],
    earlier: Sequence[str],
) -> None:
    """Every requested field must be there, empty and where it was asked for, and the
    signatures that held before must still hold. Only then does the file appear."""
    with path.open("rb") as fh:
        reader = _open_reader(fh, password)
        empty = {field.name: field for field in empty_signature_fields(reader)}
        for request, name in zip(requests, names, strict=True):
            field = empty.get(name)
            if field is None or not _same_place(field, request):
                raise SigningError(f"A signature field wasn't placed correctly. {NOT_PREPARED}")
        still = {s.field_name for s in reader.embedded_signatures if _signature_ok(s)}
        lost = [name for name in earlier if name not in still]
        if lost:
            raise SigningError(
                f"Preparing the file would have invalidated the signature “{lost[0]}”. "
                f"{NOT_PREPARED}"
            )
    with ENGINE_LOCK:
        doc = engine.open(path, password)
        try:
            if doc.page_count < 1:
                raise SigningError(f"The prepared file didn't open correctly. {NOT_PREPARED}")
        finally:
            doc.close()


def _same_place(field: EmptySignatureField, request: SignatureRequest) -> bool:
    if field.page != request.page or field.rect is None:
        return False
    got, want = field.rect, request.rect
    return all(
        abs(a - b) <= PLACEMENT_TOLERANCE
        for a, b in (
            (got.x0, want.x0),
            (got.y0, want.y0),
            (got.x1, want.x1),
            (got.y1, want.y1),
        )
    )


def prepare_for_signing(
    source: Path,
    target: Path,
    requests: Sequence[SignatureRequest],
    engine: Engine,
    password: str | None = None,
    token: CancelToken | None = None,
) -> Path:
    """Write ``target``: ``source``'s bytes plus one empty, named signature field per request.

    Refuses a certified or locked document (a new field would invalidate its certification),
    and never changes ``source``. Returns ``target``.
    """
    token = token or CancelToken()
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise SigningRefused("Choose a different file name: the original is kept as it is.")
    if not requests:
        raise SigningRefused("Add at least one signer first.")
    try:
        with source.open("rb") as fh:
            _prepare_open_file(fh, target, requests, engine, password, token)
    except PdfError as exc:
        raise SigningError(f"This PDF can't be prepared for signing: {exc}") from exc
    except OSError as exc:
        raise SigningError(f"Couldn't write {target.name}: {exc}") from exc
    except _PARSE_ERRORS as exc:
        raise SigningError(f"This PDF can't be prepared for signing: {exc}") from exc
    return target
