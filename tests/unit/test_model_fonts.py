from __future__ import annotations

from pdfeditor.model.fonts import FontFace, FontRef, FontRefKind
from pdfeditor.model.geometry import Point
from pdfeditor.model.objects import TextStyle
from pdfeditor.model.pages import TextStamp


def test_font_ref_standard() -> None:
    ref = FontRef.standard("Helvetica")
    assert ref == FontRef(FontRefKind.STANDARD, "Helvetica")
    assert ref.path == "" and ref.index == 0


def test_font_ref_document() -> None:
    ref = FontRef.document("F1")
    assert ref.kind is FontRefKind.DOCUMENT
    assert ref.name == "F1"


def test_font_ref_file_defaults_name_to_path() -> None:
    ref = FontRef.file("/fonts/Foo.ttf")
    assert ref.name == "/fonts/Foo.ttf"
    assert ref.index == 0


def test_font_ref_file_with_index_and_name() -> None:
    ref = FontRef.file("/fonts/Foo.ttc", index=1, name="Foo Bold")
    assert ref.index == 1
    assert ref.name == "Foo Bold"
    assert ref.path == "/fonts/Foo.ttc"


def test_font_ref_round_trip_dict() -> None:
    ref = FontRef.file("/fonts/Foo.ttc", index=1, name="Foo Bold")
    assert FontRef.from_dict(ref.to_dict()) == ref


def test_font_ref_round_trip_standard() -> None:
    ref = FontRef.standard("Times-Roman")
    assert FontRef.from_dict(ref.to_dict()) == ref


def test_font_ref_from_dict_invalid_kind() -> None:
    assert FontRef.from_dict({"kind": "bogus", "name": "x"}) is None


def test_font_ref_from_dict_missing_fields() -> None:
    assert FontRef.from_dict({}) is None
    assert FontRef.from_dict({"kind": "standard"}) is None


def test_font_ref_from_dict_wrong_types() -> None:
    assert FontRef.from_dict({"kind": "standard", "name": 5}) is None
    assert FontRef.from_dict({"kind": "file", "name": "x", "path": 5}) is None
    assert FontRef.from_dict({"kind": "file", "name": "x", "index": "bad"}) is None


def test_font_ref_from_dict_not_a_dict() -> None:
    assert FontRef.from_dict(None) is None  # type: ignore[arg-type]


def test_font_face_defaults() -> None:
    face = FontFace(
        family="Test Sans",
        style="Regular",
        weight=400,
        italic=False,
        path="/fonts/TestSans.ttf",
    )
    assert face.index == 0
    assert face.embeddable is True
    assert face.reason == ""
    assert face.scripts == frozenset()
    assert face.variable is False


def test_text_style_default_font_ref_is_none() -> None:
    style = TextStyle()
    assert style.font_ref is None


def test_text_style_positional_construction_unaffected() -> None:
    # existing positional callers (font, size, color, bold, italic, align, line_height) must
    # keep working with the new field appended at the end
    style = TextStyle("Times-Roman", 10.0)
    assert style.font == "Times-Roman"
    assert style.size == 10.0
    assert style.font_ref is None


def test_text_style_with_font_ref() -> None:
    ref = FontRef.file("/fonts/Foo.ttf")
    style = TextStyle(font_ref=ref)
    assert style.font_ref == ref


def test_text_stamp_default_font_ref_is_none() -> None:
    stamp = TextStamp("hello", Point(0, 0))
    assert stamp.font_ref is None


def test_text_stamp_with_font_ref() -> None:
    ref = FontRef.standard("Times-Bold")
    stamp = TextStamp("hello", Point(0, 0), font_ref=ref)
    assert stamp.font_ref == ref
