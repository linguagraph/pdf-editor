"""Finding and removing marked page artifacts (headers, watermarks, ...) in content streams."""

from __future__ import annotations

from pdfeditor.engine.contentstream.artifacts import (
    artifact_sections,
    begin_operator,
    remove_sections,
    section_xobjects,
)
from pdfeditor.engine.contentstream.parser import Name, parse, write


def ops_text(ops) -> str:
    return write(ops).decode().replace("\n", " ").strip()


def test_begin_operator_is_a_standard_artifact_with_the_feature() -> None:
    (op,) = parse(begin_operator("HeaderFooter", "Pagination", "Header"))
    assert op.operator == "BDC"
    assert op.operands[0] == Name("Artifact")
    props = op.operands[1]
    assert props == {
        Name("Type"): Name("Pagination"),
        Name("Subtype"): Name("Header"),
        Name("PDFEditorMark"): Name("HeaderFooter"),
    }
    (bg,) = parse(begin_operator("Background", "Background", None))
    assert Name("Subtype") not in bg.operands[1]


def test_sections_are_outermost_and_resolve_named_properties() -> None:
    src = (
        begin_operator("Watermark", "Pagination", "Watermark")
        + b"q /Span <</ActualText (x)>> BDC BT (W) Tj ET EMC Q EMC "
        b"/P <</MCID 0>> BDC BT (body) Tj ET EMC "
        b"/Artifact /MC0 BDC q /Fm0 Do Q EMC "
        b"/Artifact /Missing BDC (x) Tj EMC "
        b"/Artifact <</Type /Pagination>> BDC (open"
        b") Tj"  # never closed: ignored
    )
    ops = parse(src)
    named = {Name("Type"): Name("Pagination"), Name("Subtype"): Name("Watermark")}
    found = artifact_sections(ops, lambda name: named if name == "MC0" else None)
    assert [s.app_mark for s in found] == ["Watermark", None]
    assert found[1].name("Subtype") == "Watermark"
    assert section_xobjects(ops, found[1]) == ["Fm0"]
    assert ops[found[0].end].operator == "EMC" and found[0].end < found[1].start


def test_self_contained_sections_are_cut_out_whole() -> None:
    src = (
        b"q 1 0 0 rg 0 0 10 10 re f Q "
        + begin_operator("HeaderFooter", "Pagination", "Footer")
        + b"q BT /helv 9 Tf 1 0 0 1 36 20 Tm (Page 1) Tj ET Q EMC "
        b"BT /F1 12 Tf (body) Tj ET"
    )
    ops = parse(src)
    out = remove_sections(ops, artifact_sections(ops))
    assert ops_text(out) == "q 1 0 0 rg 0 0 10 10 re f Q BT /F1 12 Tf (body) Tj ET"


def test_sections_that_change_lasting_state_keep_it() -> None:
    # Not wrapped in q/Q: the font and colour it sets are still in force for the body text.
    src = (
        b"/Artifact <</Type /Pagination /Subtype /Header>> BDC BT /F1 9 Tf 0 0 1 rg (H) Tj ET EMC "
    )
    src += b"BT (body) Tj ET"
    ops = parse(src)
    out = ops_text(remove_sections(ops, artifact_sections(ops)))
    assert "(H)" not in out and "/F1 9 Tf" in out and "0 0 1 rg" in out
    assert out.endswith("BT (body) Tj ET")
