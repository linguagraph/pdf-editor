"""Tags (structure tree) and accessibility settings through the Protocol."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdfeditor.core.commands import (
    ReorderStructCommand,
    SetAccessibilityCommand,
    SetStructElementCommand,
)
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import Engine, EngineError
from pdfeditor.engine.pdfobject import Name, Ref, parse, text_string, write_name, write_text_string
from pdfeditor.model.structure import AccessibilitySettings, walk_all


def test_pdf_object_parser() -> None:
    assert parse("[15 0 R 16 0 R]") == [Ref(15), Ref(16)]
    assert parse("[<</Type/MCR/MCID 0/Pg 3 0 R>> 5 0 R 7]") == [
        {"Type": "MCR", "MCID": 0, "Pg": Ref(3)},
        Ref(5),
        7,
    ]
    d = parse(r"<</S/H1 /Alt (A \(pic\)) /K [1 2] /N null /B true>>")
    assert d == {"S": "H1", "Alt": b"A (pic)", "K": [1, 2], "N": None, "B": True}
    assert isinstance(d["S"], Name)
    assert text_string(parse(write_text_string("Снимка (1)"))) == "Снимка (1)"
    assert parse(write_name("My Tag#1")) == "My Tag#1"
    assert parse("12") == 12 and parse("-1.5") == -1.5


def test_read_tree(engine: Engine, fixture_pdf) -> None:
    if not engine.capabilities.structure:
        pytest.skip("no structure support")
    doc = engine.open(fixture_pdf("tagged"))
    (root,) = doc.structure_tree()
    assert root.type == "Document"
    assert [n.type for n in root.children] == ["H1", "P", "P", "Figure", "H3", "P", "H2"]
    assert [n.page_index for n in root.children] == [0, 0, 1, 0, 0, 0, 1]
    assert root.children[3].alt == ""
    assert engine.open(fixture_pdf("report")).structure_tree() == []
    doc.close()


def test_indirect_values_are_read(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    """Real files (e.g. IRS forms) store /Lang, /DisplayDocTitle and /Alt as indirect objects."""
    if not engine.capabilities.structure:
        pytest.skip("no structure support")
    path = tmp_path / "indirect.pdf"
    with pikepdf.open(fixture_pdf("tagged")) as pdf:
        pdf.Root.Lang = pdf.make_indirect(pikepdf.String("de-DE"))
        pdf.Root.ViewerPreferences = pikepdf.Dictionary(
            DisplayDocTitle=pdf.make_indirect(pikepdf.Object.parse(b"true"))
        )
        pdf.Root.StructTreeRoot.K.K[3].Alt = pdf.make_indirect(pikepdf.String("A chart"))
        pdf.save(path)
    doc = engine.open(path)
    settings = doc.accessibility_settings()
    assert settings.language == "de-DE" and settings.display_doc_title
    (root,) = doc.structure_tree()
    assert root.children[3].alt == "A chart"
    doc.close()


def test_edit_tags_round_trip(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc = engine.open(fixture_pdf("tagged"))
    (root,) = doc.structure_tree()
    fig, h3 = root.children[3], root.children[4]
    doc.set_struct_element(fig.ref, alt="Chart of sales — Снимка")
    doc.set_struct_element(h3.ref, type="H2")
    order = [c.ref for c in root.children]
    new_order = [order[0], order[1], order[3], order[4], order[5], order[2], order[6]]
    doc.reorder_struct_children(root.ref, new_order)
    with pytest.raises(EngineError):
        doc.reorder_struct_children(root.ref, new_order[:-1])
    with pytest.raises(EngineError):
        doc.set_struct_element(root.ref, type=" ")
    doc.set_accessibility_settings(AccessibilitySettings("bg-BG", True, True))
    out = doc.save(tmp_path / "fixed.pdf")
    doc.close()
    reopened = engine.open(out)
    (root,) = reopened.structure_tree()
    assert [n.type for n in root.children] == ["H1", "P", "Figure", "H2", "P", "P", "H2"]
    assert root.children[2].alt == "Chart of sales — Снимка"
    assert reopened.accessibility_settings() == AccessibilitySettings("bg-BG", True, True)
    reopened.close()
    with pikepdf.open(out) as pdf:
        assert str(pdf.Root.Lang) == "bg-BG" and pdf.Root.ViewerPreferences.DisplayDocTitle
        assert str(pdf.Root.StructTreeRoot.K.K[2].Alt) == "Chart of sales — Снимка"
        assert all(p.obj.Tabs == "/S" for p in pdf.pages)


def test_commands_undo(fixture_pdf) -> None:
    session = DocumentSession.open(fixture_pdf("tagged"))
    doc = session.document
    (root,) = doc.structure_tree()
    fig = root.children[3]
    session.execute(SetStructElementCommand(fig.ref, fig.type, fig.alt, None, "A chart"))
    assert next(n for n in walk_all(doc.structure_tree()) if n.ref == fig.ref).alt == "A chart"
    order = [c.ref for c in root.children]
    session.execute(ReorderStructCommand(root.ref, order, list(reversed(order))))
    session.execute(SetAccessibilityCommand(AccessibilitySettings("en-US", True, True)))
    for _ in range(3):
        session.undo()
    (root,) = doc.structure_tree()
    assert [c.ref for c in root.children] == order and root.children[3].alt == ""
    assert doc.accessibility_settings().language == ""
    session.undo_stack.set_clean()
    session.close()
