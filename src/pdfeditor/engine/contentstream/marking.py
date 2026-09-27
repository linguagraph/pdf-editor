"""Marked-content rewriting for auto-tagging (PDF 32000-1 14.6), engine-neutral.

Two pieces: :func:`text_show_points` tracks the text state far enough to say *where* each
text-showing operator starts drawing (no font metrics needed: the start of a show is fixed by
the text line matrix; a second show on the same line gets the line's start, which is close
enough to decide which paragraph it belongs to), and :func:`insert_marks` wraps operator
ranges in ``BDC``/``BMC`` ... ``EMC``.

Ranges handed to :func:`insert_marks` must be properly nested with the stream's own
``q``/``Q``, ``BT``/``ET`` and marked content. :func:`text_runs` produces such ranges: runs of
text shows inside one text object that never cross an existing marked-content operator.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from pdfeditor.engine.contentstream.parser import Name, Operand, Operation
from pdfeditor.model.geometry import Matrix, Point

TEXT_SHOWING = {"Tj", "TJ", "'", '"'}
MARKED_CONTENT = {"BMC", "BDC", "EMC", "MP", "DP"}
_BASELINE_LIFT = 0.3  # probe a little above the baseline so it lands inside the glyph boxes

K = TypeVar("K", bound=Hashable)


def _num(v: Operand) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _mat(values: Sequence[Operand]) -> Matrix | None:
    nums = [_num(v) for v in values]
    if len(nums) != 6 or any(n is None for n in nums):
        return None
    a, b, c, d, e, f = (n for n in nums if n is not None)
    return Matrix(a, b, c, d, e, f)


def text_show_points(ops: Sequence[Operation]) -> dict[int, Point]:
    """Operation index of every text show inside ``BT``/``ET`` -> a point (PDF user space)
    just above where it starts drawing."""
    out: dict[int, Point] = {}
    ctm = Matrix.identity()
    stack: list[Matrix] = []
    tm = tlm = Matrix.identity()
    leading = 0.0
    size = 0.0
    rise = 0.0
    in_text = False

    def next_line(tx: float, ty: float) -> None:
        nonlocal tm, tlm
        tlm = Matrix.translate(tx, ty) @ tlm
        tm = tlm

    for i, op in enumerate(ops):
        name, args = op.operator, op.operands
        if name == "q":
            stack.append(ctm)
        elif name == "Q":
            ctm = stack.pop() if stack else Matrix.identity()
        elif name == "cm":
            m = _mat(args)
            if m is not None:
                ctm = m @ ctm
        elif name == "BT":
            in_text = True
            tm = tlm = Matrix.identity()
        elif name == "ET":
            in_text = False
        elif name == "Tf" and len(args) == 2:
            size = _num(args[1]) or 0.0
        elif name == "TL" and args:
            leading = _num(args[0]) or 0.0
        elif name == "Ts" and args:
            rise = _num(args[0]) or 0.0
        elif name == "Tm":
            m = _mat(args)
            if m is not None:
                tm = tlm = m
        elif name in ("Td", "TD") and len(args) == 2:
            tx, ty = _num(args[0]) or 0.0, _num(args[1]) or 0.0
            if name == "TD":
                leading = -ty
            next_line(tx, ty)
        elif name == "T*":
            next_line(0.0, -leading)
        elif name in TEXT_SHOWING and in_text:
            if name in ("'", '"'):
                next_line(0.0, -leading)
            probe = Point(0.0, rise + abs(size) * _BASELINE_LIFT)
            out[i] = probe.transform(tm @ ctm)
            # later shows on this line start further right; the line start is a good proxy
    return out


def text_runs(ops: Sequence[Operation], owner: Mapping[int, K]) -> list[tuple[int, int, K]]:
    """Group consecutive text shows with the same owner into ``(start, end, owner)`` ranges.

    A run never leaves its text object and never contains a marked-content operator, so
    wrapping it keeps every sequence properly nested. Shows missing from ``owner`` end a run
    and stay unmarked.
    """
    runs: list[tuple[int, int, K]] = []
    current: tuple[int, int, K] | None = None
    for i, op in enumerate(ops):
        name = op.operator
        if name in TEXT_SHOWING and i in owner:
            key = owner[i]
            if current is not None and current[2] == key:
                current = (current[0], i, key)
            else:
                if current is not None:
                    runs.append(current)
                current = (i, i, key)
        elif name in TEXT_SHOWING or name in MARKED_CONTENT or name in ("BT", "ET"):
            if current is not None:
                runs.append(current)
            current = None
    if current is not None:
        runs.append(current)
    return runs


@dataclass(frozen=True, slots=True)
class Mark:
    """A marked-content wrapper: ``/tag <</MCID n>> BDC`` or, without an MCID, ``/tag BMC``."""

    tag: str
    mcid: int | None = None

    def begin(self) -> Operation:
        if self.mcid is None:
            return Operation("BMC", [Name(self.tag)])
        return Operation("BDC", [Name(self.tag), {Name("MCID"): self.mcid}])


def insert_marks(
    ops: Sequence[Operation], marks: Sequence[tuple[int, int, Mark]]
) -> list[Operation]:
    """Wrap each inclusive ``(start, end)`` range in its mark; ranges must not overlap."""
    begins = {start: mark for start, _end, mark in marks}
    ends = {end for _start, end, _mark in marks}
    if len(begins) != len(marks):
        raise ValueError("overlapping marked-content ranges")
    out: list[Operation] = []
    for i, op in enumerate(ops):
        mark = begins.get(i)
        if mark is not None:
            out.append(mark.begin())
        out.append(op)
        if i in ends:
            out.append(Operation("EMC"))
    return out


def has_marked_content(ops: Sequence[Operation], start: int, end: int) -> bool:
    return any(ops[i].operator in MARKED_CONTENT for i in range(start, end + 1))


def strip_mcids(ops: Sequence[Operation]) -> list[Operation]:
    """Drop MCIDs left over from lost or stripped tags (they'd clash with new ones); the
    marked-content sequences themselves and their other properties stay."""
    out: list[Operation] = []
    for op in ops:
        props = op.operands[1] if op.operator == "BDC" and len(op.operands) == 2 else None
        if isinstance(props, dict) and Name("MCID") in props:
            rest = {k: v for k, v in props.items() if k != "MCID"}
            tag = op.operands[0]
            op = Operation("BDC", [tag, rest]) if rest else Operation("BMC", [tag])
        out.append(op)
    return out
