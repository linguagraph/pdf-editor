"""Optional content (layers): decide visibility and strip hidden content from a content stream.

Engine-neutral: the backend resolves resource names to "hidden or not" and hands over the
parsed operations (PDF 32000-1 8.11).

Hidden sections are ``/OC /name BDC ... EMC`` sequences whose property list is a hidden OCG
or OCMD, and ``Do`` of XObjects that carry a hidden ``/OC`` entry. Marked content doesn't have
to nest with ``q``/``Q`` or ``BT``/``ET`` (MuPDF itself writes ``/OC /x BDC q ... EMC Q``), so a
hidden section isn't simply cut out: everything that *paints* is dropped while the operators
that change graphics or text state are kept, so the content after the section draws exactly
as before.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from typing import TypeAlias

from pdfeditor.engine.contentstream.objects import CLIPPING, PATH_CONSTRUCTION, PATH_PAINTING
from pdfeditor.engine.contentstream.parser import Name, Operation

# -- visibility -------------------------------------------------------------------------------
VisibilityExpr: TypeAlias = "int | list[str | VisibilityExpr]"
"""An OCMD /VE expression: an OCG id, or ``["And" | "Or" | "Not", operand, ...]``."""


def ocmd_visible(
    ocgs: Sequence[int],
    policy: str,
    is_on: Callable[[int], bool],
    expression: VisibilityExpr | None = None,
) -> bool:
    """Visibility of an optional-content membership dictionary (8.11.2.2).

    ``/VE`` takes precedence over ``/OCGs`` + ``/P``; an OCMD that names no groups is visible.
    """
    if expression is not None:
        result = _evaluate(expression, is_on)
        if result is not None:
            return result
    if not ocgs:
        return True
    states = [is_on(g) for g in ocgs]
    if policy == "AllOn":
        return all(states)
    if policy == "AnyOff":
        return not all(states)
    if policy == "AllOff":
        return not any(states)
    return any(states)  # AnyOn, the default


def _evaluate(expr: VisibilityExpr, is_on: Callable[[int], bool]) -> bool | None:
    if isinstance(expr, int):
        return is_on(expr)
    if not expr or not isinstance(expr[0], str):
        return None
    op, args = expr[0], [a for a in expr[1:] if not isinstance(a, str)]
    values = [_evaluate(a, is_on) for a in args]
    known = [v for v in values if v is not None]
    if not known:
        return None
    if op == "Not":
        return not known[0]
    if op == "And":
        return all(known)
    if op == "Or":
        return any(known)
    return None


# -- stripping --------------------------------------------------------------------------------
# Operators kept inside a hidden section because later content depends on the state they set.
_STATE = {
    "q", "Q", "cm", "w", "J", "j", "M", "d", "ri", "i", "gs",
    "CS", "cs", "SC", "SCN", "sc", "scn", "G", "g", "RG", "rg", "K", "k",
    "BT", "ET", "Tc", "Tw", "Tz", "TL", "Tf", "Tr", "Ts", "Td", "TD", "Tm", "T*",
    "BX", "EX", "d0", "d1",
}  # fmt: skip


def strip_hidden_content(
    ops: Sequence[Operation],
    hidden_properties: Collection[str],
    hidden_xobjects: Collection[str] = (),
) -> tuple[list[Operation], int]:
    """Remove what hidden optional content paints; returns the new operations and how many
    hidden sections (marked-content sequences and XObject calls) were removed.

    ``hidden_properties`` are /Properties resource names of hidden OCGs/OCMDs;
    ``hidden_xobjects`` are /XObject resource names whose /OC is hidden.
    """
    return strip_sections(
        ops,
        lambda _i, op: op.operator == "BDC" and _is_hidden_oc(op, hidden_properties),
        lambda name: name in hidden_xobjects,
    )


def strip_sections(
    ops: Sequence[Operation],
    starts_section: Callable[[int, Operation], bool],
    drops_xobject: Callable[[str], bool] = lambda _name: False,
) -> tuple[list[Operation], int]:
    """Remove what marked-content sections paint, keeping their state changes.

    A section starts at a ``BDC``/``BMC`` (at ``ops[index]``) that ``starts_section`` picks and
    runs to its matching ``EMC``; ``Do`` of XObjects that ``drops_xobject`` picks is removed
    anywhere. Returns the new operations and how many sections and calls were removed.
    """
    out: list[Operation] = []
    removed = 0
    depth = 0  # marked-content nesting inside the current removed section (0 = outside)
    path: list[Operation] = []
    for index, op in enumerate(ops):
        name = op.operator
        if depth == 0:
            if name in ("BDC", "BMC") and starts_section(index, op):
                depth = 1
                removed += 1
            elif name == "Do" and op.operands and drops_xobject(str(op.operands[0])):
                removed += 1
            else:
                out.append(op)
            continue
        if name in ("BMC", "BDC"):
            depth += 1
        elif name == "EMC":
            depth -= 1
        elif name in PATH_CONSTRUCTION or name in CLIPPING:
            path.append(op)
        elif name in PATH_PAINTING:
            if any(p.operator in CLIPPING for p in path):
                # a clipping path set here still clips what follows: keep it, unpainted
                out.extend(path)
                out.append(Operation("n"))
            path = []
        elif name == "'":
            out.append(Operation("T*"))
        elif name == '"':
            if len(op.operands) >= 2:
                out.append(Operation("Tw", [op.operands[0]]))
                out.append(Operation("Tc", [op.operands[1]]))
            out.append(Operation("T*"))
        elif name in _STATE:
            out.append(op)
        # everything else paints (Tj, TJ, Do, BI, sh) or is marked content: dropped
    return _drop_empty_saves(out), removed


def _is_hidden_oc(op: Operation, hidden: Collection[str]) -> bool:
    operands = op.operands
    return (
        len(operands) >= 2
        and isinstance(operands[0], Name)
        and str(operands[0]) == "OC"
        and isinstance(operands[1], Name)
        and str(operands[1]) in hidden
    )


def _drop_empty_saves(ops: list[Operation]) -> list[Operation]:
    """Remove ``q Q`` and ``BT ET`` pairs left with nothing between them (both are no-ops)."""
    out: list[Operation] = []
    for op in ops:
        if out and (out[-1].operator, op.operator) in (("q", "Q"), ("BT", "ET")):
            out.pop()
            continue
        out.append(op)
    return out
