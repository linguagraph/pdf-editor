"""Generate the TypeScript side of the web RPC from the Python DTOs (W3.8, W1.2 section 14).

It reads the dataclasses of ``pdfeditor.webapi.dto``, the engine call table ``METHODS`` and the
error codes, and writes ``web/app/src/rpc/generated.ts``: the types of every params and result,
the error code and call name unions, each call's soft deadline class (W1.2 section 10), and one
decoder per dataclass. A decoder checks the JSON shape of a reply, turns each ``$bin`` marker into
the buffer it names, and rejects anything else, so the client never casts an unchecked value. There
is no runtime schema library.

The output is deterministic (sorted names, a fixed layout), so ``--check`` can compare the committed
file with a fresh render and fail on any drift:

    uv run python scripts/make_web_types.py          # rewrite the file
    uv run python scripts/make_web_types.py --check  # exit 1 when it is out of date
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import inspect
import io
import sys
import tokenize
import types
import typing
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Union, get_args, get_origin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pdfeditor.services.optimize import PRESETS  # noqa: E402
from pdfeditor.webapi import METHODS, Method, dto  # noqa: E402
from pdfeditor.webapi import jobs as job_specs  # noqa: E402
from pdfeditor.webapi.errors import ALL_CODES  # noqa: E402

OUTPUT = ROOT / "web" / "app" / "src" / "rpc" / "generated.ts"

# W1.2 section 10: the soft deadline of each class, in milliseconds.
DEADLINE_MS: dict[str, int] = {
    "action": 30_000,
    "edit": 10_000,
    "instant": 3_000,
    "io": 15_000,
    "network": 20_000,
    "render": 5_000,
    "slice": 5_000,
}
# The class of each engine call. The table must name every method in METHODS (checked on render).
# Calls that W1.2 section 10 does not list (session.close, session.unlock_owner, jobs.abort) are
# "instant": they touch no page content.
DEADLINE_CLASS: dict[str, str] = {
    "actions.available": "instant",
    "attachments.get": "io",  # one file, up to MAX_JOB_BYTES, crosses the worker boundary once
    "attachments.list": "instant",
    "comments.flattenable": "instant",
    "content.objects": "render",
    "content.preview_font": "render",
    "fonts.validate": "action",  # reads a whole font file (up to MAX_FONT_BYTES) under Pyodide
    "combine.probe": "action",  # opens a whole PDF (up to MAX_PROBE_BYTES) under Pyodide (W6.3b)
    "actions.redo": "edit",
    "actions.run": "action",
    "actions.undo": "edit",
    "hello": "instant",
    "layers.list": "instant",
    "jobs.abort": "instant",
    "jobs.apply": "action",
    "comments.xfdf_preview": "action",  # parsing up to 8 MiB under Pyodide can pass 3 s
    "jobs.prepare": "action",
    "marks.list": "instant",
    "redaction.list": "instant",
    "comments.list": "instant",
    "render.page": "render",
    "render.tile": "render",
    "save.mark_saved": "instant",
    "save.autosave_bytes": "io",
    "save.save_bytes": "io",
    "session.capabilities": "instant",
    "session.close": "instant",
    "session.fonts": "action",  # Document.fonts() walks every page (W7.6)
    "session.info": "instant",
    "session.links": "render",  # one page's annotations, read once per visible page (W5.3)
    "session.open": "io",
    "session.outline": "instant",
    "split.plan": "instant",
    "session.pages": "instant",
    "session.security": "instant",
    "session.unlock_owner": "instant",
    "system.memory": "instant",
    "text.extract": "slice",
    "text.layer": "render",
    "text.search": "slice",
}
# W1.2 section 10: the io class adds one second per 4 MB of the file. Client-side only.
IO_EXTRA_BYTES = 4 * 1024 * 1024
IO_EXTRA_MS = 1_000

_NONE = type(None)


def _quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _comment_text(text: str) -> str:
    return text.replace("*/", "*\\/")


def _doc_lines(lines: list[str], indent: str) -> list[str]:
    """TSDoc for a list of lines; nothing for an empty list."""
    if not lines:
        return []
    if len(lines) == 1:
        return [f"{indent}/** {_comment_text(lines[0])} */"]
    body = [f"{indent} * {_comment_text(line)}".rstrip() for line in lines]
    return [f"{indent}/**", *body, f"{indent} */"]


def _class_source(cls: type) -> tuple[list[str], dict[str, str]]:
    """The docstring lines of a class and the trailing ``#`` comment of each of its fields."""
    try:
        path = Path(inspect.getsourcefile(cls) or "")
    except TypeError:  # no source file at all (a class made at run time)
        return [], {}
    text = path.read_text(encoding="utf-8")
    comments: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            comments[token.start[0]] = token.string.lstrip("#").strip()
    for node in ast.parse(text).body:
        if not (isinstance(node, ast.ClassDef) and node.name == cls.__name__):
            continue
        doc = ast.get_docstring(node, clean=True)
        field_comments: dict[str, str] = {}
        for stmt in node.body:
            is_field = isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            if is_field and stmt.lineno in comments:
                field_comments[stmt.target.id] = comments[stmt.lineno]
        return (doc.splitlines() if doc else []), field_comments
    # Not defined in that file (made by make_dataclass): no docs, no comments.
    return [], {}


