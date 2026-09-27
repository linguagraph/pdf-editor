"""Tokenizer, parser and writer for PDF content streams (PDF 32000-1 7.2, 7.8, 8.9.7).

Engine-neutral and pure Python: backends hand over raw stream bytes and get bytes back.
``parse(write(ops)) == ops`` holds for every operation list (see the property tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"
_REGULAR_END = WHITESPACE + DELIMITERS


class Name(str):
    """A PDF name object (stored decoded, without the leading slash)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Name({str.__repr__(self)})"


class HexString(bytes):
    """A string written in hex form ``<...>`` (kept so the writer preserves the form)."""

    __slots__ = ()


Operand: TypeAlias = (
    int | float | bool | Name | bytes | list["Operand"] | dict[Name, "Operand"] | None
)


@dataclass
class Operation:
    operator: str
    operands: list[Operand] = field(default_factory=list)
    inline_data: bytes | None = None  # BI ... ID <data> EI: operands = [image dict]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Operation):
            return NotImplemented
        return (
            self.operator == other.operator
            and _same(self.operands, other.operands)
            and self.inline_data == other.inline_data
        )


def _same(a: Operand, b: Operand) -> bool:
    """Structural equality that treats 1 and 1.0 alike but keeps names and strings apart."""
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b) < 1e-4
    if isinstance(a, Name) != isinstance(b, Name):
        return False
    return a == b


class ContentSyntaxError(ValueError):
    pass


# -- lexer ------------------------------------------------------------------------------------
class _Lexer:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def skip_space(self) -> None:
        data, n = self.data, len(self.data)
        while self.pos < n:
            c = data[self.pos]
            if c in WHITESPACE:
                self.pos += 1
            elif c == 0x25:  # % comment to end of line
                while self.pos < n and data[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                break

    def at_end(self) -> bool:
        self.skip_space()
        return self.pos >= len(self.data)

    def read_regular(self) -> bytes:
        start = self.pos
        data, n = self.data, len(self.data)
        while self.pos < n and data[self.pos] not in _REGULAR_END:
            self.pos += 1
        return data[start : self.pos]

    def next(self) -> object:
        """The next object, or an ``_Op`` for an operator keyword."""
        self.skip_space()
        data = self.data
        if self.pos >= len(data):
            raise ContentSyntaxError("unexpected end of content stream")
        c = data[self.pos]
        if c == 0x2F:  # /
            self.pos += 1
            return Name(_decode_name(self.read_regular()))
        if c == 0x28:  # (
            return self.read_literal()
        if c == 0x3C:  # <
            if data[self.pos : self.pos + 2] == b"<<":
                self.pos += 2
                return self.read_dict()
            return self.read_hex()
        if c == 0x5B:  # [
            self.pos += 1
            items: list[Operand] = []
            while True:
                self.skip_space()
                if self.pos < len(data) and data[self.pos] == 0x5D:
                    self.pos += 1
                    return items
                item = self.next()
                if isinstance(item, _Op):
                    raise ContentSyntaxError(f"operator {item.name!r} inside an array")
                items.append(item)  # type: ignore[arg-type]
        if c in b")>]}":
            self.pos += 1
            raise ContentSyntaxError(f"unexpected {chr(c)!r}")
        if c == 0x7B or c == 0x7D:  # { } (PostScript calculator; not valid in content)
            self.pos += 1
            raise ContentSyntaxError("braces are not allowed in content streams")
        token = self.read_regular()
        if not token:
            self.pos += 1
            raise ContentSyntaxError(f"unexpected byte {c:#x}")
        number = _number(token)
        if number is not None:
            return number
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None
        return _Op(token.decode("latin-1"))

    def read_dict(self) -> dict[Name, Operand]:
        out: dict[Name, Operand] = {}
        while True:
            self.skip_space()
            if self.data[self.pos : self.pos + 2] == b">>":
                self.pos += 2
                return out
            key = self.next()
            if not isinstance(key, Name):
                raise ContentSyntaxError("dictionary key must be a name")
            value = self.next()
            if isinstance(value, _Op):
                raise ContentSyntaxError("operator inside a dictionary")
            out[key] = value  # type: ignore[assignment]

    def read_literal(self) -> bytes:
        data, n = self.data, len(self.data)
        self.pos += 1  # (
        depth = 1
        out = bytearray()
        while self.pos < n:
            c = data[self.pos]
            self.pos += 1
            if c == 0x5C:  # backslash
                if self.pos >= n:
                    break
                e = data[self.pos]
                self.pos += 1
                simple = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C}
                if e in simple:
                    out.append(simple[e])
                elif e in b"()\\":
                    out.append(e)
                elif e in b"\r\n":  # line continuation
                    if e == 0x0D and self.pos < n and data[self.pos] == 0x0A:
                        self.pos += 1
                elif 0x30 <= e <= 0x37:  # octal, up to 3 digits
                    digits = bytes([e])
                    while len(digits) < 3 and self.pos < n and 0x30 <= data[self.pos] <= 0x37:
                        digits += bytes([data[self.pos]])
                        self.pos += 1
                    out.append(int(digits, 8) & 0xFF)
                else:
                    out.append(e)  # unknown escape: the backslash is ignored
            elif c == 0x28:
                depth += 1
                out.append(c)
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    return bytes(out)
                out.append(c)
            else:
                out.append(c)
        raise ContentSyntaxError("unterminated string")

    def read_hex(self) -> HexString:
        end = self.data.find(b">", self.pos)
        if end < 0:
            raise ContentSyntaxError("unterminated hex string")
        digits = bytes(b for b in self.data[self.pos + 1 : end] if b not in WHITESPACE)
        self.pos = end + 1
        if len(digits) % 2:
            digits += b"0"
        try:
            return HexString(bytes.fromhex(digits.decode("ascii")))
        except ValueError as exc:
            raise ContentSyntaxError(f"bad hex string: {exc}") from exc

    def read_inline_image(self) -> bytes:
        """Data after ``ID``: up to an ``EI`` that stands alone between whitespace."""
        data, n = self.data, len(self.data)
        self.pos += 1  # the single whitespace byte after ID
        start = self.pos
        search = start
        while True:
            i = data.find(b"EI", search)
            if i < 0:
                raise ContentSyntaxError("inline image without EI")
            before_ok = i > start and data[i - 1] in WHITESPACE
            after_ok = i + 2 >= n or data[i + 2] in WHITESPACE
            if before_ok and after_ok:
                self.pos = i + 2
                return data[start : i - 1]
            search = i + 1


