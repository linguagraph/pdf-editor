"""Marked page artifacts (PDF 32000-1 14.8.2.2): headers, footers, watermarks and backgrounds
added to a page as one unit, so they can be found again, replaced or removed.

What the app adds is wrapped in ``/Artifact <</Type /Pagination /Subtype /Header
/PDFEditorMark /HeaderFooter>> BDC ... EMC``: the standard artifact entries tell readers
(screen readers, text extraction for reuse) that it isn't real content, and the private
``/PDFEditorMark`` entry names the feature that added it. Acrobat writes the same kind of
section around a form XObject whose piece info names the feature (checked by the backend).

Engine-neutral: the backend resolves named property lists and hands over parsed operations.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

from pdfeditor.engine.contentstream.optional import strip_sections
from pdfeditor.engine.contentstream.parser import Name, Operand, Operation, write_operand

ARTIFACT = "Artifact"
APP_KEY = "PDFEditorMark"

Properties = dict[Name, Operand]
PropertyResolver = Callable[[str], Properties | None]  # /Properties resource name -> dict


@dataclass(frozen=True, slots=True)
class ArtifactSection:
    start: int  # index of the BDC
    end: int  # index of its matching EMC
    properties: Properties

    def name(self, key: str) -> str | None:
        """A name-valued entry of the property list, without the slash."""
        value = self.properties.get(Name(key))
        return str(value) if isinstance(value, Name) else None

    @property
    def app_mark(self) -> str | None:
        """The feature name the app stored in the section, or None for other artifacts."""
        return self.name(APP_KEY)


def begin_operator(mark: str, artifact_type: str, subtype: str | None) -> bytes:
    """The ``BDC`` line that opens an app-added artifact section of feature ``mark``."""
    props: Properties = {Name("Type"): Name(artifact_type)}
    if subtype:
        props[Name("Subtype")] = Name(subtype)
    props[Name(APP_KEY)] = Name(mark)
    return write_operand(Name(ARTIFACT)) + b" " + write_operand(props) + b" BDC\n"


def artifact_sections(
    ops: Sequence[Operation], resolve: PropertyResolver = lambda _name: None
) -> list[ArtifactSection]:
    """Outermost ``/Artifact`` sections that have a property list (unterminated ones are
    ignored: they can't be removed safely)."""
    found: list[ArtifactSection] = []
    start = -1
    props: Properties = {}
    depth = 0
    for index, op in enumerate(ops):
        name = op.operator
        if name in ("BDC", "BMC"):
            if start >= 0:
                depth += 1
            elif name == "BDC" and _is_artifact(op):
                resolved = _properties(op.operands[1], resolve)
                if resolved is not None:
                    start, props, depth = index, resolved, 1
        elif name == "EMC" and start >= 0:
            depth -= 1
            if depth == 0:
                found.append(ArtifactSection(start, index, props))
                start = -1
    return found


def section_xobjects(ops: Sequence[Operation], section: ArtifactSection) -> list[str]:
    """Names of the XObjects a section draws with ``Do``."""
    return [
        str(op.operands[0])
        for op in ops[section.start + 1 : section.end]
        if op.operator == "Do" and op.operands and isinstance(op.operands[0], Name)
    ]


def remove_sections(
    ops: Sequence[Operation], sections: Collection[ArtifactSection]
) -> list[Operation]:
    """Delete ``sections`` from ``ops``.

    A section that saves and restores everything it changes (``q ... Q``, as the app and
    Acrobat write them) is cut out whole. Any other one only loses what it paints, so the
    graphics state that later content relies on stays the same.
    """
    cut: set[int] = set()
    partial: set[int] = set()
    for s in sections:
        if _self_contained(ops[s.start + 1 : s.end]):
            cut.update(range(s.start, s.end + 1))
        else:
            partial.add(id(ops[s.start]))
    kept = [op for i, op in enumerate(ops) if i not in cut]
    if not partial:
        return kept
    stripped, _count = strip_sections(kept, lambda _i, op: id(op) in partial)
    return stripped


def _is_artifact(op: Operation) -> bool:
    return (
        len(op.operands) >= 2
        and isinstance(op.operands[0], Name)
        and str(op.operands[0]) == ARTIFACT
    )


def _properties(operand: Operand, resolve: PropertyResolver) -> Properties | None:
    if isinstance(operand, dict):
        return operand
    if isinstance(operand, Name):
        return resolve(str(operand))
    return None


# Operators whose effect outlives the section unless a ``q``/``Q`` pair around them undoes it
# (the text matrix is reset by every BT, so Tm/Td don't count).
_LASTING = {
    "cm", "w", "J", "j", "M", "d", "ri", "i", "gs",
    "CS", "cs", "SC", "SCN", "sc", "scn", "G", "g", "RG", "rg", "K", "k",
    "Tc", "Tw", "Tz", "TL", "Tf", "Tr", "Ts", "W", "W*",
}  # fmt: skip


def _self_contained(inner: Sequence[Operation]) -> bool:
    saves = texts = 0
    for op in inner:
        name = op.operator
        if name == "q":
            saves += 1
        elif name == "Q":
            saves -= 1
            if saves < 0:
                return False
        elif name == "BT":
            texts += 1
        elif name == "ET":
            texts -= 1
            if texts < 0:
                return False
        elif saves == 0 and name in _LASTING:
            return False
    return saves == 0 and texts == 0
