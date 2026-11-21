"""The browser RPC facade (W3.2): one entry point, :func:`dispatch`, that the worker's JS calls.

It is plain Python with no JS imports, so the host CPython runs it in the tests. Requests and
replies are JSON strings; the bytes they carry travel as a parallel list of buffers. Nothing here
raises into the caller: every failure becomes a reply with an error code (W1.2 section 4).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from pdfeditor.core.session import DocumentSession
from pdfeditor.webapi import (
    actions,
    attachments,
    combine,
    content,
    dto,
    fonts,
    jobs,
    layers,
    memory,
    render,
    save,
    session,
    text,
)
from pdfeditor.webapi.dto import PROTOCOL_VERSION, BufferTable, from_json, to_json
from pdfeditor.webapi.errors import INVALID_REQUEST, PROTOCOL_MISMATCH, RpcFailure, failure_for
from pdfeditor.webapi.events import CallRecorder, call_events, event, state_of
from pdfeditor.webapi.limits import MAX_JSON_DEPTH
from pdfeditor.webapi.registry import REGISTRY

log = logging.getLogger(__name__)

ENGINE_NAME = "mupdf"
PLATFORM_NAME = "web"
_ENVELOPE_KEYS = frozenset({"v", "id", "method", "params"})
# Calls that name a handle but must not be tracked: session.close forgets its document (its
# ``closed`` event is built from the success instead).
_UNTRACKED = frozenset({"session.close"})


@dataclass(frozen=True, slots=True)
class Method:
    params: type
    handler: Callable[..., object]
    # True when the handler also gets the call's buffer table, because it converts part of its
    # params itself (actions.run). Such a handler runs before the unused-buffer check.
    takes_buffers: bool = False


def _hello(params: dto.Empty) -> dto.HelloResult:
    return dto.HelloResult(protocol=PROTOCOL_VERSION, engine=ENGINE_NAME, platform=PLATFORM_NAME)


METHODS: dict[str, Method] = {
    "hello": Method(dto.Empty, _hello),
    "session.open": Method(dto.OpenParams, session.open_document),
    "session.close": Method(dto.HandleParams, session.close_document),
    "session.info": Method(dto.HandleParams, session.document_info),
    "session.pages": Method(dto.HandleParams, session.document_pages),
    "session.capabilities": Method(dto.HandleParams, session.document_capabilities),
    "session.unlock_owner": Method(dto.UnlockParams, session.unlock_owner),
    "session.outline": Method(dto.HandleParams, session.document_outline),
    "session.security": Method(dto.HandleParams, session.document_security),
    "session.fonts": Method(dto.HandleParams, session.document_fonts),
    "session.links": Method(dto.PageParams, session.page_links),
    "text.layer": Method(dto.PageParams, text.text_layer),
    "text.extract": Method(dto.ExtractParams, text.extract),
    "text.search": Method(dto.SearchParams, text.search),
    "render.page": Method(dto.RenderPageParams, render.render_page),
    "render.tile": Method(dto.RenderTileParams, render.render_tile),
    "actions.run": Method(dto.RunParams, actions.run_action, takes_buffers=True),
    "actions.undo": Method(dto.UndoParams, actions.undo),
    "actions.redo": Method(dto.HandleParams, actions.redo),
    "actions.available": Method(dto.HandleParams, actions.available),
    "comments.flattenable": Method(dto.HandleParams, actions.flattenable),
    "comments.xfdf_preview": Method(dto.XfdfPreviewParams, actions.xfdf_preview),
    "fonts.validate": Method(dto.FontValidateParams, fonts.validate),
    "combine.probe": Method(dto.CombineProbeParams, combine.probe),
    "content.objects": Method(dto.PageParams, content.list_objects),
    "content.preview_font": Method(dto.PreviewFontParams, content.preview_font),
    "attachments.list": Method(dto.HandleParams, attachments.list_attachments),
    "attachments.get": Method(dto.AttachmentParams, attachments.get_attachment),
    "layers.list": Method(dto.HandleParams, layers.list_layers),
    "marks.list": Method(dto.HandleParams, actions.list_marks),
    "redaction.list": Method(dto.HandleParams, actions.list_redactions),
    "comments.list": Method(dto.HandleParams, actions.list_comments),
    "save.save_bytes": Method(dto.SaveParams, save.save_bytes),
    "save.mark_saved": Method(dto.HandleParams, save.mark_saved),
    "save.autosave_bytes": Method(dto.HandleParams, save.autosave_bytes),
    "system.memory": Method(dto.Empty, memory.system_memory),
    "split.plan": Method(dto.SplitPlanParams, jobs.split_plan),
    "jobs.prepare": Method(dto.JobPrepareParams, jobs.prepare, takes_buffers=True),
    "jobs.apply": Method(dto.JobApplyParams, jobs.apply),
    "jobs.abort": Method(dto.JobAbortParams, jobs.abort),
}


class _CallScope:
    """The events of one call (W1.2 section 7): the open document the call names, its state
    before the call, and the recorder that the registry arms while the handler runs.

    A call with no open handle (or session.close) has no scope and gives no events. The state
    is read before the recorder is armed, and the recorder is armed last, so a failure while
    starting the scope leaves nothing armed.
    """

    def __init__(self) -> None:
        self._method = ""
        self._handle: str | None = None
        self._session: DocumentSession | None = None
        self._recorder: CallRecorder | None = None
        self._before: dto.DocStateDto | None = None
        self._armed = ExitStack()

    def begin(self, method: str, params: object) -> None:
        self._method = method
        handle = getattr(params, "handle", None)
        if not isinstance(handle, str):
            return
        self._handle = handle
        if method in _UNTRACKED:
            return
        session_ = REGISTRY.find(handle)
        if session_ is None:
            return
        with session_.lock:
            before = state_of(session_)
        self._session, self._before = session_, before
        recorder = self._armed.enter_context(REGISTRY.recording(handle))
        self._recorder = recorder

    def end(self) -> None:
        """Disarm the recorder, on success and on failure alike."""
        self._armed.close()

    def events(self, result: object | None, out: list[bytes]) -> list[dict[str, Any]]:
        """The events of the call. ``result`` is None for a failed call. A failure while the
        events are built is logged and gives none, so the call's own error is never masked."""
        if self._method == "session.close":
            if result is None or self._handle is None:
                return []
            return [event("closed", dto.ClosedEvent(self._handle), out)]
        if self._session is None or self._recorder is None or self._before is None:
            return []
        try:
            with self._session.lock:
                return call_events(
                    self._handle or "",
                    self._session,
                    self._recorder,
                    self._before,
                    result,
                    out,
                )
        except Exception:
            log.exception("could not build the events of %s", self._method)
            return []


