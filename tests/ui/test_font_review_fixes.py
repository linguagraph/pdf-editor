"""Fixes from the code review of Phase F (font management)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt

from pdfeditor.model.fonts import FontRefKind
from pdfeditor.model.objects import TextStyle
from pdfeditor.services.fonts import FontCatalog
from pdfeditor.services.stamping import Watermark
from pdfeditor.ui import font_picker
from pdfeditor.ui.dialogs.pages import WatermarkDialog
from pdfeditor.ui.view.text_editor import _initial_font_ref

pytestmark = pytest.mark.gui


def test_stamp_dialogs_keep_bold_standard_fonts(qtbot) -> None:
    dialog = WatermarkDialog(3, 0, [])
    qtbot.addWidget(dialog)
    dialog.load(replace(Watermark(text="DRAFT"), font="hebo"))
    assert dialog.font_picker.current_ref().name == "Helvetica-Bold"
    assert dialog.spec().font == "hebo"  # still bold after the round trip
    dialog.load(replace(Watermark(text="DRAFT"), font="tiro"))
    assert dialog.spec().font == "tiro"


def test_editing_starts_from_the_document_font(font_catalog: FontCatalog) -> None:
    # an installed family: the document's font first, the installed file only as a fallback
    ref, display = _initial_font_ref(TextStyle(font="TestSans"))
    assert ref.kind is FontRefKind.DOCUMENT and ref.path.endswith("TestSans-Regular.ttf")
    assert display == "TestSans"
    # a standard name selects its Standard entry, never an installed file of that name
    ref, _ = _initial_font_ref(TextStyle(font="Helvetica"))
    assert ref.kind is FontRefKind.STANDARD


def test_unchanged_standard_font_keeps_the_default_path(qtbot) -> None:
    """Untouched, "Helvetica" goes the default way (the PDF's embedded program if it can show
    the text, else base-14); picked explicitly, it's the base-14 font."""
    from PySide6.QtWidgets import QWidget

    from pdfeditor.model.fonts import FontRef
    from pdfeditor.ui.view.text_editor import TextStyleBar

    host = QWidget()
    qtbot.addWidget(host)
    bar = TextStyleBar(host, TextStyle(font="Helvetica"), lambda: None)
    assert bar.text_style().font_ref is None
    bar.font_picker.set_selection(FontRef.standard("Times-Roman"), "Times-Roman")
    assert bar.text_style().font_ref == FontRef.standard("Times-Roman")


def test_standard_fonts_are_not_added_to_recent() -> None:
    from pdfeditor.model.fonts import FontRef
    from pdfeditor.ui.settings import AppSettings

    font_picker.remember_font(FontRef.standard("Helvetica-Bold"))
    assert AppSettings().recent_fonts == []


def test_a_failing_scan_is_not_retried_in_a_loop(qtbot, monkeypatch) -> None:
    empty = FontCatalog()
    calls: list[int] = []

    def broken_scan(*_args: object, **_kwargs: object) -> None:
        calls.append(1)
        raise OSError("no access")

    monkeypatch.setattr(empty, "scan", broken_scan)
    monkeypatch.setattr(font_picker, "cached_catalog", lambda: empty)
    monkeypatch.setattr(font_picker, "_scan_started", False)
    monkeypatch.setattr(font_picker, "_scan_running", False)
    monkeypatch.setattr(font_picker, "_scan_failed", False)
    picker = font_picker.FontPicker()
    qtbot.addWidget(picker)
    qtbot.waitUntil(lambda: not font_picker._scan_running, timeout=5000)
    qtbot.wait(200)  # a retry loop would have started more scans by now
    assert calls == [1]
    rows = [picker.itemText(r) for r in range(picker.count())]
    assert "Couldn't read the installed fonts" in rows
    second = font_picker.FontPicker()  # a new picker gets one more try
    qtbot.addWidget(second)
    qtbot.waitUntil(lambda: len(calls) == 2 and not font_picker._scan_running, timeout=5000)


def test_damaged_font_cache_is_ignored(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "fonts.json"
    from pdfeditor.services.fonts import CACHE_VERSION

    bad = {"version": CACHE_VERSION, "files": {"x.ttf": {"faces": [{"scripts": "latin"}]}}}
    cache.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr("pdfeditor.services.fonts._cache_path", lambda: cache)
    catalog = FontCatalog()
    catalog.load_cache()  # used to raise AssertionError and break every font picker
    assert catalog.faces() == []


def test_picker_says_when_no_installed_fonts_were_found(qtbot, monkeypatch) -> None:
    empty = FontCatalog()
    monkeypatch.setattr(font_picker, "cached_catalog", lambda: empty)
    monkeypatch.setattr(font_picker, "_scan_started", True)  # a scan already ran...
    monkeypatch.setattr(font_picker, "_scan_running", False)  # ...and found nothing
    picker = font_picker.FontPicker()
    qtbot.addWidget(picker)
    rows = [
        picker.model().index(r, 0).data(Qt.ItemDataRole.DisplayRole) for r in range(picker.count())
    ]
    assert "No installed fonts found" in rows and "Loading installed fonts…" not in rows