class _Types:
    """Collects the dataclasses and enums reachable from the call table, by name."""

    def __init__(self) -> None:
        self.classes: dict[str, type] = {}
        self.enums: dict[str, type[Enum]] = {}
        self._queue: list[type] = []

    def _register(self, tp: type) -> str:
        name = tp.__name__
        is_enum = issubclass(tp, Enum)
        own, other = (self.enums, self.classes) if is_enum else (self.classes, self.enums)
        if name in other or (name in own and own[name] is not tp):
            raise TypeError(f"two different types are named {name}")
        if name not in own:
            if is_enum:
                values = [member.value for member in tp]
                if not all(isinstance(value, str) for value in values):
                    raise TypeError(f"{name} must have str values (W1.2 section 6)")
            own[name] = tp  # type: ignore[assignment]
            self._queue.append(tp)
        return name

    def ts(self, tp: object) -> str:
        """The TypeScript type of a Python type hint (W1.2 section 6)."""
        if tp is _NONE:
            return "null"
        if tp is bool:
            return "boolean"
        if tp is int or tp is float:
            return "number"
        if tp is str:
            return "string"
        if tp is bytes:
            return "ArrayBuffer"
        if tp is object:
            return "unknown"
        if tp is dto.JsonObject:
            return "Record<string, unknown>"
        origin = get_origin(tp)
        args = get_args(tp)
        if origin is Union or origin is types.UnionType:
            rest = [arg for arg in args if arg is not _NONE]
            if len(args) != 2 or len(rest) != 1:
                raise TypeError(f"only X | None is supported, not {tp!r}")
            return f"{self.ts(rest[0])} | null"
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            item = self.ts(args[0])
            return f"({item})[]" if "|" in item else f"{item}[]"
        if origin is dict and args[0] is str:
            return f"Record<string, {self.ts(args[1])}>"
        if isinstance(tp, type) and (issubclass(tp, Enum) or dataclasses.is_dataclass(tp)):
            return self._register(tp)
        raise TypeError(f"no TypeScript form for {tp!r} (W1.2 section 6)")

    def drain(self) -> None:
        """Visits the fields of every registered dataclass, which can register more types."""
        while self._queue:
            tp = self._queue.pop()
            if dataclasses.is_dataclass(tp) and not issubclass(tp, Enum):
                for hint in typing.get_type_hints(tp).values():
                    self.ts(hint)