def _failure(request_id: object, exc: Exception, events: list[dict[str, Any]]) -> dict[str, Any]:
    failure = failure_for(exc)
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {"code": failure.code, "message": failure.message, "details": failure.details},
        "events": events,
    }


def dispatch(
    request_json: str, buffers: list[bytes], methods: Mapping[str, Method] = METHODS
) -> tuple[str, list[bytes]]:
    """Handle one request. Returns the reply JSON and the reply's buffers, in ``$bin`` order.

    ``methods`` is the table of this worker: the engine's by default, the job worker's own in
    :mod:`pdfeditor.webapi.jobworker`.

    Never raises: a malformed envelope, an unknown method, bad arguments, a document error or a
    bug all come back as a failure reply (``"ok": false`` with ``error`` and ``events``). A failed
    call carries the events it caused before it failed (W1.2 section 2).
    """
    request_id: object = None
    scope = _CallScope()
    try:
        data = _load(request_json)
        if isinstance(data, dict) and _is_id(data.get("id")):
            request_id = data["id"]  # echoed even in a failure about the rest of the envelope
        reply, out = _run(data, buffers, methods, scope)
    except Exception as exc:  # every failure becomes a reply; the worker must not die of one
        reply = _failure(request_id, exc, scope.events(None, []))
        out = []
    return json.dumps(reply, allow_nan=False, separators=(",", ":")), out


# Escape pairs, quotes and brackets: the only characters the depth scan needs to see.
_JSON_TOKEN = re.compile(r'\\.|["\[\]{}]', re.DOTALL)


def _check_depth(request_json: str) -> None:
    """Refuse a request nested deeper than MAX_JSON_DEPTH, before the decoder recurses into it.

    An iterative scan (no recursion), so it cannot itself overflow. Brackets inside strings do
    not count, and a backslash escapes the character after it.
    """
    depth = 0
    in_string = False
    for match in _JSON_TOKEN.finditer(request_json):
        token = match.group()
        if token == '"':
            in_string = not in_string
        elif in_string or token.startswith("\\"):
            continue
        elif token in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise RpcFailure(INVALID_REQUEST, "the request is nested too deeply")
        elif token in "]}":
            depth -= 1


def _load(request_json: str) -> object:
    _check_depth(request_json)
    try:
        return json.loads(request_json, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # a second line: nesting the scan let through
        raise RpcFailure(INVALID_REQUEST, "the request is not valid JSON") from exc


def _is_id(value: object) -> bool:
    # An integer within the JSON-safe range (W1.2 section 6); bool is an int subclass, so it is
    # excluded by type.
    return type(value) is int and abs(value) <= dto.MAX_SAFE_INT


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not allowed in JSON")


def _run(
    data: object, buffers: list[bytes], methods: Mapping[str, Method], scope: _CallScope
) -> tuple[dict[str, object], list[bytes]]:
    if not isinstance(data, dict) or set(data) != _ENVELOPE_KEYS:
        raise RpcFailure(INVALID_REQUEST, "the request needs exactly v, id, method and params")
    version = data["v"]
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise RpcFailure(
            PROTOCOL_MISMATCH,
            f"the request uses protocol {version!r}; this worker speaks {PROTOCOL_VERSION}",
        )
    request_id = data["id"]
    method_name = data["method"]
    params = data["params"]
    if not _is_id(request_id) or not isinstance(method_name, str):
        raise RpcFailure(INVALID_REQUEST, "id must be an integer and method a string")
    if not isinstance(params, dict):
        raise RpcFailure(INVALID_REQUEST, "params must be an object")
    method = methods.get(method_name)
    if method is None:
        raise RpcFailure(INVALID_REQUEST, "unknown method")

    table = BufferTable(buffers)
    typed: Any = from_json(method.params, params, table, "params")
    scope.begin(method_name, typed)
    try:
        if method.takes_buffers:
            result = method.handler(typed, table)
            table.check_all_used()
        else:
            table.check_all_used()
            result = method.handler(typed)
    finally:
        scope.end()  # the recorder is disarmed on a failure too; the failure reads its events

    out: list[bytes] = []
    result_json = to_json(result, out)
    # The events are built from what the recorder heard and the state before and after the call
    # (W1.2 section 7); a read-only call gets an empty list.
    events = scope.events(result, out)
    reply: dict[str, object] = {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": True,
        "result": result_json,
        "events": events,
    }
    return reply, out