class _Op:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def _number(token: bytes) -> int | float | None:
    if not token or token[0] not in b"+-.0123456789":
        return None
    try:
        if b"." in token:
            return float(token)
        return int(token)
    except ValueError:
        return None


def _decode_name(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x23 and i + 2 < len(raw):  # #xx escape
            try:
                out.append(int(raw[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(raw[i])
        i += 1
    return out.decode("latin-1")


def parse(data: bytes) -> list[Operation]:
    """Parse a content stream into operations (raises ContentSyntaxError on bad syntax)."""
    lexer = _Lexer(data)
    ops: list[Operation] = []
    operands: list[Operand] = []
    while not lexer.at_end():
        item = lexer.next()
        if not isinstance(item, _Op):
            operands.append(item)  # type: ignore[arg-type]
            continue
        if item.name == "BI":
            image: dict[Name, Operand] = {}
            while True:
                lexer.skip_space()
                token = lexer.next()
                if isinstance(token, _Op):
                    if token.name != "ID":
                        raise ContentSyntaxError(f"unexpected {token.name!r} in inline image")
                    break
                if not isinstance(token, Name):
                    raise ContentSyntaxError("inline image key must be a name")
                value = lexer.next()
                if isinstance(value, _Op):
                    raise ContentSyntaxError("operator inside inline image dictionary")
                image[token] = value  # type: ignore[assignment]
            ops.append(Operation("BI", [image], lexer.read_inline_image()))
            operands = []
            continue
        ops.append(Operation(item.name, operands))
        operands = []
    if operands:
        raise ContentSyntaxError("operands without an operator at the end of the stream")
    return ops


# -- writer -----------------------------------------------------------------------------------
def fmt_number(v: float) -> str:
    """Fixed-point PDF number (PDF has no exponent notation)."""
    if isinstance(v, bool):
        raise TypeError("bool is not a number")
    if isinstance(v, int):
        return str(v)
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


_NAME_SAFE = set(range(0x21, 0x7F)) - set(DELIMITERS) - {0x23}


def _name(name: str) -> bytes:
    raw = name.encode("latin-1")
    return b"/" + b"".join(bytes([b]) if b in _NAME_SAFE else b"#%02X" % b for b in raw)


def _literal(value: bytes) -> bytes:
    out = bytearray(b"(")
    for b in value:
        if b in b"()\\":
            out += b"\\" + bytes([b])
        elif b == 0x0A:
            out += b"\\n"
        elif b == 0x0D:
            out += b"\\r"
        elif b < 0x20 or b > 0x7E:
            out += b"\\%03o" % b
        else:
            out.append(b)
    out += b")"
    return bytes(out)


def write_operand(value: Operand) -> bytes:
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int | float):
        return fmt_number(value).encode()
    if isinstance(value, Name):
        return _name(value)
    if isinstance(value, HexString):
        return b"<" + value.hex().upper().encode() + b">"
    if isinstance(value, bytes):
        return _literal(value)
    if isinstance(value, list):
        return b"[" + b" ".join(write_operand(v) for v in value) + b"]"
    if isinstance(value, dict):
        parts = [_name(k) + b" " + write_operand(v) for k, v in value.items()]
        return b"<<" + b" ".join(parts) + b">>"
    raise TypeError(f"can't write {type(value).__name__} in a content stream")


def write(ops: list[Operation]) -> bytes:
    lines: list[bytes] = []
    for op in ops:
        if op.operator == "BI":
            image = op.operands[0] if op.operands else {}
            assert isinstance(image, dict)
            pairs = b" ".join(_name(k) + b" " + write_operand(v) for k, v in image.items())
            lines.append(b"BI " + pairs + b" ID " + (op.inline_data or b"") + b"\nEI")
        elif op.operands:
            lines.append(
                b" ".join(write_operand(v) for v in op.operands)
                + b" "
                + op.operator.encode("latin-1")
            )
        else:
            lines.append(op.operator.encode("latin-1"))
    return b"\n".join(lines) + b"\n"