def _decoder(tp: object, value: str, path: str, ctx: list[int]) -> str:
    """The TypeScript expression that decodes ``value`` (a JS expression) of type ``tp``.

    ``path`` is a JS expression for the field path in error details. ``ctx`` is a one-item
    counter that gives nested callbacks distinct parameter names.
    """
    if tp is bool:
        return f"expectBoolean({value}, {path})"
    if tp is int:
        return f"expectInteger({value}, {path})"
    if tp is float:
        return f"expectNumber({value}, {path})"
    if tp is str:
        return f"expectString({value}, {path})"
    if tp is bytes:
        return f"takeBinary({value}, ctx, {path})"
    if tp is dto.JsonObject:
        return f"expectObject({value}, {path})"
    if tp is object:
        return f"decodeJsonValue({value}, ctx, {path})"
    origin = get_origin(tp)
    args = get_args(tp)
    if origin is Union or origin is types.UnionType:
        rest = [arg for arg in args if arg is not _NONE]
        ctx[0] += 1
        n = ctx[0]
        inner = _decoder(rest[0], f"v{n}", f"p{n}", ctx)
        return f"orNull({value}, {path}, (v{n}, p{n}) => {inner})"
    if origin is tuple:
        ctx[0] += 1
        n = ctx[0]
        inner = _decoder(args[0], f"item{n}", f"`${{{path}}}[${{index{n}}}]`", ctx)
        return f"expectArray({value}, {path}).map((item{n}, index{n}) => {inner})"
    if origin is dict:
        ctx[0] += 1
        n = ctx[0]
        inner = _decoder(args[1], f"item{n}", f"p{n}", ctx)
        return f"decodeRecord({value}, {path}, (item{n}, p{n}) => {inner})"
    if isinstance(tp, type) and issubclass(tp, Enum):
        values = ", ".join(_quote(member.value) for member in tp)
        return f"expectOneOf({value}, [{values}] as const, {path})"
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return f"decode{tp.__name__}({value}, ctx, {path})"
    raise TypeError(f"no decoder for {tp!r}")


def _dataclass_block(name: str, tp: type, types_: _Types) -> tuple[list[str], list[str]]:
    """The interface and the decoder of one dataclass."""
    doc, comments = _class_source(tp)
    hints = typing.get_type_hints(tp)
    fields = dataclasses.fields(tp)
    field_names = [f.name for f in fields]

    interface = _doc_lines(doc, "")
    if not fields:
        interface.append(f"export interface {name} {{}}")
    else:
        interface.append(f"export interface {name} {{")
        for f in fields:
            interface += _doc_lines([comments[f.name]] if f.name in comments else [], "  ")
            interface.append(f"  {f.name}: {types_.ts(hints[f.name])};")
        interface.append("}")

    decoder = [
        f"export function decode{name}(",
        "  value: unknown,",
        "  ctx: DecodeContext,",
        "  path: string,",
        f"): {name} {{",
        "  const o = expectObject(value, path);",
        f"  checkKeys(o, [{', '.join(_quote(n) for n in field_names)}], path);",
    ]
    if not fields:
        decoder.append("  return {};")
    else:
        decoder.append("  return {")
        for f in fields:
            field_path = f"`${{path}}.{f.name}`"
            value = f"field(o, {_quote(f.name)}, path)"
            decoder.append(f"    {f.name}: {_decoder(hints[f.name], value, field_path, [0])},")
        decoder.append("  };")
    decoder.append("}")
    return interface, decoder


def _enum_block(name: str, tp: type[Enum]) -> list[str]:
    doc, _ = _class_source(tp)
    values = " | ".join(_quote(member.value) for member in tp)
    return [*_doc_lines(doc, ""), f"export type {name} = {values};"]


def _is_chunked(params: object) -> bool:
    """A chunked call's params carry a cursor and a budget (W1.2 section 9)."""
    names = {f.name for f in dataclasses.fields(params)}  # type: ignore[arg-type]
    return {"cursor", "budget_ms"} <= names


def _method_hints(method: Method) -> tuple[object, object]:
    """The params type and the result type of one call (the handler's return annotation)."""
    result = typing.get_type_hints(method.handler).get("return")
    if result is None:
        raise TypeError(f"handler {method.handler.__name__} has no return annotation")
    return method.params, result


