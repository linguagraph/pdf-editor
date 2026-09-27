"""Parse and write PDF object syntax (PDF 32000-1 7.3), engine-neutral.

Backends that expose dictionary values as PDF text (MuPDF's ``xref_get_key``) use this to
read arrays with inline dictionaries and references, and to write values back safely.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

_WS = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"


@dataclass(frozen=True, slots=True)
class Ref:
    num: int
    gen: int = 0


class Name(str):
    """A name object, stored without the leading slash."""

    __slots__ = ()


PdfValue = (
    int | float | bool | str | bytes | Name | Ref | list["PdfValue"] | dict[str, "PdfValue"] | None
)


class PdfSyntaxError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.i = 0

    def skip(self) -> None:
        d, n = self.d, len(self.d)
        while self.i < n:
            c = d[self.i]
            if c in _WS:
                self.i += 1
            elif c == 0x25:  # % comment
                while self.i < n and d[self.i] not in b"\r\n":
                    self.i += 1
            else:
                return

    def peek(self) -> int | None:
        self.skip()
        return self.d[self.i] if self.i < len(self.d) else None

    def regular(self) -> bytes:
        start = self.i
        while self.i < len(self.d) and self.d[self.i] not in _WS + _DELIM:
            self.i += 1
        return self.d[start : self.i]

    def value(self) -> PdfValue:
        c = self.peek()
        if c is None:
            raise PdfSyntaxError("unexpected end")
        if c == 0x2F:  # /
            self.i += 1
            return Name(_decode_name(self.regular()))
        if c == 0x28:  # (
            return self._literal()
        if c == 0x3C:  # <
            if self.d[self.i : self.i + 2] == b"<<":
                return self._dict()
            return self._hex()
        if c == 0x5B:  # [
            self.i += 1
            out: list[PdfValue] = []
            while self.peek() != 0x5D:
                if self.peek() is None:
                    raise PdfSyntaxError("unterminated array")
                out.append(self._maybe_ref())
            self.i += 1
            return out
        token = self.regular()
        if not token:
            raise PdfSyntaxError(f"unexpected byte {chr(c)!r} at {self.i}")
        return _atom(token)

    def _maybe_ref(self) -> PdfValue:
        v = self.value()
        if isinstance(v, int) and not isinstance(v, bool):
            save = self.i
            if self.peek() is not None:
                token = self.regular()
                if token.isdigit():
                    self.skip()
                    if self.d[self.i : self.i + 1] == b"R" and (
                        self.i + 1 >= len(self.d) or self.d[self.i + 1] in _WS + _DELIM
                    ):
                        self.i += 1
                        return Ref(v, int(token))
            self.i = save
        return v

    def _dict(self) -> dict[str, PdfValue]:
        self.i += 2
        out: dict[str, PdfValue] = {}
        while True:
            c = self.peek()
            if c is None:
                raise PdfSyntaxError("unterminated dictionary")
            if self.d[self.i : self.i + 2] == b">>":
                self.i += 2
                return out
            key = self.value()
            if not isinstance(key, Name):
                raise PdfSyntaxError("dictionary key is not a name")
            out[str(key)] = self._maybe_ref()

    def _literal(self) -> bytes:
        self.i += 1
        out = bytearray()
        depth = 1
        d = self.d
        while self.i < len(d):
            c = d[self.i]
            self.i += 1
            if c == 0x5C:  # backslash
                e = d[self.i : self.i + 1]
                self.i += 1
                if e in b"nrtbf":
                    out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}[e]
                elif e.isdigit():
                    digits = e
                    while len(digits) < 3 and d[self.i : self.i + 1].isdigit():
                        digits += d[self.i : self.i + 1]
                        self.i += 1
                    out.append(int(digits, 8) & 0xFF)
                elif e in (b"\r", b"\n"):
                    if e == b"\r" and d[self.i : self.i + 1] == b"\n":
                        self.i += 1
                else:
                    out += e
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
        raise PdfSyntaxError("unterminated string")

    def _hex(self) -> bytes:
        self.i += 1
        end = self.d.index(b">", self.i)
        digits = bytes(b for b in self.d[self.i : end] if b not in _WS)
        self.i = end + 1
        if len(digits) % 2:
            digits += b"0"
        return bytes.fromhex(digits.decode("ascii"))


def _decode_name(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x23 and i + 2 < len(raw) + 1:  # #xx
            try:
                out.append(int(raw[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(raw[i])
        i += 1
    return out.decode("utf-8", errors="replace")


def _atom(token: bytes) -> PdfValue:
    if token == b"true":
        return True
    if token == b"false":
        return False
    if token == b"null":
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return Name(token.decode("latin-1"))  # a bare keyword; callers treat it as opaque


def parse(text: str | bytes) -> PdfValue:
    """One PDF object (references ``n g R`` become :class:`Ref`)."""
    data = text.encode("latin-1", errors="replace") if isinstance(text, str) else text
    reader = _Reader(data)
    return reader._maybe_ref()


def text_string(raw: bytes) -> str:
    """Decode a PDF text string (UTF-16BE with BOM, UTF-8 with BOM, else PDFDocEncoding)."""
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", errors="replace")
    return raw.decode("latin-1")


def write_text_string(value: str) -> str:
    """A PDF text string literal for ``value`` (hex UTF-16BE: safe for any characters)."""
    return "<" + (codecs.BOM_UTF16_BE + value.encode("utf-16-be")).hex().upper() + ">"


def write_name(value: str) -> str:
    out = []
    for b in value.encode("utf-8"):
        c = chr(b)
        if b < 0x21 or b > 0x7E or c in "#()<>[]{}/%":
            out.append(f"#{b:02X}")
        else:
            out.append(c)
    return "/" + "".join(out)