def _ts_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _reduce_constants() -> list[str]:
    """The reduce_size presets, dpi and quality ranges and space-audit keys the dialog and its
    result read. Taken from the Python tables, so a change there shows up as drift in --check."""
    lines = [
        "/** services.optimize.PRESETS: the reduce presets the dialog seeds its fields from. */",
        "export const REDUCE_PRESETS = {",
    ]
    for item in PRESETS:
        o = item.options.optimize
        fields = {
            "image_dpi": o.image_dpi,
            "jpeg_quality": o.jpeg_quality,
            "grayscale": o.grayscale,
            "subset_fonts": o.subset_fonts,
            "remove_thumbnails": o.remove_thumbnails,
            "remove_metadata": o.remove_metadata,
            "object_streams": item.options.object_streams,
        }
        body = ", ".join(f"{name}: {_ts_value(value)}" for name, value in fields.items())
        lines.append(f"  {_quote(item.key)}: {{ {body} }},")
    dpi_low, dpi_high = job_specs.REDUCE_DPI_RANGE
    quality_low, quality_high = job_specs.REDUCE_QUALITY_RANGE
    keys = ", ".join(_quote(k) for k in job_specs.SPACE_CATEGORY_KEYS)
    lines += [
        "} as const;",
        "/** webapi.jobs REDUCE_DPI_RANGE and REDUCE_QUALITY_RANGE: the spin box limits. */",
        f"export const REDUCE_DPI_RANGE = [{dpi_low}, {dpi_high}] as const;",
        f"export const REDUCE_QUALITY_RANGE = [{quality_low}, {quality_high}] as const;",
        "/** webapi.jobs.SPACE_CATEGORY_KEYS: the stable keys of an audit category, for t(). */",
        f"export const SPACE_CATEGORY_KEYS = [{keys}] as const;",
        "export type SpaceCategoryKey = (typeof SPACE_CATEGORY_KEYS)[number];",
        "",
    ]
    return lines


def render(
    methods: Mapping[str, Method] = METHODS,
    codes: tuple[str, ...] = ALL_CODES,
    deadlines: Mapping[str, str] = DEADLINE_CLASS,
) -> str:
    """The text of ``generated.ts`` for a call table. Deterministic."""
    missing = sorted(set(methods) - set(deadlines))
    if missing:
        raise ValueError(f"no deadline class for {', '.join(missing)} (W1.2 section 10)")
    unknown = sorted(set(deadlines) - set(methods))
    if unknown:
        raise ValueError(f"deadline class for an unknown call: {', '.join(unknown)}")
    for klass in deadlines.values():
        if klass not in DEADLINE_MS:
            raise ValueError(f"unknown deadline class {klass!r}")

    types_ = _Types()
    names = sorted(methods)
    signatures: dict[str, tuple[object, object]] = {}
    for method_name in names:
        params, result = _method_hints(methods[method_name])
        types_.ts(params)
        types_.ts(result)
        signatures[method_name] = (params, result)
    # The change events (W1.2 section 7) are not reachable from a call's result, so they are
    # registered here; their interfaces and decoders are emitted like any other dataclass.
    for kind in sorted(dto.EVENT_TYPES):
        event_class = dto.EVENT_TYPES[kind]
        if not dataclasses.is_dataclass(event_class):
            raise TypeError(f"event {kind!r} must be a dataclass (W1.2 section 7)")
        types_.ts(event_class)
    types_.drain()

    lines: list[str] = [
        "// Generated by scripts/make_web_types.py from src/pdfeditor/webapi (W1.2 section 14).",
        "// Do not edit: run `uv run python scripts/make_web_types.py` after changing a DTO.",
        "// Prettier and ESLint skip this file. The drift check is make_web_types.py --check.",
        "",
        "import {",
        "  checkKeys,",
        "  decodeJsonValue,",
        "  decodeRecord,",
        "  expectArray,",
        "  expectBoolean,",
        "  expectInteger,",
        "  expectNumber,",
        "  expectObject,",
        "  expectOneOf,",
        "  expectString,",
        "  field,",
        "  orNull,",
        "  takeBinary,",
        "  type DecodeContext,",
        "  type Decoder,",
        "} from './shape';",
        "",
        "/** dto.PROTOCOL_VERSION: the reply's `v` must equal it (W1.2 section 3). */",
        f"export const PROTOCOL_VERSION = {dto.PROTOCOL_VERSION};",
        "",
        "/** Every stable error code (W1.2 section 4), sorted. */",
        f"export const ERROR_CODES = [{', '.join(_quote(c) for c in sorted(codes))}] as const;",
        "export type ErrorCode = (typeof ERROR_CODES)[number];",
        "",
        "/** The soft deadline classes (W1.2 section 10), in milliseconds. */",
        "export type DeadlineClass = keyof typeof DEADLINE_MS;",
        "export const DEADLINE_MS = {",
        *[f"  {_quote(k)}: {v}," for k, v in sorted(DEADLINE_MS.items())],
        "} as const;",
        f"/** The io class adds {IO_EXTRA_MS} ms per {IO_EXTRA_BYTES} bytes of the file. */",
        "export const IO_EXTRA = {",
        f"  bytesPerStep: {IO_EXTRA_BYTES},",
        f"  msPerStep: {IO_EXTRA_MS},",
        "} as const;",
        "",
        *_reduce_constants(),
    ]

    blocks = {
        name: _dataclass_block(name, types_.classes[name], types_)
        for name in sorted(types_.classes)
    }
    for name in sorted(types_.enums):
        lines += [*_enum_block(name, types_.enums[name]), ""]
    for name in sorted(types_.classes):
        lines += [*blocks[name][0], ""]

    lines += [
        "/** Each call: its params, result and deadline class. */",
        "export interface Methods {",
    ]
    for method_name in names:
        params, result = signatures[method_name]
        lines.append(
            f"  {_quote(method_name)}: {{ params: {types_.ts(params)}; "
            f"result: {types_.ts(result)}; deadline: {_quote(deadlines[method_name])} }};"
        )
    lines += [
        "}",
        "export type MethodName = keyof Methods;",
        "",
        "/** The deadline class of each call (W1.2 section 10). */",
        "export const METHOD_DEADLINE: { readonly [M in MethodName]: DeadlineClass } = {",
        *[f"  {_quote(n)}: {_quote(deadlines[n])}," for n in names],
        "};",
        "",
    ]

    chunked = [n for n in names if _is_chunked(signatures[n][0])]
    lines += [
        "/** The calls that return one slice of a chunked operation (W1.2 section 9). */",
        f"export const CHUNKED_METHODS = [{', '.join(_quote(n) for n in chunked)}] as const;",
        "export type ChunkedMethod = (typeof CHUNKED_METHODS)[number];",
        "",
        "/** The reply decoder of each call's result: checks the shape and resolves buffers. */",
        "export const RESULT_DECODERS: {",
        "  readonly [M in MethodName]: Decoder<Methods[M]['result']>;",
        "} = {",
    ]
    for method_name in names:
        _, result = signatures[method_name]
        lines.append(f"  {_quote(method_name)}: decode{result.__name__},")  # type: ignore[attr-defined]
    lines += ["};", ""]

    kinds = sorted(dto.EVENT_TYPES)
    event_names = {kind: dto.EVENT_TYPES[kind].__name__ for kind in kinds}
    lines += [
        "/** Every kind of change event (W1.2 section 7), sorted. */",
        f"export const EVENT_KINDS = [{', '.join(_quote(k) for k in kinds)}] as const;",
        "export type RpcEventKind = (typeof EVENT_KINDS)[number];",
        "/** The fields of each event, by kind: its dataclass, without the `kind` key. */",
        "export interface EventPayloads {",
        *[f"  {_quote(k)}: {event_names[k]};" for k in kinds],
        "}",
        "/** A change event of a reply: its kind and the fields of its dataclass. */",
        "export type RpcEvent = {",
        "  [K in RpcEventKind]: { kind: K } & EventPayloads[K];",
        "}[RpcEventKind];",
        "/** The decoder of each event's fields; the caller strips and re-adds the `kind` key. */",
        "export const EVENT_DECODERS: {",
        "  readonly [K in RpcEventKind]: Decoder<EventPayloads[K]>;",
        "} = {",
        *[f"  {_quote(k)}: decode{event_names[k]}," for k in kinds],
        "};",
        "",
    ]

    for name in sorted(types_.classes):
        lines += [*blocks[name][1], ""]

    return "\n".join(lines).rstrip("\n") + "\n"


def _read_text(path: Path) -> str:
    # Git may check the file out with CRLF on Windows; the comparison ignores line endings.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def check(output: Path = OUTPUT) -> int:
    fresh = render()
    if not output.exists():
        print(f"{output} is missing; run scripts/make_web_types.py", file=sys.stderr)
        return 1
    if _read_text(output) != fresh:
        print(
            f"{output} is out of date with src/pdfeditor/webapi; "
            "run `uv run python scripts/make_web_types.py`",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when the file is out of date")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="file to write or check")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(render().encode("utf-8"))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
